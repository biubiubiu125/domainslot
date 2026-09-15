from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.aliyun.records import parse_guide_records
from app.config import Settings
from app.db.models import AliyunAccount, Domain, YydsAccount
from app.worker.clients import aliyun_client, yyds_client
from app.worker.events import add_event
from app.worker.locks import acquire_fill_lock, release_fill_lock
from app.worker.logic import (
    STATUS_ERROR,
    STATUS_USED,
    AccountView,
    DomainView,
    select_fill_account,
    select_unused_domain,
)
from app.worker.poll_yyds import persist_yyds_session
from app.yyds.client import YydsError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _account_views(accounts: list[YydsAccount]) -> list[AccountView]:
    return [
        AccountView(
            id=str(item.id),
            sort_order=item.sort_order,
            receive_enabled=item.receive_enabled,
            enabled=item.enabled,
            login_ok=not item.login_error,
            max_wildcard=item.max_wildcard,
            used_wildcard=item.used_wildcard or 0,
        )
        for item in accounts
    ]


def fill_available(session: Session, settings: Settings, prefer_yyds_id: str | None = None) -> bool:
    acquire_fill_lock(session)
    try:
        _clear_stale_filling(session)
        repaired = _repair_bound_errors(session, settings)
        if repaired:
            return True
        return _fill_new(session, settings, prefer_yyds_id)
    finally:
        release_fill_lock(session)


def _clear_stale_filling(session: Session) -> None:
    cutoff = _now() - timedelta(minutes=15)
    rows = list(session.scalars(select(Domain).where(Domain.filling_at.is_not(None))).all())
    for row in rows:
        filled_at = _aware(row.filling_at)
        if filled_at and filled_at < cutoff:
            row.filling_at = None


def _fill_new(session: Session, settings: Settings, prefer_yyds_id: str | None) -> bool:
    yyds_accounts = list(
        session.scalars(select(YydsAccount).where(YydsAccount.enabled.is_(True)).order_by(YydsAccount.sort_order)).all()
    )
    picked_account_view = select_fill_account(_account_views(yyds_accounts), prefer_id=prefer_yyds_id)
    if picked_account_view is None:
        return False
    yyds_account = next(item for item in yyds_accounts if str(item.id) == picked_account_view.id)

    aliyun_accounts = {item.id: item for item in session.scalars(select(AliyunAccount)).all()}
    now = _now()
    domain_rows = list(session.scalars(select(Domain)).all())
    views: list[DomainView] = []
    for row in domain_rows:
        aliyun = aliyun_accounts.get(row.aliyun_account_id) if row.aliyun_account_id else None
        ready = True
        if aliyun is None or not aliyun.enabled:
            ready = False
        elif _aware(aliyun.throttle_until) and _aware(aliyun.throttle_until) > now:
            ready = False
        views.append(
            DomainView(
                id=str(row.id),
                name=row.name,
                status=row.status,
                registration_at=_aware(row.registration_at),
                created_at=_aware(row.created_at) or now,
                filling=row.filling_at is not None,
                yyds_account_id=str(row.yyds_account_id) if row.yyds_account_id else None,
                aliyun_ready=ready,
            )
        )
    picked_domain_view = select_unused_domain(views)
    if picked_domain_view is None:
        return False
    domain = next(item for item in domain_rows if str(item.id) == picked_domain_view.id)
    domain.filling_at = now
    session.commit()
    try:
        _bind_and_verify(session, settings, yyds_account, domain)
        return True
    except Exception as exc:  # noqa: BLE001
        session.refresh(domain)
        domain.filling_at = None
        if domain.status != STATUS_USED:
            domain.status = STATUS_ERROR
            domain.error_reason = str(exc)[:500]
        add_event(
            session,
            level="error",
            code="fill_failed",
            message=f"补位 {domain.name} 失败: {exc}",
            domain_name=domain.name,
            yyds_account_id=yyds_account.id,
            aliyun_account_id=domain.aliyun_account_id,
        )
        session.commit()
        return True


def _repair_bound_errors(session: Session, settings: Settings) -> bool:
    row = session.scalar(
        select(Domain)
        .where(Domain.status == STATUS_ERROR, Domain.yyds_domain_id.is_not(None), Domain.yyds_account_id.is_not(None))
        .order_by(Domain.updated_at)
    )
    if row is None:
        return False
    updated_at = _aware(row.updated_at)
    if updated_at and updated_at > _now() - timedelta(minutes=2):
        return False
    account = session.get(YydsAccount, row.yyds_account_id)
    if account is None or not account.enabled:
        return False
    row.filling_at = _now()
    session.commit()
    try:
        _write_dns_and_verify(session, settings, account, row, row.yyds_domain_id or "")
        row.status = STATUS_USED
        row.error_reason = None
        row.filling_at = None
        add_event(
            session,
            level="info",
            code="fill_repaired",
            message=f"域名 {row.name} 已补写解析并验证成功",
            domain_name=row.name,
            yyds_account_id=account.id,
        )
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        row.filling_at = None
        row.error_reason = str(exc)[:500]
        add_event(
            session,
            level="error",
            code="fill_repair_failed",
            message=f"域名 {row.name} 补写解析失败: {exc}",
            domain_name=row.name,
            yyds_account_id=account.id,
        )
        session.commit()
        return True


