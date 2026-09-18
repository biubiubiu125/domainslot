from datetime import datetime, timedelta, timezone

from app.aliyun.client import AliyunDomain, _audit_status_from, domain_ready_for_fill
from app.aliyun.records import (
    is_conflict_record,
    parse_guide_records,
    required_guide_missing,
    to_aliyun_rr,
    aliyun_nameservers_ok,
    WantedRecord,
)
from app.domainutil import domains_match, normalize_domain
from app.worker.logic import (
    STATUS_ERROR,
    STATUS_UNUSED,
    STATUS_USED,
    AccountView,
    DomainView,
    aliyun_account_due,
    aliyun_readiness_error,
    aliyun_throttle_backoff,
    assign_discovered_status,
    diff_domain_sets,
    has_domain_slot,
    has_wildcard_slot,
    name_in_snapshot_json,
    occupancy_used,
    next_fill_prefer,
    overview_notice,
    parse_limit,
    select_fill_account,
    select_unused_domain,
    status_after_yyds_account_removed,
    status_after_yyds_removed,
    verify_attempts_this_cycle,
    verify_repair_cooldown,
)


def test_first_sync_marks_used():
    assert assign_discovered_status(False) == STATUS_USED
    assert assign_discovered_status(True) == STATUS_UNUSED


def test_aliyun_readiness_error_classifies_reasons():
    assert aliyun_readiness_error("域名处于赎回状态") is True
    assert aliyun_readiness_error("未实名或审核未通过: NONAUDIT") is True
    assert aliyun_readiness_error("NS 不是阿里云: ns1.example.com") is True
    assert aliyun_readiness_error("域名处于 ClientHold") is True
    assert aliyun_readiness_error("阿里云列表中已不存在") is True
    assert aliyun_readiness_error("yyds 加域名失败: cross_origin_request_blocked HTTP 403") is False
    assert aliyun_readiness_error("yyds 返回 409，域名可能仍绑在别人或删除未完成") is False
    assert aliyun_readiness_error(None) is False


def test_yyds_removed_keeps_used():
    assert status_after_yyds_removed(STATUS_USED) == STATUS_USED
    assert status_after_yyds_removed(STATUS_UNUSED) == STATUS_UNUSED
    assert status_after_yyds_removed(STATUS_ERROR) == STATUS_USED


def test_normalize_idn_and_wildcard_prefix():
    assert normalize_domain("Example.COM.") == "example.com"
    assert normalize_domain("*.Example.COM") == "example.com"
    assert domains_match("New.COM", "new.com") is True
    assert domains_match("例子.com", normalize_domain("例子.com")) is True
    assert domains_match("a.com", "b.com") is False


def test_pick_oldest_unused():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    domains = [
        DomainView("2", "b.com", STATUS_UNUSED, now, now, False, None),
        DomainView("1", "a.com", STATUS_UNUSED, older, now, False, None),
        DomainView("3", "c.com", STATUS_USED, older, now, False, None),
        DomainView("4", "d.com", STATUS_UNUSED, older, now, True, None),
    ]
    picked = select_unused_domain(domains)
    assert picked is not None
    assert picked.name == "a.com"


def test_pick_oldest_skips_missing_registration():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    domains = [
        DomainView("2", "b.com", STATUS_UNUSED, None, now, False, None),
        DomainView("1", "a.com", STATUS_UNUSED, older, now, False, None),
    ]
    picked = select_unused_domain(domains)
    assert picked is not None
    assert picked.name == "a.com"


def test_skip_unused_without_aliyun_ready():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    domains = [
        DomainView("1", "a.com", STATUS_UNUSED, now, now, False, None, aliyun_ready=False),
        DomainView("2", "b.com", STATUS_UNUSED, now, now, False, None, aliyun_ready=True),
    ]
    assert select_unused_domain(domains).name == "b.com"


