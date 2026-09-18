from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.aliyun.client import AliyunDomain, AliyunError, domain_ready_for_fill
from app.aliyun.records import parse_guide_records, required_guide_missing
from app.config import Settings
from app.db.models import AliyunAccount, Domain, YydsAccount, YydsDomainSnapshot
from app.domainutil import normalize_domain
from app.worker.clients import aliyun_client, persist_yyds_session, yyds_client
from app.worker.events import add_event, redact
from app.worker.locks import acquire_fill_lock, acquire_yyds_session_lock, release_fill_lock, release_yyds_session_lock
from app.worker.logic import (
    STATUS_ERROR,
    STATUS_UNUSED,
    STATUS_USED,
    AccountView,
    DomainView,
    aliyun_throttle_backoff,
    first_snapshot_names,
    name_in_snapshot_json,
    occupancy_used,
    increment_known_occupancy,
    stored_occupancy,
    apply_account_throttle,
    has_domain_slot,
    has_wildcard_slot,
    select_fill_account,
    select_unused_domain,
    verify_attempts_this_cycle,
    verify_repair_cooldown,
    yyds_lists_unreliable_for_add,
)
from app.yyds.client import YydsError, extract_quota, find_listed_domain

RETRY_RESULTS = {"dns_propagating", "txt_missing", "mx_missing", "wildcard_mx_missing"}
_WILDCARD_ENABLE_RETRY_CODES = {"wildcard_rule_mx_not_ready", "wildcard_dns_refresh_pending"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _wildcard_enable_retryable(exc: YydsError) -> bool:
    parts = [str(exc), str(exc.code or "")]
    payload = exc.payload
    if isinstance(payload, dict):
        parts.extend(str(payload.get(key) or "") for key in ("errorCode", "error", "message", "code"))
    blob = " ".join(parts).lower()
    return any(code in blob for code in _WILDCARD_ENABLE_RETRY_CODES)


def fill_available(session: Session, settings: Settings, prefer_yyds_id: str | None = None) -> bool:
    lock_conn = acquire_fill_lock(session)
    try:
        immediate_ids = _clear_stale_filling(session)
        if _repair_bound_errors(session, settings, immediate_ids=immediate_ids):
            return True
        _mark_stored_unusable_unused(session)
        return _fill_new(session, settings, prefer_yyds_id)
    finally:
        release_fill_lock(lock_conn)


def _account_views(session: Session) -> list[AccountView]:
    items = list(session.scalars(select(YydsAccount)).all())
    now = _now()
    views: list[AccountView] = []
    for item in items:
        views.append(
            AccountView(
                id=str(item.id),
                sort_order=item.sort_order,
                receive_enabled=item.receive_enabled,
                enabled=item.enabled,
                login_ok=not bool(item.login_error) and not _account_throttled(item, now),
                max_wildcard=item.max_wildcard,
                used_wildcard=stored_occupancy(item.used_wildcard),
                max_domains=item.max_domains,
                used_domains=stored_occupancy(item.used_domains),
            )
        )
    return views


def _stored_nameservers(item: Domain) -> list[str]:
    if not item.nameservers:
        return []
    return [part.strip() for part in item.nameservers.split(",") if part.strip()]


def _stored_aliyun_ready(item: Domain) -> bool:
    ready, _reason = domain_ready_for_fill(
        AliyunDomain(
            name=item.name,
            domain_status=item.domain_status,
            audit_status=item.audit_status,
            nameservers=_stored_nameservers(item),
            client_hold=bool(getattr(item, "client_hold", False)),
        ),
        require_nameservers=False,
        require_audit=False,
    )
    return ready


def _stored_unusable_reason(item: Domain) -> str | None:
    _ready, reason = domain_ready_for_fill(
        AliyunDomain(
            name=item.name,
            domain_status=item.domain_status,
            audit_status=item.audit_status,
            nameservers=_stored_nameservers(item),
            client_hold=bool(getattr(item, "client_hold", False)),
        ),
        require_nameservers=False,
        require_audit=False,
    )
    text = reason or ""
    if any(token in text for token in ("赎回", "NS 不是阿里云", "ClientHold", "状态异常")):
        return text
    return None


def _mark_stored_unusable_unused(session: Session) -> None:
    rows = list(
        session.scalars(
            select(Domain).where(
                Domain.status == STATUS_UNUSED,
                Domain.yyds_account_id.is_(None),
                Domain.filling_at.is_(None),
            )
        ).all()
    )
    changed = False
    for row in rows:
        reason = _stored_unusable_reason(row)
        if not reason:
            continue
        _mark_unusable(session, row, reason)
        changed = True
    if changed:
        session.commit()


def _rewrite_dns_on_repair(error_reason: str | None) -> bool:
    return "dns_propagating" not in (error_reason or "")


def _domain_views(session: Session) -> list[DomainView]:
    now = _now()
    throttled_ids = {
        item.id
        for item in session.scalars(select(AliyunAccount)).all()
        if _throttle_active(item, now)
    }
    items = list(session.scalars(select(Domain)).all())
    return [
        DomainView(
            id=str(item.id),
            name=item.name,
            status=item.status,
            yyds_account_id=str(item.yyds_account_id) if item.yyds_account_id else None,
            filling=item.filling_at is not None,
            registration_at=_aware(item.registration_at),
            created_at=_aware(item.created_at) or _now(),
            aliyun_ready=_stored_aliyun_ready(item) and item.aliyun_account_id not in throttled_ids,
            yyds_domain_id=item.yyds_domain_id or None,
        )
        for item in items
    ]


def _occupying_yyds_account(session: Session, name: str, except_account_id: UUID | None = None) -> UUID | None:
    row = session.scalar(select(Domain).where(Domain.name == name))
    if row and row.yyds_account_id and (except_account_id is None or row.yyds_account_id != except_account_id):
        return row.yyds_account_id
    for snap in session.scalars(select(YydsDomainSnapshot)).all():
        if except_account_id is not None and snap.yyds_account_id == except_account_id:
            continue
        if name_in_snapshot_json(snap.names_json, name):
            return snap.yyds_account_id
    return None


def _sync_yyds_usage(account: YydsAccount, client, *, fallback_add: bool) -> dict[str, Any]:
    try:
        me = client.get_me()
    except YydsError:
        me = {}
    try:
        quota_payload = client.get_quota()
    except YydsError:
        quota_payload = {}
    rules_ok = False
    rules: list = []
    try:
        rules = client.list_wildcard_rules()
        rules_ok = True
    except YydsError:
        rules = []
    domains_ok = False
    domains: list = []
    try:
        domains = client.list_domains()
        domains_ok = True
    except YydsError:
        domains = []
    quota = extract_quota(me, quota_payload, rules, [item.__dict__ for item in domains])
    if quota.get("max_wildcard") is not None:
        account.max_wildcard = quota.get("max_wildcard")
    if quota.get("max_domains") is not None:
        account.max_domains = quota.get("max_domains")
    if quota.get("plan_name"):
        account.plan_name = quota.get("plan_name")
    account.used_wildcard = occupancy_used(
        listed=len(rules) if rules_ok else None,
        quota_used=quota.get("used_wildcard_from_quota"),
        max_rules=account.max_wildcard,
    )
    account.used_domains = occupancy_used(
        listed=len(domains) if domains_ok else None,
        quota_used=quota.get("used_domains_from_quota"),
        max_rules=account.max_domains,
    )
    if fallback_add and not rules_ok:
        account.used_wildcard = increment_known_occupancy(account.used_wildcard)
    if fallback_add and not domains_ok:
        account.used_domains = increment_known_occupancy(account.used_domains)
    return {
        "domains_ok": domains_ok,
        "domains": domains,
        "listed_domains": len(domains) if domains_ok else None,
        "quota_domains": quota.get("used_domains_from_quota"),
        "rules_ok": rules_ok,
        "listed_rules": len(rules) if rules_ok else None,
        "quota_wildcard": quota.get("used_wildcard_from_quota"),
    }


def _refresh_live_registrar(
    session: Session,
    settings: Settings,
    domain: Domain,
    *,
    require_audit: bool = False,
) -> bool:
    aliyun_account = session.get(AliyunAccount, domain.aliyun_account_id) if domain.aliyun_account_id else None
    if aliyun_account is None or not aliyun_account.enabled:
        _mark_unusable(session, domain, "找不到对应的阿里云账号")
        session.commit()
        return False
    if _throttle_active(aliyun_account):
        domain.filling_at = None
        domain.updated_at = _now()
        session.commit()
        return False
    dns = aliyun_client(settings, aliyun_account)
    try:
        live = dns.describe_registrar_domain(domain.name)
    except AliyunError as exc:
        if _handle_aliyun_fill_error(session, aliyun_account, domain, exc):
            return False
        _mark_unusable(session, domain, f"无法读取注册商状态: {exc}")
        session.commit()
        return False
    if live.nameservers:
        domain.nameservers = ",".join(live.nameservers)
    if live.domain_status:
        domain.domain_status = live.domain_status
    if live.audit_status:
        domain.audit_status = live.audit_status
    domain.client_hold = bool(getattr(live, "client_hold", False))
    ready, reason = domain_ready_for_fill(
        AliyunDomain(
            name=domain.name,
            domain_status=live.domain_status,
            audit_status=live.audit_status,
            nameservers=live.nameservers or [],
            client_hold=bool(getattr(live, "client_hold", False)),
        ),
        require_nameservers=True,
        require_audit=require_audit,
    )
    if not ready:
        _mark_unusable(session, domain, reason or "域名当前不能补位")
        session.commit()
        return False
    return True


def _handle_aliyun_fill_error(session: Session, account: AliyunAccount, domain: Domain, exc: AliyunError) -> bool:
    if not getattr(exc, "throttled", False):
        return False
    delay, nxt = aliyun_throttle_backoff(account.throttle_backoff_seconds)
    account.throttle_until = _now() + timedelta(seconds=delay)
    account.throttle_backoff_seconds = nxt
    domain.filling_at = None
    domain.updated_at = _now()
    if not domain.yyds_domain_id:
        domain.status = STATUS_UNUSED
        domain.error_reason = None
    else:
        domain.status = STATUS_ERROR
        domain.error_reason = "阿里云接口限流"
    add_event(
        session,
        level="error",
        code="aliyun_throttled",
        message=f"阿里云账号 {account.name} 被限流，补位暂停",
        domain_name=domain.name,
        aliyun_account_id=account.id,
        yyds_account_id=domain.yyds_account_id,
    )
    session.commit()
    return True


def _handle_yyds_fill_error(session: Session, account: YydsAccount, domain: Domain, exc: YydsError) -> bool:
    if exc.status_code != 429:
        return False
    apply_account_throttle(account, retry_after=getattr(exc, "retry_after_seconds", None))
    domain.filling_at = None
    domain.updated_at = _now()
    if not domain.yyds_domain_id:
        domain.status = STATUS_UNUSED
        domain.error_reason = None
    else:
        domain.status = STATUS_ERROR
        domain.error_reason = "yyds 接口限流"
    add_event(
        session,
        level="error",
        code="yyds_throttled",
        message=f"yyds 账号 {account.name} 被限流，补位暂停",
        domain_name=domain.name,
        aliyun_account_id=domain.aliyun_account_id,
        yyds_account_id=account.id,
    )
    session.commit()
    return True


def _mark_unusable(session: Session, domain: Domain, reason: str) -> None:
    domain.status = STATUS_ERROR
    domain.error_reason = redact(reason)[:500]
    domain.filling_at = None
    domain.updated_at = _now()
    add_event(
        session,
        level="error",
        code="fill_skipped",
        message=f"{domain.name} 不能补位: {reason}",
        domain_name=domain.name,
        aliyun_account_id=domain.aliyun_account_id,
        yyds_account_id=domain.yyds_account_id,
    )


def _fill_new(session: Session, settings: Settings, prefer_yyds_id: str | None) -> bool:
    picked_account = select_fill_account(_account_views(session), prefer_yyds_id)
    if picked_account is None:
        return False
    picked_domain = select_unused_domain(_domain_views(session))
    if picked_domain is None:
        return False
    account = session.get(YydsAccount, UUID(picked_account.id))
    domain = session.get(Domain, UUID(picked_domain.id))
    if account is None or domain is None:
        return False
    domain.filling_at = _now()
    session.commit()
    occupant = _occupying_yyds_account(session, domain.name)
    if occupant is not None:
        domain.status = STATUS_USED
        domain.yyds_account_id = occupant
        domain.error_reason = None
        domain.filling_at = None
        domain.updated_at = _now()
        add_event(
            session,
            level="info",
            code="fill_occupied",
            message=f"{domain.name} 已在其他 yyds 账号占用，记为已使用",
            domain_name=domain.name,
            yyds_account_id=occupant,
        )
        session.commit()
        return True
    if not _refresh_live_registrar(session, settings, domain, require_audit=True):
        return True
    try:
        _bind_and_verify(session, settings, account, domain, newly_added=True)
        return True
    except YydsError as exc:
        session.refresh(domain)
        if _handle_yyds_fill_error(session, account, domain, exc):
            return True
        if domain.status != STATUS_USED:
            domain.status = STATUS_ERROR
            domain.error_reason = redact(str(exc))[:500]
            domain.filling_at = None
            domain.updated_at = _now()
            add_event(
                session,
                level="error",
                code="fill_failed",
                message=f"{domain.name} 补位失败: {redact(str(exc))}",
                domain_name=domain.name,
                aliyun_account_id=domain.aliyun_account_id,
                yyds_account_id=account.id,
            )
        session.commit()
        return True
    except AliyunError as exc:
        session.refresh(domain)
        aliyun_account = session.get(AliyunAccount, domain.aliyun_account_id) if domain.aliyun_account_id else None
        if aliyun_account is not None and _handle_aliyun_fill_error(session, aliyun_account, domain, exc):
            return True
        if domain.status != STATUS_USED:
            domain.status = STATUS_ERROR
            domain.error_reason = redact(str(exc))[:500]
            domain.filling_at = None
            domain.updated_at = _now()
            add_event(
                session,
                level="error",
                code="fill_failed",
                message=f"{domain.name} 补位失败: {redact(str(exc))}",
                domain_name=domain.name,
                aliyun_account_id=domain.aliyun_account_id,
                yyds_account_id=account.id,
            )
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        session.refresh(domain)
        if domain.status != STATUS_USED:
            domain.status = STATUS_ERROR
            domain.error_reason = redact(str(exc))[:500]
            domain.filling_at = None
            domain.updated_at = _now()
            add_event(
                session,
                level="error",
                code="fill_failed",
                message=f"{domain.name} 补位失败: {redact(str(exc))}",
                domain_name=domain.name,
                aliyun_account_id=domain.aliyun_account_id,
                yyds_account_id=account.id,
            )
        session.commit()
        return True


def _throttle_active(account, now: datetime | None = None) -> bool:
    until = _aware(getattr(account, "throttle_until", None))
    current = now or _now()
    return until is not None and until > current


def _account_throttled(account: YydsAccount, now: datetime | None = None) -> bool:
    return _throttle_active(account, now)


def _repair_bound_errors(
    session: Session,
    settings: Settings,
    immediate_ids: set[UUID] | None = None,
) -> bool:
    cooldown = _now() - verify_repair_cooldown(settings.verify_retry_seconds)
    skip_cooldown = immediate_ids or set()
    rows = list(
        session.scalars(
            select(Domain)
            .where(
                Domain.status == STATUS_ERROR,
                Domain.yyds_domain_id.is_not(None),
                Domain.yyds_account_id.is_not(None),
                Domain.filling_at.is_(None),
            )
            .order_by(Domain.updated_at)
        ).all()
    )
    for row in rows:
        if row.id not in skip_cooldown and _aware(row.updated_at) and _aware(row.updated_at) > cooldown:
            continue
        account = session.get(YydsAccount, row.yyds_account_id)
        if account is None or not account.enabled or _account_throttled(account):
            continue
        aliyun_account = session.get(AliyunAccount, row.aliyun_account_id) if row.aliyun_account_id else None
        if aliyun_account is not None and _throttle_active(aliyun_account):
            continue
        row.filling_at = _now()
        session.commit()
        if not _refresh_live_registrar(session, settings, row):
            return True
        try:
            _bind_and_verify(
                session,
                settings,
                account,
                row,
                newly_added=False,
                rewrite_dns=_rewrite_dns_on_repair(row.error_reason),
            )
            return True
        except YydsError as exc:
            session.refresh(row)
            if _handle_yyds_fill_error(session, account, row, exc):
                return True
            row.status = STATUS_ERROR
            row.error_reason = redact(str(exc))[:500]
            row.filling_at = None
            row.updated_at = _now()
            add_event(
                session,
                level="error",
                code="fill_repair_failed",
                message=f"{row.name} 修复失败: {redact(str(exc))}",
                domain_name=row.name,
                yyds_account_id=account.id,
            )
            session.commit()
            return True
        except AliyunError as exc:
            session.refresh(row)
            bound_aliyun = session.get(AliyunAccount, row.aliyun_account_id) if row.aliyun_account_id else None
            if bound_aliyun is not None and _handle_aliyun_fill_error(session, bound_aliyun, row, exc):
                return True
            row.status = STATUS_ERROR
            row.error_reason = redact(str(exc))[:500]
            row.filling_at = None
            row.updated_at = _now()
            add_event(
                session,
                level="error",
                code="fill_repair_failed",
                message=f"{row.name} 修复失败: {redact(str(exc))}",
                domain_name=row.name,
                yyds_account_id=account.id,
            )
            session.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            row.status = STATUS_ERROR
            row.error_reason = redact(str(exc))[:500]
            row.filling_at = None
            row.updated_at = _now()
            add_event(
                session,
                level="error",
                code="fill_repair_failed",
                message=f"{row.name} 修复失败: {redact(str(exc))}",
                domain_name=row.name,
                yyds_account_id=account.id,
            )
            session.commit()
            return True
    return False


def _clear_stale_filling(session: Session) -> set[UUID]:
    rows = list(session.scalars(select(Domain).where(Domain.filling_at.is_not(None))).all())
    immediate_ids: set[UUID] = set()
    changed = False
    for row in rows:
        if _aware(row.filling_at) is None:
            continue
        row.filling_at = None
        if row.status == STATUS_UNUSED and row.yyds_domain_id and row.yyds_account_id:
            row.status = STATUS_ERROR
            row.error_reason = "补位中断，等待修复"
            add_event(
                session,
                level="warning",
                code="fill_stale",
                message=f"{row.name} 补位中断，改为异常后等待修复",
                domain_name=row.name,
                aliyun_account_id=row.aliyun_account_id,
                yyds_account_id=row.yyds_account_id,
            )
        if row.status == STATUS_ERROR and row.yyds_domain_id and row.yyds_account_id:
            immediate_ids.add(row.id)
        changed = True
    if changed:
        session.commit()
    return immediate_ids


def _bind_and_verify(
    session: Session,
    settings: Settings,
    account: YydsAccount,
    domain: Domain,
    newly_added: bool,
    rewrite_dns: bool = True,
) -> None:
    lock = acquire_yyds_session_lock(session, account.id)
    client = None
    try:
        client = yyds_client(settings, account)
        client.ensure_session()
        persist_yyds_session(settings, account, client)
        did_add = False
        if newly_added:
            usage = _sync_yyds_usage(account, client, fallback_add=False)
            session.commit()
            snap = session.scalar(select(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id == account.id))
            previous_names = first_snapshot_names(snap.names_json) if snap else None
            previous = len(previous_names or set())
            current_names: set[str] = set()
            for listed in usage["domains"]:
                raw = getattr(listed, "domain", None)
                if not raw:
                    continue
                try:
                    current_names.add(normalize_domain(str(raw)))
                except ValueError:
                    continue
            missing_previous = bool(previous_names) and any(name not in current_names for name in previous_names)
            if yyds_lists_unreliable_for_add(
                domains_ok=bool(usage["domains_ok"]),
                listed_domains=usage["listed_domains"],
                quota_used=usage["quota_domains"],
                previous_count=previous,
                missing_previous=missing_previous,
            ):
                account.used_domains = -1
                add_event(
                    session,
                    level="warning",
                    code="yyds_list_unreliable",
                    message=f"{account.name} 列表不可靠，本轮不加域",
                    domain_name=domain.name,
                    yyds_account_id=account.id,
                )
                domain.filling_at = None
                domain.updated_at = _now()
                session.commit()
                return
            existing = find_listed_domain(usage["domains"], domain.name)
            if existing is None:
                if not has_wildcard_slot(account.max_wildcard, stored_occupancy(account.used_wildcard)) or not has_domain_slot(
                    account.max_domains, stored_occupancy(account.used_domains)
                ):
                    domain.filling_at = None
                    domain.updated_at = _now()
                    session.commit()
                    return
                created = None
                try:
                    created = client.add_domain(domain.name, enable_wildcard=True)
                except YydsError as exc:
                    if exc.status_code != 409:
                        raise YydsError(
                            f"yyds 加域名失败: {exc}",
                            status_code=exc.status_code,
                            payload=exc.payload,
                            retry_after_seconds=exc.retry_after_seconds,
                            code=exc.code,
                        ) from exc
                    existing = find_listed_domain(client.list_domains(), domain.name)
                    if existing is None:
                        raise YydsError(
                            "yyds 返回 409，域名可能仍绑在别人或删除未完成",
                            status_code=409,
                            payload=exc.payload,
                        ) from exc
                if existing is not None:
                    domain.yyds_account_id = account.id
                    domain.yyds_domain_id = existing.id
                    session.commit()
                else:
                    persist_yyds_session(settings, account, client)
                    domain_id = str(created.get("id") or created.get("domainId") or "")
                    if not domain_id:
                        match = find_listed_domain(client.list_domains(), domain.name)
                        if match is None:
                            raise RuntimeError("添加域名后未返回 id")
                        domain_id = match.id
                    domain.yyds_account_id = account.id
                    domain.yyds_domain_id = domain_id
                    did_add = True
                    session.commit()
            else:
                domain.yyds_account_id = account.id
                domain.yyds_domain_id = existing.id
                session.commit()
        if not domain.yyds_domain_id:
            raise RuntimeError("缺少 yyds 域名 id")
        _sync_yyds_usage(account, client, fallback_add=did_add)
        session.commit()
        _write_dns_and_verify(session, settings, account, domain, client, rewrite_dns=rewrite_dns)
        domain.status = STATUS_USED
        domain.error_reason = None
        domain.filling_at = None
        domain.updated_at = _now()
        _sync_yyds_usage(account, client, fallback_add=False)
        persist_yyds_session(settings, account, client)
        add_event(
            session,
            level="info",
            code="fill_ok",
            message=f"{domain.name} 已写入 {account.name} 并验证通过",
            domain_name=domain.name,
            aliyun_account_id=domain.aliyun_account_id,
            yyds_account_id=account.id,
        )
        session.commit()
    finally:
        try:
            if client is not None:
                persist_yyds_session(settings, account, client)
            session.commit()
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
        finally:
            if client is not None:
                client.close()
            release_yyds_session_lock(lock)


def _write_dns_and_verify(
    session: Session,
    settings: Settings,
    account: YydsAccount,
    domain: Domain,
    yyds,
    rewrite_dns: bool = True,
) -> None:
    domain_id = domain.yyds_domain_id
    if not domain_id:
        raise RuntimeError("缺少 yyds 域名 id")
    if yyds.set_private(domain_id) is False:
        raise RuntimeError("无法把域名设为私有")
    yyds.ensure_wildcard_rule(domain_id)
    persist_yyds_session(settings, account, yyds)
    guide = yyds.dns_guide(domain_id)
    wanted = parse_guide_records(guide, domain.name)
    missing = required_guide_missing(wanted)
    if missing:
        raise RuntimeError(f"{missing}，拒绝用本地写死记录")
    if rewrite_dns:
        aliyun_account = session.get(AliyunAccount, domain.aliyun_account_id) if domain.aliyun_account_id else None
        if aliyun_account is None:
            raise RuntimeError("找不到对应的阿里云账号")
        dns = aliyun_client(settings, aliyun_account)
        result = dns.apply_guide(domain.name, wanted)
        add_event(
            session,
            level="info",
            code="dns_applied",
            message=f"{domain.name} 已写阿里云解析，删除 {result['deleted']} 条，新增 {result['added']} 条",
            domain_name=domain.name,
            aliyun_account_id=aliyun_account.id,
            yyds_account_id=account.id,
        )
    yyds.verify_domain(domain_id)
    persist_yyds_session(settings, account, yyds)
    attempts = verify_attempts_this_cycle(settings.verify_attempts, settings.verify_retry_seconds)
    last_reason = "dns_propagating"
    for attempt in range(attempts):
        payload = None
        try:
            payload = yyds.batch_verify([domain_id])
        except YydsError as exc:
            if exc.status_code == 429:
                raise
            payload = None
        except AttributeError:
            payload = None
        if payload is None:
            payload = yyds.dns_status(domain_id)
        ready, last_reason = yyds.verify_ready(payload)
        if ready:
            try:
                yyds.enable_wildcard_rules(domain_id)
                persist_yyds_session(settings, account, yyds)
                return
            except YydsError as exc:
                if exc.status_code == 429:
                    persist_yyds_session(settings, account, yyds)
                    raise
                if _wildcard_enable_retryable(exc):
                    last_reason = "wildcard_mx_missing"
                else:
                    persist_yyds_session(settings, account, yyds)
                    raise YydsError(
                        f"yyds 启用规则失败: {exc}",
                        status_code=exc.status_code,
                        payload=exc.payload,
                        retry_after_seconds=exc.retry_after_seconds,
                        code=exc.code,
                    ) from exc
        if last_reason not in RETRY_RESULTS:
            raise RuntimeError(f"yyds 验证失败: {last_reason}")
        if attempt >= attempts - 1:
            break
        persist_yyds_session(settings, account, yyds)
    raise RuntimeError(f"yyds 验证待生效: {last_reason}")