def _bind_and_verify(session: Session, settings: Settings, account: YydsAccount, domain: Domain) -> None:
    add_event(
        session,
        level="info",
        code="fill_start",
        message=f"开始把 {domain.name} 加进 yyds 账号 {account.name}",
        domain_name=domain.name,
        yyds_account_id=account.id,
        aliyun_account_id=domain.aliyun_account_id,
    )
    session.commit()

    client = yyds_client(settings, account)
    newly_added = False
    try:
        client.ensure_session()
        existing = {item.domain.lower(): item for item in client.list_domains()}
        current = existing.get(domain.name)
        if current is None:
            try:
                created = client.add_domain(domain.name, enable_wildcard=True)
            except YydsError as exc:
                persist_yyds_session(settings, account, client)
                if exc.status_code == 409:
                    raise RuntimeError("yyds 返回 409，域名可能仍绑在别人或删除未完成") from exc
                raise
            domain_id = str(created.get("id") or created.get("domainId") or "")
            if not domain_id:
                listed = {item.domain.lower(): item for item in client.list_domains()}
                match = listed.get(domain.name)
                domain_id = match.id if match else ""
            if not domain_id:
                raise RuntimeError("yyds 加域成功但没有返回域名 id")
            newly_added = True
        else:
            domain_id = current.id
        persist_yyds_session(settings, account, client)
        domain.yyds_account_id = account.id
        domain.yyds_domain_id = domain_id
        session.commit()
        client.set_private(domain_id)
        client.ensure_wildcard_rule(domain_id)
        _write_dns_and_verify(session, settings, account, domain, domain_id, yyds=client)
        domain.status = STATUS_USED
        domain.error_reason = None
        domain.filling_at = None
        domain.last_seen_on_yyds_at = _now()
        if newly_added:
            account.used_wildcard = (account.used_wildcard or 0) + 1
            account.used_domains = (account.used_domains or 0) + 1
        persist_yyds_session(settings, account, client)
        add_event(
            session,
            level="info",
            code="fill_success",
            message=f"域名 {domain.name} 已加进 {account.name} 并完成解析验证",
            domain_name=domain.name,
            yyds_account_id=account.id,
            aliyun_account_id=domain.aliyun_account_id,
        )
        session.commit()
    finally:
        client.close()


def _write_dns_and_verify(
    session: Session,
    settings: Settings,
    account: YydsAccount,
    domain: Domain,
    domain_id: str,
    yyds=None,
) -> None:
    close_client = False
    if yyds is None:
        yyds = yyds_client(settings, account)
        yyds.ensure_session()
        close_client = True
    try:
        guide = yyds.dns_guide(domain_id)
        wanted = parse_guide_records(guide, domain.name)
        if not any(item.type == "MX" for item in wanted) or not any(item.type == "TXT" for item in wanted):
            raise RuntimeError("yyds dns-guide 缺少 TXT 或 MX，拒绝写死记录")
        if domain.aliyun_account_id is None:
            raise RuntimeError("域名没有归属的阿里云账号，无法写解析")
        aliyun_account = session.get(AliyunAccount, domain.aliyun_account_id)
        if aliyun_account is None:
            raise RuntimeError("域名归属的阿里云账号已删除")
        dns = aliyun_client(settings, aliyun_account)
        result = dns.apply_guide(domain.name, wanted)
        add_event(
            session,
            level="info",
            code="dns_written",
            message=f"已按 dns-guide 写入 {domain.name} 解析，删除 {result['deleted']} 条冲突记录，新增 {result['added']} 条",
            domain_name=domain.name,
            aliyun_account_id=aliyun_account.id,
            yyds_account_id=account.id,
        )
        session.commit()
        last_reason = "dns_propagating"
        attempts = max(1, settings.verify_attempts)
        for index in range(attempts):
            try:
                payload = yyds.verify_domain(domain_id)
            except YydsError as exc:
                last_reason = str(exc)
                time.sleep(settings.verify_retry_seconds)
                continue
            ready, reason = yyds.verify_ready(payload)
            last_reason = reason
            if ready:
                persist_yyds_session(settings, account, yyds)
                return
            if reason not in {"dns_propagating", "txt_missing", "mx_missing", "wildcard_mx_missing", "unknown"}:
                raise RuntimeError(f"yyds 验证失败: {reason}")
            if index < attempts - 1:
                time.sleep(settings.verify_retry_seconds)
        raise RuntimeError(f"解析验证超时: {last_reason}")
    finally:
        if close_client:
            persist_yyds_session(settings, account, yyds)
            yyds.close()