def test_skip_unused_with_leftover_yyds_domain_id():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    domains = [
        DomainView("1", "old.com", STATUS_UNUSED, now, now, False, None, yyds_domain_id="yd-old"),
        DomainView("2", "fresh.com", STATUS_UNUSED, now, now, False, None),
    ]
    picked = select_unused_domain(domains)
    assert picked is not None
    assert picked.name == "fresh.com"


def test_status_after_yyds_account_removed_keeps_unbound_unused():
    assert status_after_yyds_account_removed(STATUS_UNUSED, None) == STATUS_UNUSED
    assert status_after_yyds_account_removed(STATUS_UNUSED, "yd1") == STATUS_USED
    assert status_after_yyds_account_removed(STATUS_ERROR, "yd1") == STATUS_USED
    assert status_after_yyds_account_removed(STATUS_USED, "yd1") == STATUS_USED


def test_verify_attempts_this_cycle_does_not_hammer_when_retry_waits():
    assert verify_attempts_this_cycle(12, 15) == 1
    assert verify_attempts_this_cycle(3, 0) == 3
    assert verify_attempts_this_cycle(0, 0) == 1


def test_verify_repair_cooldown_uses_retry_seconds():
    assert verify_repair_cooldown(15) == timedelta(seconds=15)
    assert verify_repair_cooldown(0) == timedelta(0)


def test_fill_account_order_and_prefer_deleted():
    accounts = [
        AccountView("a", 10, True, True, True, 5, 5, max_domains=50, used_domains=0),
        AccountView("b", 20, True, True, True, 5, 1, max_domains=50, used_domains=0),
        AccountView("c", 30, False, True, True, 5, 0, max_domains=50, used_domains=0),
        AccountView("d", 40, True, True, False, 5, 0, max_domains=50, used_domains=0),
    ]
    picked = select_fill_account(accounts)
    assert picked is not None
    assert picked.id == "b"
    preferred = select_fill_account(accounts, prefer_id="b")
    assert preferred is not None and preferred.id == "b"


def test_wildcard_unlimited_and_full():
    assert has_wildcard_slot(-1, 99) is True
    assert has_wildcard_slot(-1, 0) is True
    assert has_wildcard_slot(-1, -1) is False
    assert has_wildcard_slot(None, 0) is False
    assert has_wildcard_slot(5, 5) is False
    assert has_wildcard_slot(5, 4) is True


def test_overview_notices():
    assert overview_notice(0, True) == "有空位但没有未使用域名"
    assert overview_notice(3, False) == "有未使用但 yyds 已满"
    assert overview_notice(3, False, quota_unknown=True) == "有未使用但 yyds 配额未知，暂不补位"
    assert overview_notice(1, True) is None
    assert overview_notice(3, True, inventory_ready=False) == "有未使用但当前都不能补位"
    assert overview_notice(0, True, inventory_ready=False) == "有空位但没有未使用域名"


def test_snapshot_diff_and_idn():
    diff = diff_domain_sets(["A.com", "old.com"], ["a.com", "new.com"])
    assert diff.added == frozenset({"new.com"})
    assert diff.removed == frozenset({"old.com"})


def test_aliyun_rr_and_conflicts():
    assert to_aliyun_rr("example.com", "example.com") == "@"
    assert to_aliyun_rr("*.example.com", "example.com") == "*"
    assert to_aliyun_rr("_yydsmail-verify.example.com", "example.com") == "_yydsmail-verify"
    wanted = [
        WantedRecord("TXT", "_yydsmail-verify", "token"),
        WantedRecord("MX", "@", "mx.example", 10),
        WantedRecord("MX", "*", "mx.example", 10),
    ]
    assert is_conflict_record("@", "CNAME", wanted) is True
    assert is_conflict_record("@", "MX", wanted) is True
    assert is_conflict_record("_yydsmail-verify", "TXT", wanted) is True
    assert is_conflict_record("@", "NS", wanted) is False
    assert is_conflict_record("@", "A", wanted) is True
    assert is_conflict_record("www", "AAAA", wanted) is False
    assert is_conflict_record("www", "CNAME", wanted) is False
    assert is_conflict_record("www", "A", wanted) is False
    assert is_conflict_record("*", "CNAME", wanted) is True
    assert is_conflict_record("mail", "A", wanted) is False


