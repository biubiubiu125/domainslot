from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

STATUS_UNUSED = "unused"
STATUS_USED = "used"
STATUS_ERROR = "error"
USER_STATUSES = (STATUS_UNUSED, STATUS_USED, STATUS_ERROR)


def assign_discovered_status(account_has_completed_first_sync: bool) -> str:
    return STATUS_UNUSED if account_has_completed_first_sync else STATUS_USED


def status_after_yyds_bind_success() -> str:
    return STATUS_USED


def status_after_yyds_removed(previous_status: str) -> str:
    if previous_status == STATUS_ERROR:
        return STATUS_ERROR
    return STATUS_USED


def has_wildcard_slot(max_rules: int | None, used_rules: int) -> bool:
    if max_rules is None or max_rules < 0:
        return True
    return used_rules < max_rules


@dataclass(frozen=True)
class AccountView:
    id: str
    sort_order: int
    receive_enabled: bool
    enabled: bool
    login_ok: bool
    max_wildcard: int | None
    used_wildcard: int


def select_fill_account(
    accounts: list[AccountView],
    prefer_id: str | None = None,
) -> AccountView | None:
    eligible = [
        item
        for item in accounts
        if item.enabled
        and item.receive_enabled
        and item.login_ok
        and has_wildcard_slot(item.max_wildcard, item.used_wildcard)
    ]
    eligible.sort(key=lambda item: (item.sort_order, item.id))
    if prefer_id:
        for item in eligible:
            if item.id == prefer_id:
                return item
    return eligible[0] if eligible else None


@dataclass(frozen=True)
class DomainView:
    id: str
    name: str
    status: str
    registration_at: datetime | None
    created_at: datetime
    filling: bool
    yyds_account_id: str | None
    aliyun_ready: bool = True


def _sort_dt(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def select_unused_domain(domains: list[DomainView]) -> DomainView | None:
    candidates = [
        item
        for item in domains
        if item.status == STATUS_UNUSED
        and not item.filling
        and not item.yyds_account_id
        and item.aliyun_ready
    ]
    candidates.sort(
        key=lambda item: (
            item.registration_at is None,
            _sort_dt(item.registration_at),
            _sort_dt(item.created_at),
            item.name,
        )
    )
    return candidates[0] if candidates else None


@dataclass(frozen=True)
class SnapshotDiff:
    added: frozenset[str]
    removed: frozenset[str]


def diff_domain_sets(previous: list[str] | set[str], current: list[str] | set[str]) -> SnapshotDiff:
    prev = {normalize_silent(item) for item in previous if item}
    curr = {normalize_silent(item) for item in current if item}
    prev.discard("")
    curr.discard("")
    return SnapshotDiff(added=frozenset(curr - prev), removed=frozenset(prev - curr))


def normalize_silent(value: str) -> str:
    try:
        from app.domainutil import normalize_domain

        return normalize_domain(value)
    except ValueError:
        return (value or "").strip().rstrip(".").lower()


def overview_notice(unused_count: int, has_fillable_account: bool) -> str | None:
    if has_fillable_account and unused_count <= 0:
        return "有空位但没有未使用域名"
    if unused_count > 0 and not has_fillable_account:
        return "有未使用但 yyds 已满"
    return None


def parse_limit(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number
