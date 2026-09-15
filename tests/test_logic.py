from datetime import datetime, timezone

from app.aliyun.client import AliyunDomain, domain_ready_for_fill
from app.aliyun.records import is_conflict_record, parse_guide_records, to_aliyun_rr, aliyun_nameservers_ok, WantedRecord
from app.domainutil import normalize_domain
from app.worker.logic import (
    STATUS_ERROR,
    STATUS_UNUSED,
    STATUS_USED,
    AccountView,
    DomainView,
    assign_discovered_status,
    diff_domain_sets,
    has_wildcard_slot,
    overview_notice,
    select_fill_account,
    select_unused_domain,
    status_after_yyds_removed,
)


def test_first_sync_marks_used():
    assert assign_discovered_status(False) == STATUS_USED
    assert assign_discovered_status(True) == STATUS_UNUSED


def test_yyds_removed_keeps_used():
    assert status_after_yyds_removed(STATUS_USED) == STATUS_USED
    assert status_after_yyds_removed(STATUS_UNUSED) == STATUS_USED
    assert status_after_yyds_removed(STATUS_ERROR) == STATUS_ERROR


def test_normalize_idn_and_wildcard_prefix():
    assert normalize_domain("Example.COM.") == "example.com"
    assert normalize_domain("*.Example.COM") == "example.com"


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


def test_fill_account_order_and_prefer_deleted():
    accounts = [
        AccountView("a", 10, True, True, True, 5, 5),
        AccountView("b", 20, True, True, True, 5, 1),
        AccountView("c", 30, False, True, True, 5, 0),
        AccountView("d", 40, True, True, False, 5, 0),
    ]
    picked = select_fill_account(accounts)
    assert picked is not None
    assert picked.id == "b"
    preferred = select_fill_account(accounts, prefer_id="b")
    assert preferred is not None and preferred.id == "b"


def test_wildcard_unlimited_and_full():
    assert has_wildcard_slot(-1, 99) is True
    assert has_wildcard_slot(None, 0) is True
    assert has_wildcard_slot(5, 5) is False
    assert has_wildcard_slot(5, 4) is True


def test_overview_notices():
    assert overview_notice(0, True) == "有空位但没有未使用域名"
    assert overview_notice(3, False) == "有未使用但 yyds 已满"
    assert overview_notice(1, True) is None


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
    assert is_conflict_record("@", "A", wanted) is False


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