def test_conflict_record_does_not_delete_unrelated_mx():
    wanted = [
        WantedRecord("MX", "@", "mx.example", 10),
        WantedRecord("MX", "*", "mx.example", 10),
        WantedRecord("TXT", "_yydsmail-verify", "token"),
    ]
    assert is_conflict_record("@", "MX", wanted) is True
    assert is_conflict_record("*", "MX", wanted) is True
    assert is_conflict_record("mail", "MX", wanted) is False
    assert is_conflict_record("smtp", "MX", wanted) is False
    assert is_conflict_record("@", "NS", wanted) is False
    wanted_mail = [WantedRecord("MX", "mail", "mx.example", 10)]
    assert is_conflict_record("mail", "MX", wanted_mail) is True
    assert is_conflict_record("smtp", "MX", wanted_mail) is False


def test_parse_guide_records():
    payload = {
        "data": {
            "records": [
                {"type": "TXT", "name": "_yydsmail-verify.foo.com", "value": "yyds-xiaolajiao-abc"},
                {"type": "MX", "name": "foo.com", "value": "mx.215.im", "priority": 10},
                {"type": "MX", "name": "*.foo.com", "value": "mx.215.im", "priority": 10},
            ]
        }
    }
    records = parse_guide_records(payload, "foo.com")
    assert [item.rr for item in records] == ["_yydsmail-verify", "@", "*"]
    assert aliyun_nameservers_ok(["dns9.hichina.com", "dns10.hichina.com"]) is True
    assert aliyun_nameservers_ok(["bob.ns.cloudflare.com"]) is False


def test_parse_guide_preview_items_nested_records():
    payload = {
        "items": [
            {
                "domainId": "x",
                "domain": "foo.com",
                "records": [
                    {"type": "TXT", "name": "_yydsmail-verify.foo.com", "value": "tok"},
                    {"type": "MX", "name": "foo.com", "value": "mx.215.im", "priority": 10},
                    {"type": "MX", "name": "*.foo.com", "value": "mx.215.im", "priority": 10},
                ],
            }
        ]
    }
    records = parse_guide_records(payload, "foo.com")
    assert [item.rr for item in records] == ["_yydsmail-verify", "@", "*"]


def test_parse_guide_ignores_other_domains():
    payload = {
        "items": [
            {
                "domain": "foo.com",
                "records": [
                    {"type": "TXT", "name": "_yydsmail-verify.foo.com", "value": "tok1"},
                    {"type": "MX", "name": "foo.com", "value": "mx.215.im", "priority": 10},
                ],
            },
            {
                "domain": "bar.com",
                "records": [
                    {"type": "TXT", "name": "_yydsmail-verify.bar.com", "value": "tok2"},
                    {"type": "MX", "name": "bar.com", "value": "mx.other", "priority": 10},
                ],
            },
            {
                "domain": "bar.com",
                "type": "MX",
                "name": "@",
                "value": "mx.other",
                "priority": 10,
            },
        ]
    }
    records = parse_guide_records(payload, "foo.com")
    assert [(item.type, item.rr, item.value) for item in records] == [
        ("TXT", "_yydsmail-verify", "tok1"),
        ("MX", "@", "mx.215.im"),
    ]


