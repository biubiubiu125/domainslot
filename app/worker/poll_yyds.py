from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Domain, EventLog, YydsAccount, YydsDomainSnapshot
from app.domainutil import normalize_domain
from app.worker.clients import persist_yyds_session, yyds_client
from app.worker.events import add_event, redact
from app.worker.locks import acquire_yyds_session_lock, release_yyds_session_lock
from app.worker.logic import STATUS_UNUSED, STATUS_USED, apply_account_throttle, diff_domain_sets, occupancy_used, status_after_yyds_removed, yyds_snapshot_unreliable
from app.yyds.client import YydsError, extract_quota


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _add_yyds_list_warning(session: Session, account: YydsAccount, code: str, message: str) -> None:
    last = session.scalars(
        select(EventLog).where(EventLog.yyds_account_id == account.id).order_by(EventLog.created_at.desc())
    ).first()
    if last is not None and last.code == code and last.message == message:
        return
    add_event(
        session,
        level="warning",
        code=code,
        message=message,
        yyds_account_id=account.id,
    )


def _yyds_list_incomplete(exc: YydsError) -> bool:
    if getattr(exc, "code", None) == "LIST_INCOMPLETE":
        return True
    return "列表不完整" in str(exc)


def poll_yyds_accounts(session: Session, settings: Settings) -> list[str]:
    shrink_ids: list[str] = []
    accounts = list(
        session.scalars(select(YydsAccount).where(YydsAccount.enabled.is_(True)).order_by(YydsAccount.sort_order, YydsAccount.created_at)).all()
    )
    for account in accounts:
        if poll_one_yyds_account(session, settings, account):
            shrink_ids.append(str(account.id))
        session.commit()
    return shrink_ids


