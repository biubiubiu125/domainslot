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
    assert quota["max_wildcard"] is None
    assert quota["max_domains"] is None
    assert quota["used_wildcard"] == 2
    assert quota["used_domains"] == 2


def test_verify_ready_from_dns_status():
    client = YydsClient("https://example.invalid/v1", "u", "p")
    try:
        ok, reason = client.verify_ready({"result": "receiving_ready"})
        assert ok is True
        assert reason == "receiving_ready"
        pending, pending_reason = client.verify_ready({"result": "dns_propagating"})
        assert pending is False
        assert pending_reason == "dns_propagating"
    finally:
        client.close()