def test_parse_guide_skips_relative_mx_without_domain_in_items():
    payload = {
        "items": [
            {
                "domain": "foo.com",
                "records": [
                    {"type": "TXT", "name": "_yydsmail-verify.foo.com", "value": "tok"},
                    {"type": "MX", "name": "@", "value": "mx.215.im", "priority": 10},
                    {"type": "MX", "name": "*", "value": "mx.215.im", "priority": 10},
                ],
            },
            {"type": "MX", "name": "@", "value": "mx.other", "priority": 10},
        ]
    }
    records = parse_guide_records(payload, "foo.com")
    assert ("MX", "@", "mx.other") not in [(item.type, item.rr, item.value) for item in records]
    assert ("MX", "@", "mx.215.im") in [(item.type, item.rr, item.value) for item in records]
    assert ("MX", "*", "mx.215.im") in [(item.type, item.rr, item.value) for item in records]


def test_parse_guide_keeps_relative_records_array():
    payload = {
        "records": [
            {"type": "TXT", "name": "_yydsmail-verify", "value": "tok"},
            {"type": "MX", "name": "@", "value": "mx.215.im", "priority": 10},
            {"type": "MX", "name": "*", "value": "mx.215.im", "priority": 10},
        ]
    }
    records = parse_guide_records(payload, "foo.com")
    assert [(item.type, item.rr, item.value) for item in records] == [
        ("TXT", "_yydsmail-verify", "tok"),
        ("MX", "@", "mx.215.im"),
        ("MX", "*", "mx.215.im"),
    ]


def test_required_guide_missing_wildcard_mx():
    wanted = [
        WantedRecord("TXT", "_yydsmail-verify", "tok"),
        WantedRecord("MX", "@", "mx.215.im", 10),
    ]
    assert "通配" in (required_guide_missing(wanted) or "")
    wanted.append(WantedRecord("MX", "*", "mx.215.im", 10))
    assert required_guide_missing(wanted) is None


def test_fill_account_respects_domain_cap():
    accounts = [
        AccountView("a", 10, True, True, True, 15, 1, max_domains=50, used_domains=50),
        AccountView("b", 20, True, True, True, 5, 1, max_domains=188, used_domains=10),
    ]
    picked = select_fill_account(accounts)
    assert picked is not None
    assert picked.id == "b"
    assert select_fill_account(accounts[:1]) is None


def test_occupancy_unknown_treats_known_max_as_full():
    assert occupancy_used(listed=3, quota_used=1, max_rules=5) == 3
    assert occupancy_used(listed=None, quota_used=4, max_rules=5) == 4
    assert occupancy_used(listed=None, quota_used=None, max_rules=5) == 5
    assert occupancy_used(listed=None, quota_used=None, max_rules=None) == 0
    assert occupancy_used(listed=None, quota_used=None, max_rules=-1) == -1
    assert occupancy_used(listed=0, quota_used=5, max_rules=15) == 5
    assert occupancy_used(listed=0, quota_used=0, max_rules=15) == 0
    assert occupancy_used(listed=4, quota_used=5, max_rules=5) == 5
    assert occupancy_used(listed=0, quota_used=5, max_rules=5) == 5
    assert occupancy_used(listed=0, quota_used=None, max_rules=5) == -1
    assert occupancy_used(listed=0, quota_used=-1, max_rules=15) == -1
    assert has_wildcard_slot(5, occupancy_used(listed=0, quota_used=5, max_rules=5)) is False
    assert has_wildcard_slot(5, occupancy_used(listed=0, quota_used=None, max_rules=5)) is False


def test_unknown_quota_does_not_fill():
    assert has_wildcard_slot(None, 0) is False
    assert has_domain_slot(None, 0) is False
    accounts = [AccountView("a", 10, True, True, True, None, 0, max_domains=50, used_domains=0)]
    assert select_fill_account(accounts) is None


def test_apply_account_throttle_uses_retry_after():
    from datetime import datetime, timedelta, timezone

    from app.worker.logic import apply_account_throttle

    class Acc:
        throttle_until = None
        throttle_backoff_seconds = 120

    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    account = Acc()
    delay = apply_account_throttle(account, now, retry_after=7)
    assert delay == 7
    assert account.throttle_until == now + timedelta(seconds=7)
    assert account.throttle_backoff_seconds == 240


