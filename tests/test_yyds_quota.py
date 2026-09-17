from app.yyds.client import YydsClient, extract_quota


def test_extract_quota_reads_live_fields_not_plan_table():
    me = {
        "user": {"plan": {"name": "Max"}},
        "quota": {"maxWildcardRules": 15, "maxDomains": 50, "usedWildcardRules": 4},
    }
    quota = extract_quota(me, {}, [{}, {}, {}, {}], ["a.com"])
    assert quota["plan_name"] == "Max"
    assert quota["max_wildcard"] == 15
    assert quota["used_wildcard"] == 4
    assert quota["max_domains"] == 50


def test_extract_quota_unlimited_and_fallback_counts():
    me = {"maxWildcardRules": -1, "maxDomains": -1}
    quota = extract_quota(me, {}, [{}, {}], ["a.com", "b.com"])
    assert quota["max_wildcard"] == -1
    assert quota["max_domains"] == -1
    assert quota["used_wildcard"] == 2
    assert quota["used_domains"] == 2


def test_extract_quota_missing_max_stays_unknown():
    quota = extract_quota({"user": {"id": "u"}}, {}, [{}, {}], ["a.com"])
    assert quota["max_wildcard"] is None
    assert quota["max_domains"] is None


def test_extract_quota_reads_nested_dashboard_max():
    me = {"user": {"id": "u"}, "isAdmin": False, "quota": {}}
    quota_payload = {
        "domains": {"used": 12, "max": 50},
        "wildcardRules": {"used": 4, "max": 15},
        "plan": {"name": "Max"},
    }
    quota = extract_quota(me, quota_payload, [{}, {}, {}, {}], ["a"] * 12)
    assert quota["plan_name"] == "Max"
    assert quota["max_wildcard"] == 15
    assert quota["used_wildcard"] == 4
    assert quota["max_domains"] == 50
    assert quota["used_domains"] == 12


def test_verify_ready_from_dns_status():
    client = YydsClient("https://example.invalid/v1", "u", "p")
    try:
        ok, reason = client.verify_ready({"result": "receiving_ready"})
        assert ok is True
        assert reason == "receiving_ready"
        pending, pending_reason = client.verify_ready({"result": "dns_propagating"})
        assert pending is False
        assert pending_reason == "dns_propagating"
        batch_ok, batch_reason = client.verify_ready(
            {"items": [{"domainId": "x", "domain": "a.com", "result": "receiving_ready"}]}
        )
        assert batch_ok is True
        assert batch_reason == "receiving_ready"
        preview_ok, preview_reason = client.verify_ready(
            {"items": [{"domainId": "x", "domain": "a.com", "receivingReady": True, "records": []}]}
        )
        assert preview_ok is True
        assert preview_reason == "receiving_ready"
        domain_ok, domain_reason = client.verify_ready(
            {"id": "d1", "domain": "a.com", "isVerified": True, "isMxValid": True, "isPublic": False}
        )
        assert domain_ok is False
        assert domain_reason == "dns_propagating"
        envelope_ok, envelope_reason = client.verify_ready(
            {"success": True, "data": {"id": "d1", "isVerified": True, "isMxValid": True}}
        )
        assert envelope_ok is False
        assert envelope_reason == "dns_propagating"
        none_ok, none_reason = client.verify_ready(None)
        assert none_ok is False
        assert none_reason == "dns_propagating"
        wild_confirmed, wild_confirmed_reason = client.verify_ready(
            {"id": "d1", "isVerified": True, "isMxValid": True, "wildcardMxValid": True}
        )
        assert wild_confirmed is False
        assert wild_confirmed_reason == "dns_propagating"
        dns_ok, dns_ok_reason = client.verify_ready(
            {
                "dnsRecords": {
                    "mxValid": True,
                    "ownershipValid": True,
                    "wildcardMxValid": True,
                    "wildcardMxRequired": True,
                }
            }
        )
        assert dns_ok is False
        assert dns_ok_reason == "dns_propagating"
        unverified, unverified_reason = client.verify_ready(
            {"id": "d1", "isVerified": False, "isMxValid": False}
        )
        assert unverified is False
        assert unverified_reason in {"mx_missing", "dns_propagating", "txt_missing"}
        verified_only, verified_only_reason = client.verify_ready(
            {"id": "d1", "domain": "a.com", "isVerified": True, "isMxValid": False, "isCnameValid": False}
        )
        assert verified_only is False
        assert verified_only_reason == "mx_missing"
        envelope_mx_false, envelope_mx_reason = client.verify_ready(
            {"success": True, "data": {"id": "d1", "isVerified": True, "isMxValid": False}}
        )
        assert envelope_mx_false is False
        assert envelope_mx_reason == "mx_missing"
        wild_missing, wild_reason = client.verify_ready(
            {
                "isVerified": True,
                "isMxValid": True,
                "dnsRecords": {
                    "mxValid": True,
                    "ownershipValid": True,
                    "wildcardMxValid": False,
                    "wildcardMxRequired": True,
                },
            }
        )
        assert wild_missing is False
        assert wild_reason == "wildcard_mx_missing"
        for alias in ("verified", "ok", "healthy"):
            alias_ok, alias_reason = client.verify_ready({"status": alias})
            assert alias_ok is False
            assert alias_reason == "dns_propagating"
            result_ok, result_reason = client.verify_ready({"result": alias})
            assert result_ok is False
            assert result_reason == "dns_propagating"
        conflict_ok, conflict_reason = client.verify_ready({"result": "record_conflict"})
        assert conflict_ok is False
        assert conflict_reason == "record_conflict"
    finally:
        client.close()


def test_extract_quota_plan_name_not_display_name():
    me = {
        "user": {
            "id": "u",
            "displayName": "小辣椒",
            "plan": {"name": "Max", "maxWildcardRules": 15, "maxDomains": 50},
        },
        "isAdmin": False,
        "quota": {
            "maxWildcardRules": 15,
            "usedWildcardRules": 4,
            "maxDomains": 50,
            "usedDomains": 12,
        },
    }
    quota = extract_quota(me, {}, [], [])
    assert quota["plan_name"] == "Max"
    assert quota["max_wildcard"] == 15
    assert quota["used_wildcard"] == 4