def poll_one_yyds_account(session: Session, settings: Settings, account: YydsAccount) -> bool:
    now = _now()
    account.last_poll_at = now
    until = _aware(getattr(account, "throttle_until", None))
    if until is not None and until > now:
        return False
    lock = acquire_yyds_session_lock(session, account.id)
    client = None
    try:
        client = yyds_client(settings, account)
        client.ensure_session()
        me = client.get_me()
        try:
            quota_payload = client.get_quota()
        except YydsError:
            quota_payload = {}
        domains = client.list_domains()
        rules_ok = False
        try:
            rules = client.list_wildcard_rules()
            rules_ok = True
        except YydsError:
            rules = []
        persist_yyds_session(settings, account, client)
        quota = extract_quota(me, quota_payload, rules, [item.__dict__ for item in domains])
        account.login_error = None
        account.throttle_until = None
        account.throttle_backoff_seconds = 120
        if quota.get("plan_name"):
            account.plan_name = quota.get("plan_name")
        if quota.get("max_wildcard") is not None:
            account.max_wildcard = quota.get("max_wildcard")
        if quota.get("max_domains") is not None:
            account.max_domains = quota.get("max_domains")
        account.used_wildcard = occupancy_used(
            listed=len(rules) if rules_ok else None,
            quota_used=quota.get("used_wildcard_from_quota"),
            max_rules=account.max_wildcard,
        )
        account.used_domains = occupancy_used(
            listed=len(domains),
            quota_used=quota.get("used_domains_from_quota"),
            max_rules=account.max_domains,
        )
        account.last_success_at = now

        current_names: list[str] = []
        current_map: dict[str, object] = {}
        for item in domains:
            try:
                name = normalize_domain(item.domain)
            except ValueError:
                continue
            current_names.append(name)
            current_map[name] = item

        snapshot = session.scalar(select(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id == account.id))
        previous: list[str] = []
        if snapshot:
            try:
                loaded = json.loads(snapshot.names_json or "[]")
                previous = [str(item) for item in loaded]
            except json.JSONDecodeError:
                previous = []

        quota_domains = quota.get("used_domains_from_quota")
        listed = len(current_names)
        current_set = set(current_names)
        missing_previous = any(name not in current_set for name in previous)
        if yyds_snapshot_unreliable(
            listed,
            quota_domains,
            len(previous),
            missing_previous=missing_previous,
        ):
            account.used_domains = -1
            short_vs_quota = (
                quota_domains is not None
                and quota_domains >= 0
                and listed < int(quota_domains)
            )
            if short_vs_quota:
                warning_code = "yyds_list_short"
                warning_message = (
                    f"yyds 账号 {account.name} 域名列表比配额占用短，拒绝按截断结果对账，本轮不按删减补位"
                )
            elif listed == 0:
                warning_code = "yyds_list_unreliable"
                warning_message = (
                    f"yyds 账号 {account.name} 域名列表为空且配额占用未知，拒绝按空列表对账，本轮不按删减补位"
                )
            else:
                warning_code = "yyds_list_unreliable"
                warning_message = (
                    f"yyds 账号 {account.name} 域名列表相对上次变少或对不上且配额占用未知，拒绝按截断结果对账，本轮不按删减补位"
                )
            _add_yyds_list_warning(session, account, warning_code, warning_message)
            session.commit()
            return False

        diff = diff_domain_sets(previous, current_names)
        shrunk = False

        for name in sorted(diff.added):
            item = current_map.get(name)
            row = session.scalar(select(Domain).where(Domain.name == name))
            if row is None:
                add_event(
                    session,
                    level="info",
                    code="yyds_only_domain",
                    message=f"yyds 账号 {account.name} 有域名 {name}，本地库存没有对应阿里云记录",
                    domain_name=name,
                    yyds_account_id=account.id,
                )
                continue
            row.yyds_account_id = account.id
            row.yyds_domain_id = getattr(item, "id", None)
            row.last_seen_on_yyds_at = now
            row.filling_at = None
            if row.status != "error":
                row.status = STATUS_USED
                row.error_reason = None
            add_event(
                session,
                level="info",
                code="yyds_bound",
                message=f"域名 {name} 已出现在 yyds 账号 {account.name}",
                domain_name=name,
                yyds_account_id=account.id,
            )

        for name in sorted(diff.removed):
            shrunk = True
            row = session.scalar(select(Domain).where(Domain.name == name))
            if row is None:
                continue
            if row.yyds_account_id == account.id:
                next_status = status_after_yyds_removed(row.status)
                row.status = next_status
                row.yyds_account_id = None
                row.yyds_domain_id = None
                row.filling_at = None
                keep = "未使用" if next_status == STATUS_UNUSED else "已使用"
                add_event(
                    session,
                    level="info",
                    code="yyds_removed",
                    message=f"监测到 yyds 账号 {account.name} 少了域名 {name}，本地保持{keep}，准备补另一条",
                    domain_name=name,
                    yyds_account_id=account.id,
                )

        for name, item in current_map.items():
            row = session.scalar(select(Domain).where(Domain.name == name))
            if row is None:
                continue
            row.yyds_account_id = account.id
            row.yyds_domain_id = getattr(item, "id", None)
            row.last_seen_on_yyds_at = now

        names_json = json.dumps(sorted(set(current_names)), ensure_ascii=False)
        if snapshot is None:
            session.add(YydsDomainSnapshot(yyds_account_id=account.id, names_json=names_json))
        else:
            snapshot.names_json = names_json

        if account.first_synced_at is None:
            account.first_synced_at = now
        session.commit()
        return shrunk
    except YydsError as exc:
        if exc.status_code == 429:
            delay = apply_account_throttle(account, now, retry_after=getattr(exc, "retry_after_seconds", None))
            add_event(
                session,
                level="error",
                code="yyds_throttled",
                message=f"yyds 账号 {account.name} 被限流，{delay} 秒后重试",
                yyds_account_id=account.id,
            )
            session.commit()
            return False
        if _yyds_list_incomplete(exc):
            account.login_error = None
            account.used_domains = -1
            _add_yyds_list_warning(
                session,
                account,
                "yyds_list_unreliable",
                f"yyds 账号 {account.name} 域名列表不完整，拒绝按截断结果对账，本轮不按删减补位",
            )
            session.commit()
            return False
        account.login_error = redact(str(exc))[:500]
        add_event(
            session,
            level="error",
            code="yyds_login_failed",
            message=f"yyds 账号 {account.name} 登录或拉列表失败: {redact(str(exc))}",
            yyds_account_id=account.id,
        )
        session.commit()
        return False
    except Exception as exc:  # noqa: BLE001
        account.login_error = redact(str(exc))[:500]
        add_event(
            session,
            level="error",
            code="yyds_login_failed",
            message=f"yyds 账号 {account.name} 登录或拉列表失败: {redact(str(exc))}",
            yyds_account_id=account.id,
        )
        session.commit()
        return False
    finally:
        if client is not None:
            client.close()
        release_yyds_session_lock(lock)