def test_domain_slot_and_throttle_backoff():
    assert has_domain_slot(50, 50) is False
    assert has_domain_slot(-1, 99) is True
    assert aliyun_throttle_backoff(None) == (120, 240)
    assert aliyun_throttle_backoff(240) == (240, 480)
    assert aliyun_throttle_backoff(3600) == (3600, 3600)


def test_aliyun_stagger_and_snapshot_occupancy():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    recent = datetime(2026, 1, 1, 11, 59, 30, tzinfo=timezone.utc)
    assert aliyun_account_due(recent, now, 60, 0, force=False) is False
    assert aliyun_account_due(recent, now, 60, 0, force=True) is True
    assert aliyun_account_due(None, now, 60, 3, force=False) is True
    assert name_in_snapshot_json('["A.com", "b.com"]', "a.com") is True
    assert name_in_snapshot_json('["b.com"]', "a.com") is False


def test_keep_shrink_prefer_until_queue_empty():
    assert next_fill_prefer(["a", "b"], did=True) == "a"
    assert next_fill_prefer(["a", "b"], did=False) == "b"
    assert next_fill_prefer(["b"], did=False) is None


def test_domain_ready_for_fill():
    ok, reason = domain_ready_for_fill(
        AliyunDomain(name="foo.com", domain_status="3", audit_status="SUCCEED", nameservers=["dns9.hichina.com"])
    )
    assert ok is True
    assert reason is None
    ok, reason = domain_ready_for_fill(AliyunDomain(name="foo.com", domain_status="2", audit_status="SUCCEED"))
    assert ok is False
    assert "赎回" in (reason or "")
    ok, reason = domain_ready_for_fill(AliyunDomain(name="foo.com", domain_status="3", audit_status="NONAUDIT"))
    assert ok is False
    assert "未实名" in (reason or "")
    ok, reason = domain_ready_for_fill(
        AliyunDomain(name="foo.com", domain_status="3", audit_status="SUCCEED", nameservers=[]),
        require_nameservers=True,
    )
    assert ok is False
    assert "NS" in (reason or "")
    ok, reason = domain_ready_for_fill(
        AliyunDomain(name="foo.com", domain_status="3", audit_status=None, nameservers=["dns9.hichina.com"]),
        require_audit=True,
    )
    assert ok is False
    assert "审核" in (reason or "") or "实名" in (reason or "")
    ok, reason = domain_ready_for_fill(
        AliyunDomain(name="foo.com", domain_status="3", audit_status=None, nameservers=["dns9.hichina.com"]),
    )
    assert ok is True
    held = AliyunDomain(
        name="foo.com",
        domain_status="3",
        audit_status="SUCCEED",
        nameservers=["dns9.hichina.com"],
    )
    held.client_hold = True
    ok, reason = domain_ready_for_fill(held)
    assert ok is False
    assert "Hold" in (reason or "")
    ok, reason = domain_ready_for_fill(
        AliyunDomain(name="foo.com", domain_status="3", audit_status="未实名", nameservers=["dns9.hichina.com"])
    )
    assert ok is False
    assert "未实名" in (reason or "")
    class RegistrarBody:
        real_name_status = "SUCCEED"
        domain_name_verification_status = "FAILED"

    audit = _audit_status_from(RegistrarBody())
    ok, reason = domain_ready_for_fill(
        AliyunDomain(
            name="foo.com",
            domain_status="3",
            audit_status=audit,
            nameservers=["dns9.hichina.com"],
        )
    )
    assert ok is False
    assert "FAILED" in (reason or "") or "未实名" in (reason or "")


def test_parse_limit_keeps_unlimited():
    assert parse_limit(-1) == -1
    assert parse_limit("-1") == -1
    assert parse_limit(None) is None
    assert parse_limit(15) == 15


