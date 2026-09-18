from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.aliyun.client import AliyunError
from app.aliyun.records import mailbox_cleanup_wanted, parse_guide_records
from app.config import Settings
from app.db.models import AliyunAccount, Domain, YydsAccount, YydsDomainSnapshot
from app.domainutil import domains_match
from app.worker.clients import aliyun_client, persist_yyds_session, yyds_client
from app.worker.events import add_event
from app.worker.locks import (
    acquire_fill_lock,
    acquire_yyds_session_lock,
    release_fill_lock,
    release_yyds_session_lock,
)
from app.worker.logic import STATUS_USED, decrement_known_occupancy
from app.yyds.client import YydsError


DNS_CLEANUP_ERROR_PREFIX = "yyds 已删除，阿里云解析清理失败"


class YydsDomainDeleteError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mailbox_dns_cleanup_pending(error_reason: str | None) -> bool:
    return bool(error_reason) and DNS_CLEANUP_ERROR_PREFIX in error_reason


def _panel_status_for_yyds_error(exc: YydsError) -> int:
    code = int(exc.status_code or 502)
    if code in {409, 429}:
        return code
    return 502


def _guide_wanted(yyds, domain_id: str, domain_name: str):
    wanted = list(mailbox_cleanup_wanted())
    try:
        wanted.extend(parse_guide_records(yyds.dns_guide(domain_id), domain_name))
    except YydsError:
        pass
    return wanted


def _drop_snapshot_name(session: Session, yyds_account_id: UUID, domain_name: str) -> None:
    snap = session.scalar(select(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id == yyds_account_id))
    if snap is None:
        return
    try:
        names = json.loads(snap.names_json or "[]")
    except json.JSONDecodeError:
        return
    if not isinstance(names, list):
        return
    snap.names_json = json.dumps(
        [item for item in names if str(item or "").strip() and not domains_match(str(item), domain_name)],
        ensure_ascii=False,
    )


def _unbind_local(row: Domain, *, error_reason: str | None) -> None:
    row.yyds_account_id = None
    row.yyds_domain_id = None
    row.yyds_account = None
    row.filling_at = None
    row.status = STATUS_USED
    row.error_reason = error_reason
    row.updated_at = _now()


def _cleanup_mailbox_dns(session: Session, settings: Settings, row: Domain, wanted) -> str | None:
    if not row.aliyun_account_id:
        return f"{DNS_CLEANUP_ERROR_PREFIX}: 阿里云账号不存在"
    aliyun_account = session.get(AliyunAccount, row.aliyun_account_id)
    if aliyun_account is None:
        return f"{DNS_CLEANUP_ERROR_PREFIX}: 阿里云账号不存在"
    try:
        aliyun_client(settings, aliyun_account).delete_mailbox_records(row.name, wanted)
    except AliyunError as exc:
        return f"{DNS_CLEANUP_ERROR_PREFIX}: {exc}"
    return None


def delete_bound_yyds_domain(session: Session, settings: Settings, domain_id: UUID) -> Domain:
    row = session.get(Domain, domain_id)
    if row is None:
        raise YydsDomainDeleteError("域名不存在", 404)
    retry_dns = mailbox_dns_cleanup_pending(row.error_reason) and not row.yyds_domain_id
    if not retry_dns and (not row.yyds_account_id or not row.yyds_domain_id):
        raise YydsDomainDeleteError("域名未绑定 yyds", 400)
    if row.filling_at is not None:
        raise YydsDomainDeleteError("域名正在补位，请稍后再删", 409)
    account = None
    if row.yyds_account_id:
        account = session.get(YydsAccount, row.yyds_account_id)
        if account is None and not retry_dns:
            raise YydsDomainDeleteError("yyds 账号不存在", 404)

    lock_conn = acquire_fill_lock(session)
    yyds_lock = None
    yyds = None
    try:
        session.refresh(row)
        retry_dns = mailbox_dns_cleanup_pending(row.error_reason) and not row.yyds_domain_id
        if row.filling_at is not None:
            raise YydsDomainDeleteError("域名正在补位，请稍后再删", 409)
        if not retry_dns and (not row.yyds_account_id or not row.yyds_domain_id):
            raise YydsDomainDeleteError("域名未绑定 yyds", 400)
        if row.yyds_account_id and account is None:
            account = session.get(YydsAccount, row.yyds_account_id)
        if account is None and not retry_dns:
            raise YydsDomainDeleteError("yyds 账号不存在", 404)
        if account is not None:
            yyds_lock = acquire_yyds_session_lock(session, account.id)

        yyds_account_id = row.yyds_account_id
        yyds_domain_id = row.yyds_domain_id
        wanted = list(mailbox_cleanup_wanted())
        if not retry_dns:
            yyds = yyds_client(settings, account)
            wanted = _guide_wanted(yyds, yyds_domain_id, row.name)
            deleted_ok = False
            try:
                yyds.delete_domain(yyds_domain_id)
                deleted_ok = True
            except YydsError as exc:
                persist_yyds_session(settings, account, yyds)
                if exc.status_code != 404:
                    raise YydsDomainDeleteError(
                        f"yyds 删除域名失败: {exc}", _panel_status_for_yyds_error(exc)
                    ) from exc
            persist_yyds_session(settings, account, yyds)
            if deleted_ok:
                account.used_wildcard = decrement_known_occupancy(account.used_wildcard)
                account.used_domains = decrement_known_occupancy(account.used_domains)

        dns_error = _cleanup_mailbox_dns(session, settings, row, wanted)
        if yyds_account_id:
            _drop_snapshot_name(session, yyds_account_id, row.name)
        _unbind_local(row, error_reason=dns_error)
        add_event(
            session,
            level="error" if dns_error else "info",
            code="yyds_domain_deleted",
            message=dns_error or f"已从 yyds 删除 {row.display_name or row.name}，并清理邮箱解析",
            domain_name=row.name,
            aliyun_account_id=row.aliyun_account_id,
            yyds_account_id=yyds_account_id,
        )
        session.flush()
        session.commit()
        return row
    finally:
        if yyds is not None:
            yyds.close()
        release_yyds_session_lock(yyds_lock)
        release_fill_lock(lock_conn)
