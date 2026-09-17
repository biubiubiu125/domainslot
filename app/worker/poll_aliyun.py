from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.aliyun.client import AliyunDomain, AliyunError, domain_ready_for_fill
from app.config import Settings
from app.db.models import AliyunAccount, Domain
from app.domainutil import display_domain, normalize_domain
from app.worker.clients import aliyun_client
from app.worker.events import add_event, redact
from app.worker.logic import (
    ALIYUN_EMPTY_LIST_WARNING,
    STATUS_ERROR,
    STATUS_UNUSED,
    STATUS_USED,
    aliyun_account_due,
    aliyun_throttle_backoff,
    assign_discovered_status,
    discovered_from_first_snapshot,
)

DESCRIBE_COOLDOWN_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def poll_aliyun_accounts(session: Session, settings: Settings, force: bool = False) -> int:
    accounts = list(session.scalars(select(AliyunAccount).where(AliyunAccount.enabled.is_(True))).all())
    discovered = 0
    now = _now()
    for index, account in enumerate(accounts):
        if not aliyun_account_due(_aware(account.last_poll_at), now, settings.aliyun_poll_seconds, index, force=force):
            continue
        discovered += poll_one_aliyun_account(session, settings, account)
        session.commit()
    return discovered


def _needs_live_registrar(
    row: Domain | None,
    item: AliyunDomain,
    last_success_at: datetime | None = None,
    now: datetime | None = None,
    cooldown_seconds: int | None = None,
) -> bool:
    if bool(getattr(item, "client_hold", False)):
        return False
    if row is None:
        return True
    checked = _aware(getattr(row, "last_registrar_checked_at", None))
    if checked is None:
        return True
    success = _aware(last_success_at)
    if success is None:
        return False
    if checked > success:
        return False
    if row.status == STATUS_USED:
        current = _aware(now) or _now()
        cooldown = DESCRIBE_COOLDOWN_SECONDS if cooldown_seconds is None else int(cooldown_seconds)
        if (current - checked).total_seconds() < cooldown:
            return False
    return True


def _merge_live_registrar(item: AliyunDomain, live: AliyunDomain) -> AliyunDomain:
    return AliyunDomain(
        name=item.name,
        registration_at=item.registration_at or live.registration_at,
        domain_status=live.domain_status,
        audit_status=live.audit_status,
        nameservers=list(live.nameservers or []),
        client_hold=bool(getattr(live, "client_hold", False)),
    )


def _persist_live_registrar(row: Domain, item: AliyunDomain, live: AliyunDomain | None, nameservers: list[str]) -> None:
    if live is not None:
        if live.domain_status:
            row.domain_status = live.domain_status
        if live.audit_status:
            row.audit_status = live.audit_status
        if live.nameservers:
            row.nameservers = ",".join(list(live.nameservers))
        row.client_hold = bool(getattr(live, "client_hold", False))
        return
    row.client_hold = bool(getattr(item, "client_hold", False))
    if getattr(row, "last_registrar_checked_at", None) is not None:
        return
    if item.domain_status:
        row.domain_status = item.domain_status
    if item.audit_status:
        row.audit_status = item.audit_status
    if nameservers:
        row.nameservers = ",".join(nameservers)


