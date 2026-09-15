from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.aliyun.client import AliyunError, domain_ready_for_fill
from app.config import Settings
from app.db.models import AliyunAccount, Domain
from app.domainutil import display_domain, normalize_domain
from app.worker.clients import aliyun_client
from app.worker.events import add_event
from app.worker.logic import STATUS_ERROR, assign_discovered_status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def poll_aliyun_accounts(session: Session, settings: Settings) -> int:
    accounts = list(session.scalars(select(AliyunAccount).where(AliyunAccount.enabled.is_(True))).all())
    discovered = 0
    for account in accounts:
        discovered += poll_one_aliyun_account(session, settings, account)
        session.commit()
    return discovered


def poll_one_aliyun_account(session: Session, settings: Settings, account: AliyunAccount) -> int:
    now = _now()
    account.last_poll_at = now
    if _aware(account.throttle_until) and _aware(account.throttle_until) > now:
        return 0
    try:
        client = aliyun_client(settings, account)
        items = client.list_domains()
    except AliyunError as exc:
        account.last_error = str(exc)[:500]
        if exc.throttled:
            delay = 120
            if account.throttle_until and account.throttle_until > now:
                delay = 120
            account.throttle_until = now + timedelta(seconds=delay)
            add_event(
                session,
                level="error",
                code="aliyun_throttled",
                message=f"阿里云账号 {account.name} 被限流，稍后重试",
                aliyun_account_id=account.id,
            )
        else:
            add_event(
                session,
                level="error",
                code="aliyun_poll_failed",
                message=f"阿里云账号 {account.name} 拉列表失败: {exc}",
                aliyun_account_id=account.id,
            )
        return 0
    except Exception as exc:  # noqa: BLE001
        account.last_error = str(exc)[:500]
        add_event(
            session,
            level="error",
            code="aliyun_poll_failed",
            message=f"阿里云账号 {account.name} 拉列表失败: {exc}",
            aliyun_account_id=account.id,
        )
        return 0

    first_sync_done = account.first_synced_at is not None
    discovered = 0
    for item in items:
        try:
            name = normalize_domain(item.name)
        except ValueError:
            continue
        row = session.scalar(select(Domain).where(Domain.name == name))
        ready, reason = domain_ready_for_fill(item)
        if row is None:
            status = assign_discovered_status(first_sync_done)
            if first_sync_done and not ready:
                status = STATUS_ERROR
            row = Domain(
                name=name,
                display_name=display_domain(name),
                aliyun_account_id=account.id,
                status=status,
                from_first_snapshot=not first_sync_done,
                registration_at=item.registration_at.replace(tzinfo=timezone.utc) if item.registration_at and item.registration_at.tzinfo is None else item.registration_at,
                error_reason=reason if status == STATUS_ERROR else None,
                domain_status=item.domain_status,
                nameservers=",".join(item.nameservers) if item.nameservers else None,
                last_seen_on_aliyun_at=now,
            )
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
            row.domain_status = item.domain_status
            row.nameservers = ",".join(item.nameservers) if item.nameservers else row.nameservers
            row.last_seen_on_aliyun_at = now
            if row.status == STATUS_UNUSED and not ready:
                row.status = STATUS_ERROR
                row.error_reason = reason

    account.last_success_at = now
    account.last_error = None
    account.throttle_until = None
    if account.first_synced_at is None:
        account.first_synced_at = now
        add_event(
            session,
            level="info",
            code="aliyun_first_sync",
            message=f"阿里云账号 {account.name} 首次对账完成，已有域名全部记为已使用，未改解析",
            aliyun_account_id=account.id,
        )
    return discovered
