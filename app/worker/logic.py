from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STATUS_UNUSED = "unused"
STATUS_USED = "used"
STATUS_ERROR = "error"
USER_STATUSES = (STATUS_UNUSED, STATUS_USED, STATUS_ERROR)

_ALIYUN_READINESS_MARKERS = (
    "ClientHold",
    "赎回",
    "域名状态异常",
    "无法读取实名审核状态",
    "未实名",
    "审核未通过",
    "无法读取注册商 NS",
    "NS 不是阿里云",
    "阿里云列表中已不存在",
    "阿里云接口限流",
)


def aliyun_readiness_error(reason: str | None) -> bool:
    text = (reason or "").strip()
    if not text:
        return False
    return any(marker in text for marker in _ALIYUN_READINESS_MARKERS)


def assign_discovered_status(account_has_completed_first_sync: bool) -> str:
    return STATUS_UNUSED if account_has_completed_first_sync else STATUS_USED


def status_after_yyds_bind_success() -> str:
    return STATUS_USED


ALIYUN_EMPTY_LIST_WARNING = "阿里云列表为空，已有库存未改"


def status_after_yyds_removed(previous_status: str) -> str:
    if previous_status == STATUS_UNUSED:
        return STATUS_UNUSED
    return STATUS_USED


def has_wildcard_slot(max_rules: int | None, used_rules: int) -> bool:
    if max_rules is None:
        return False
    if used_rules < 0:
        return False
    if max_rules < 0:
        return True
    return used_rules < max_rules


def occupancy_used(*, listed: int | None, quota_used: int | None, max_rules: int | None) -> int:
    if listed is not None:
        if list_shorter_than_quota(listed, quota_used):
            return int(quota_used)
        if listed == 0 and (quota_used is None or quota_used < 0):
            return -1
        return int(listed)
    if quota_used is not None:
        return int(quota_used)
    if max_rules is not None and max_rules >= 0:
        return int(max_rules)
    if max_rules is not None and max_rules < 0:
        return -1
    return 0


def stored_occupancy(value: int | None) -> int:
    if value is None:
        return 0
    return int(value)


def occupancy_unknown(max_rules: int | None, used_rules: int | None) -> bool:
    if max_rules is None:
        return True
    if used_rules is not None and int(used_rules) < 0:
        return True
    return False


def increment_known_occupancy(current: int | None) -> int:
    if current is None or current < 0:
        return -1
    return int(current) + 1


def first_snapshot_names(names_json: str | None) -> set[str] | None:
    if names_json is None:
        return None
    try:
        loaded = json.loads(names_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, list):
        return None
    return {normalize_silent(str(item)) for item in loaded if item}


def discovered_from_first_snapshot(
    name: str,
    *,
    first_sync_done: bool,
    first_sync_names: str | None,
) -> bool:
    cohort = first_snapshot_names(first_sync_names)
    if cohort is not None:
        return name in cohort
    return not first_sync_done


def list_shorter_than_quota(listed: int, quota_used: int | None) -> bool:
    if quota_used is None:
        return False
    try:
        return listed < int(quota_used)
    except (TypeError, ValueError):
        return False


def yyds_snapshot_unreliable(
    listed: int,
    quota_used: int | None,
    previous_count: int,
    *,
    missing_previous: bool = False,
) -> bool:
    if list_shorter_than_quota(listed, quota_used):
        return True
    quota_unknown = quota_used is None or quota_used < 0
    if listed == 0 and quota_unknown:
        return True
    if quota_unknown and previous_count > listed:
        return True
    if quota_unknown and missing_previous:
        return True
    return False


def yyds_lists_unreliable_for_add(
    *,
    domains_ok: bool,
    listed_domains: int | None,
    quota_used: int | None,
    previous_count: int = 0,
    missing_previous: bool = False,
) -> bool:
    if not domains_ok or listed_domains is None:
        return True
    return yyds_snapshot_unreliable(
        listed_domains,
        quota_used,
        previous_count,
        missing_previous=missing_previous,
    )


def has_domain_slot(max_domains: int | None, used_domains: int) -> bool:
    return has_wildcard_slot(max_domains, used_domains)


def aliyun_throttle_backoff(current_seconds: int | None) -> tuple[int, int]:
    delay = int(current_seconds or 120)
    delay = min(max(delay, 120), 3600)
    return delay, min(delay * 2, 3600)


def apply_account_throttle(account, now: datetime | None = None, retry_after: int | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    delay, nxt = aliyun_throttle_backoff(getattr(account, "throttle_backoff_seconds", None))
    if retry_after is not None:
        try:
            header_delay = int(retry_after)
        except (TypeError, ValueError):
            header_delay = 0
        if header_delay > 0:
            delay = min(max(header_delay, 1), 3600)
    account.throttle_until = current + timedelta(seconds=delay)
    account.throttle_backoff_seconds = nxt
    return delay


def aliyun_account_due(
    last_poll: datetime | None,
    now: datetime,
    interval: float,
    index: int,
    force: bool = False,
) -> bool:
    if force or last_poll is None:
        return True
    last = last_poll if last_poll.tzinfo else last_poll.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    needed = float(interval) + max(int(index), 0) * 7
    return (current - last).total_seconds() >= needed


def name_in_snapshot_json(names_json: str | None, name: str) -> bool:
    try:
        names = json.loads(names_json or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(names, list):
        return False
    want = normalize_silent(name)
    if not want:
        return False
    return want in {normalize_silent(str(item)) for item in names if item}


def next_fill_prefer(queue: list[str], did: bool) -> str | None:
    current = list(queue)
    if not did and current:
        current = current[1:]
    return current[0] if current else None


@dataclass(frozen=True)
class AccountView:
    id: str
    sort_order: int
    receive_enabled: bool
    enabled: bool
    login_ok: bool
    max_wildcard: int | None
    used_wildcard: int
    max_domains: int | None = None
    used_domains: int = 0


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
        and has_domain_slot(item.max_domains, item.used_domains)
    ]
    eligible.sort(key=lambda item: (item.sort_order, item.id))
    if prefer_id:
        for item in eligible:
            if item.id == prefer_id:
                return item
    return eligible[0] if eligible else None


def status_after_yyds_account_removed(previous_status: str, yyds_domain_id: str | None) -> str:
    if yyds_domain_id:
        return STATUS_USED
    return previous_status


def verify_attempts_this_cycle(verify_attempts: int | None, verify_retry_seconds: float | None) -> int:
    attempts = max(1, int(verify_attempts or 1))
    if float(verify_retry_seconds or 0) > 0:
        return 1
    return attempts


def verify_repair_cooldown(verify_retry_seconds: float | None) -> timedelta:
    seconds = max(0.0, float(verify_retry_seconds or 0))
    return timedelta(seconds=seconds)


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
    yyds_domain_id: str | None = None


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
        and not item.yyds_domain_id
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


def overview_notice(
    unused_count: int,
    has_fillable_account: bool,
    *,
    quota_unknown: bool = False,
    inventory_ready: bool = True,
) -> str | None:
    if has_fillable_account and unused_count <= 0:
        return "有空位但没有未使用域名"
    if unused_count > 0 and not has_fillable_account:
        if quota_unknown:
            return "有未使用但 yyds 配额未知，暂不补位"
        return "有未使用但 yyds 已满"
    if unused_count > 0 and has_fillable_account and not inventory_ready:
        return "有未使用但当前都不能补位"
    return None


def parse_limit(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number