def test_yyds_snapshot_unreliable_empty_list_without_quota():
    from app.worker.logic import list_shorter_than_quota, yyds_snapshot_unreliable

    assert list_shorter_than_quota(0, None) is False
    assert yyds_snapshot_unreliable(listed=0, quota_used=None, previous_count=2) is True
    assert yyds_snapshot_unreliable(listed=0, quota_used=-1, previous_count=2) is True
    assert yyds_snapshot_unreliable(listed=0, quota_used=0, previous_count=2) is False
    assert yyds_snapshot_unreliable(listed=0, quota_used=None, previous_count=0) is True
    assert yyds_snapshot_unreliable(listed=0, quota_used=-1, previous_count=0) is True
    assert yyds_snapshot_unreliable(listed=1, quota_used=2, previous_count=2) is True
    assert yyds_snapshot_unreliable(listed=2, quota_used=2, previous_count=2) is False
    assert yyds_snapshot_unreliable(listed=0, quota_used=5, previous_count=0) is True
    assert yyds_snapshot_unreliable(listed=1, quota_used=2, previous_count=0) is True
    assert yyds_snapshot_unreliable(listed=0, quota_used=0, previous_count=0) is False


def test_yyds_snapshot_unreliable_shorter_list_without_quota():
    from app.worker.logic import yyds_snapshot_unreliable

    assert yyds_snapshot_unreliable(listed=1, quota_used=None, previous_count=2) is True
    assert yyds_snapshot_unreliable(listed=1, quota_used=-1, previous_count=2) is True
    assert yyds_snapshot_unreliable(listed=1, quota_used=1, previous_count=2) is False
    assert yyds_snapshot_unreliable(listed=2, quota_used=None, previous_count=2) is False
    assert yyds_snapshot_unreliable(listed=3, quota_used=None, previous_count=2) is False
    assert yyds_snapshot_unreliable(
        listed=2, quota_used=None, previous_count=2, missing_previous=True
    ) is True
    assert yyds_snapshot_unreliable(
        listed=2, quota_used=2, previous_count=2, missing_previous=True
    ) is False


def test_stored_occupancy_keeps_unknown():
    from app.worker.logic import increment_known_occupancy, occupancy_unknown, stored_occupancy

    assert stored_occupancy(-1) == -1
    assert stored_occupancy(None) == 0
    assert stored_occupancy(0) == 0
    assert stored_occupancy(3) == 3
    assert increment_known_occupancy(-1) == -1
    assert increment_known_occupancy(None) == -1
    assert increment_known_occupancy(0) == 1
    assert increment_known_occupancy(4) == 5
    assert occupancy_unknown(-1, -1) is True
    assert occupancy_unknown(-1, 0) is False
    assert occupancy_unknown(None, 0) is True


def test_yyds_lists_unreliable_for_add():
    from app.worker.logic import yyds_lists_unreliable_for_add

    assert yyds_lists_unreliable_for_add(domains_ok=False, listed_domains=None, quota_used=4) is True
    assert yyds_lists_unreliable_for_add(domains_ok=True, listed_domains=0, quota_used=4) is True
    assert yyds_lists_unreliable_for_add(domains_ok=True, listed_domains=0, quota_used=0) is False
    assert yyds_lists_unreliable_for_add(domains_ok=True, listed_domains=4, quota_used=4) is False
    assert yyds_lists_unreliable_for_add(domains_ok=True, listed_domains=3, quota_used=4) is True
    assert yyds_lists_unreliable_for_add(
        domains_ok=True, listed_domains=1, quota_used=None, previous_count=5
    ) is True
    assert yyds_lists_unreliable_for_add(
        domains_ok=True, listed_domains=2, quota_used=None, previous_count=2, missing_previous=True
    ) is True
    assert yyds_lists_unreliable_for_add(
        domains_ok=True, listed_domains=2, quota_used=None, previous_count=2, missing_previous=False
    ) is False
    assert yyds_lists_unreliable_for_add(
        domains_ok=True, listed_domains=2, quota_used=2, previous_count=2, missing_previous=True
    ) is False