def _live_registrar_item(
    client,
    item: AliyunDomain,
    row: Domain | None,
    last_success_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[AliyunDomain, AliyunDomain | None]:
    if not _needs_live_registrar(row, item, last_success_at, now=now):
        return item, None
    describe = getattr(client, "describe_registrar_domain", None)
    if not callable(describe):
        return item, None
    try:
        live = describe(item.name)
    except AliyunError:
        raise
    except Exception as exc:
        raise AliyunError(str(exc), code="DESCRIBE_FAILED") from exc
    if not isinstance(live, AliyunDomain):
        raise AliyunError("注册商详情格式无效", code="DESCRIBE_FAILED")
    return _merge_live_registrar(item, live), live


def _live_only_ready(live: AliyunDomain, row: Domain | None) -> tuple[bool, str | None]:
    need_audit = row is None or row.status == STATUS_UNUSED or row.yyds_account_id is None
    return domain_ready_for_fill(
        AliyunDomain(
            name=live.name,
            domain_status=live.domain_status,
            audit_status=live.audit_status,
            nameservers=list(live.nameservers or []),
            client_hold=bool(getattr(live, "client_hold", False)),
        ),
        require_nameservers=False,
        require_audit=need_audit,
    )


def _mark_unlisted_inventory(session: Session, account: AliyunAccount, listed_names: set[str]) -> None:
    rows = list(session.scalars(select(Domain).where(Domain.aliyun_account_id == account.id)).all())
    for row in rows:
        if row.name in listed_names:
            continue
        if row.yyds_account_id is not None:
            continue
        already = row.status == STATUS_ERROR and row.error_reason == "阿里云列表中已不存在"
        row.status = STATUS_ERROR
        row.error_reason = "阿里云列表中已不存在"
        row.filling_at = None
        if already:
            continue
        add_event(
            session,
            level="warning",
            code="aliyun_missing",
            message=f"{row.name} 已不在阿里云账号 {account.name} 的列表中",
            domain_name=row.name,
            aliyun_account_id=account.id,
        )


def poll_one_aliyun_account(session: Session, settings: Settings, account: AliyunAccount) -> int:
    now = _now()
    account.last_poll_at = now
    if _aware(account.throttle_until) and _aware(account.throttle_until) > now:
        return 0
    try:
        client = aliyun_client(settings, account)
        items = client.list_domains()
    except AliyunError as exc:
        account.last_error = redact(str(exc))[:500]
        if exc.throttled:
            delay, nxt = aliyun_throttle_backoff(account.throttle_backoff_seconds)
            account.throttle_until = now + timedelta(seconds=delay)
            account.throttle_backoff_seconds = nxt
            add_event(
                session,
                level="error",
                code="aliyun_throttled",
                message=f"阿里云账号 {account.name} 被限流，{delay} 秒后重试",
                aliyun_account_id=account.id,
            )
        else:
            add_event(
                session,
                level="error",
                code="aliyun_poll_failed",
                message=f"阿里云账号 {account.name} 拉列表失败: {redact(str(exc))}",
                aliyun_account_id=account.id,
            )
        return 0
    except Exception as exc:  # noqa: BLE001
        account.last_error = redact(str(exc))[:500]
        add_event(
            session,
            level="error",
            code="aliyun_poll_failed",
            message=f"阿里云账号 {account.name} 拉列表失败: {redact(str(exc))}",
            aliyun_account_id=account.id,
        )
        return 0

    if not items:
        existing = list(session.scalars(select(Domain).where(Domain.aliyun_account_id == account.id)).all())
        if existing:
            already = account.last_error == ALIYUN_EMPTY_LIST_WARNING
            account.last_error = ALIYUN_EMPTY_LIST_WARNING
            if not already:
                add_event(
                    session,
                    level="warning",
                    code="aliyun_list_unreliable",
                    message=f"阿里云账号 {account.name} 列表为空，已有库存未改",
                    aliyun_account_id=account.id,
                )
            return 0
        account.last_error = None
        account.last_success_at = now
        account.throttle_until = None
        account.throttle_backoff_seconds = 120
        return 0

    first_sync_done = account.first_synced_at is not None
    if account.first_sync_names is None and not first_sync_done:
        cohort: list[str] = []
        for item in items:
            try:
                cohort.append(normalize_domain(item.name))
            except ValueError:
                continue
        account.first_sync_names = json.dumps(sorted(set(cohort)), ensure_ascii=False)
    discovered = 0
    for item in items:
        try:
            name = normalize_domain(item.name)
        except ValueError:
            continue
        row = session.scalar(select(Domain).where(Domain.name == name))
        stored_ns: list[str] = []
        if row is not None and row.nameservers:
            stored_ns = [part.strip() for part in row.nameservers.split(",") if part.strip()]
        try:
            live_item, live = _live_registrar_item(client, item, row, account.last_success_at, now=now)
        except AliyunError as exc:
            if not exc.throttled:
                add_event(
                    session,
                    level="error",
                    code="aliyun_describe_failed",
                    message=f"阿里云账号 {account.name} 读取 {name} 注册商状态失败: {redact(str(exc))}",
                    domain_name=name,
                    aliyun_account_id=account.id,
                )
                continue
            account.last_error = redact(str(exc))[:500]
            delay, nxt = aliyun_throttle_backoff(account.throttle_backoff_seconds)
            account.throttle_until = now + timedelta(seconds=delay)
            account.throttle_backoff_seconds = nxt
            add_event(
                session,
                level="error",
                code="aliyun_throttled",
                message=f"阿里云账号 {account.name} 被限流，{delay} 秒后重试",
                aliyun_account_id=account.id,
            )
            return discovered
        nameservers = list(live_item.nameservers or []) or stored_ns
        check = AliyunDomain(
            name=live_item.name,
            domain_status=live_item.domain_status,
            audit_status=live_item.audit_status,
            nameservers=nameservers,
            registration_at=live_item.registration_at or item.registration_at,
            client_hold=bool(getattr(live_item, "client_hold", False)),
        )
        ready, reason = domain_ready_for_fill(check, require_nameservers=False)
        live_ready, live_reason = (True, None)
        if live is not None:
            live_ready, live_reason = _live_only_ready(live, row)
        if live is not None and not live_ready:
            ready = False
            reason = live_reason
        if row is None:
            first_snapshot = discovered_from_first_snapshot(
                name,
                first_sync_done=first_sync_done,
                first_sync_names=account.first_sync_names,
            )
            status = assign_discovered_status(not first_snapshot)
            if not ready:
                status = STATUS_ERROR
            row = Domain(
                name=name,
                display_name=display_domain(name),
                aliyun_account_id=account.id,
                status=status,
                from_first_snapshot=first_snapshot,
                registration_at=item.registration_at.replace(tzinfo=timezone.utc) if item.registration_at and item.registration_at.tzinfo is None else item.registration_at,
                error_reason=reason if status == STATUS_ERROR else None,
                last_seen_on_aliyun_at=now,
                last_registrar_checked_at=now if live is not None else None,
            )
            _persist_live_registrar(row, item, live, nameservers)
            session.add(row)
            discovered += 1
            add_event(
                session,
                level="info",
                code="domain_discovered",
                message=f"发现域名 {name}，状态 {status}",
                domain_name=name,
                aliyun_account_id=account.id,
            )
        else:
            row.aliyun_account_id = account.id
            row.display_name = row.display_name or display_domain(name)
            if item.registration_at and not row.registration_at:
                dt = item.registration_at
                row.registration_at = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            _persist_live_registrar(row, item, live, nameservers)
            if live is not None:
                row.last_registrar_checked_at = now
            row.last_seen_on_aliyun_at = now
            punish_not_ready = (
                row.status == STATUS_UNUSED
                or row.yyds_account_id is None
                or bool(getattr(check, "client_hold", False))
                or (live is not None and not live_ready)
            )
            if not ready and punish_not_ready:
                row.status = STATUS_ERROR
                row.error_reason = reason
            elif ready and row.status == STATUS_ERROR and row.yyds_account_id is None:
                if row.from_first_snapshot or row.last_seen_on_yyds_at is not None:
                    row.status = STATUS_USED
                else:
                    row.status = STATUS_UNUSED
                row.error_reason = None

    listed_names: set[str] = set()
    for item in items:
        try:
            listed_names.add(normalize_domain(item.name))
        except ValueError:
            continue
    _mark_unlisted_inventory(session, account, listed_names)
    account.last_success_at = now
    account.last_error = None
    account.throttle_until = None
    account.throttle_backoff_seconds = 120
    if account.first_synced_at is None:
        account.first_synced_at = now
        add_event(
            session,
            level="info",
            code="aliyun_first_sync",
            message=f"阿里云账号 {account.name} 首次对账完成，未改解析",
            aliyun_account_id=account.id,
        )
    return discovered
