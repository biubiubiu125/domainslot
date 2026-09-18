import time
from datetime import datetime, timedelta, timezone

from app.db.models import AliyunAccount, Domain, YydsAccount, YydsDomainSnapshot
from app.aliyun.client import AliyunDomain, AliyunError
from app.worker.fill import _sync_yyds_usage, fill_available
from app.worker.logic import STATUS_ERROR, STATUS_UNUSED, STATUS_USED
from app.yyds.client import YydsClient, YydsDomain, YydsError


def _seed(session, *, domain_status="3", audit="SUCCEED", nameservers="dns9.hichina.com", unused=True):
    now = datetime.now(timezone.utc)
    aliyun = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    yyds = YydsAccount(
        name="y1",
        username="user",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
        max_wildcard=5,
        used_wildcard=0,
        max_domains=50,
        used_domains=0,
        login_error=None,
    )
    session.add_all([aliyun, yyds])
    session.flush()
    domain = Domain(
        name="new.com",
        display_name="new.com",
        aliyun_account_id=aliyun.id,
        status=STATUS_UNUSED if unused else STATUS_USED,
        domain_status=domain_status,
        audit_status=audit,
        nameservers=nameservers,
        registration_at=now - timedelta(days=3),
    )
    session.add(domain)
    session.commit()
    return aliyun, yyds, domain


class FakeYyds:
    def __init__(self):
        self.added = []
        self.ensured = []
        self.enabled = []
        self.access_token = "t"
        self._domains: dict[str, YydsDomain] = {}

    def close(self):
        return None

    def export_cookies(self):
        return []

    def ensure_session(self):
        return None

    def get_me(self):
        return {"user": {"plan": {"name": "Max", "maxWildcardRules": 5, "maxDomains": 50}}}

    def get_quota(self):
        used = len(self._domains)
        return {"wildcardRules": {"used": used, "max": 5}, "domains": {"used": used, "max": 50}}

    def list_domains(self):
        return list(self._domains.values())

    def list_wildcard_rules(self):
        return [{"id": "r1"}] if self._domains else []

    def add_domain(self, domain: str, enable_wildcard: bool = True):
        self.added.append(domain)
        item = YydsDomain(id="yd1", domain=domain, is_public=False, verification_token="tok", raw={})
        self._domains[domain] = item
        return {"id": "yd1", "domain": domain}

    def set_private(self, domain_id: str) -> bool:
        return True

    def ensure_wildcard_rule(self, domain_id: str) -> bool:
        self.ensured.append(domain_id)
        return True

    def enable_wildcard_rules(self, domain_id: str) -> list[str]:
        raise AssertionError("fill must not auto-enable wildcard rules")

    def dns_guide(self, domain_id: str):
        return {
            "records": [
                {"type": "TXT", "name": "_yydsmail-verify.new.com", "value": "tok"},
                {"type": "MX", "name": "new.com", "value": "mx.215.im", "priority": 10},
                {"type": "MX", "name": "*.new.com", "value": "mx.215.im", "priority": 10},
            ]
        }

    def verify_domain(self, domain_id: str):
        return {"result": "receiving_ready"}

    def dns_status(self, domain_id: str):
        return self.verify_domain(domain_id)

    def batch_verify(self, domain_ids):
        payload = self.verify_domain(domain_ids[0] if domain_ids else "")
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload
        result = "dns_propagating"
        if isinstance(payload, dict):
            result = str(payload.get("result") or result)
        return {
            "items": [
                {
                    "domainId": domain_ids[0] if domain_ids else "",
                    "domain": "new.com",
                    "result": result,
                }
            ]
        }

    def verify_ready(self, payload):
        helper = YydsClient("https://example.invalid/v1", "u", "p")
        try:
            return helper.verify_ready(payload)
        finally:
            helper.close()


class FakeAliyun:
    def __init__(self):
        self.described: list[str] = []
        self.live_nameservers = ["dns9.hichina.com", "dns10.hichina.com"]
        self.applied: list[tuple[str, object]] = []
        self.live_status = "3"
        self.live_audit = "SUCCEED"

    def describe_registrar_nameservers(self, domain: str):
        self.described.append(domain)
        return list(self.live_nameservers)

    def describe_registrar_domain(self, domain: str):
        self.described.append(domain)
        item = AliyunDomain(
            name=domain,
            domain_status=getattr(self, "live_status", None),
            audit_status=getattr(self, "live_audit", None),
            nameservers=list(self.live_nameservers),
        )
        item.client_hold = bool(getattr(self, "live_client_hold", False))
        return item

    def apply_guide(self, domain: str, wanted):
        self.applied.append((domain, wanted))
        return {"deleted": 1, "added": 3}


def test_fill_does_not_add_redeemed_domain(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session, domain_status="2")
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_status = "2"
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert did is False
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert "赎回" in (domain.error_reason or "")
    assert domain.yyds_domain_id is None
    assert "new.com" not in dns.described


def test_fill_does_not_add_when_ns_not_aliyun(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, nameservers="bob.ns.cloudflare.com")
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_nameservers = ["bob.ns.cloudflare.com"]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert "NS" in (domain.error_reason or "")
    assert domain.yyds_domain_id is None
    assert "new.com" not in dns.described


def test_fill_skips_name_already_on_other_yyds_snapshot(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    other = YydsAccount(
        name="y2",
        username="other",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
        max_wildcard=5,
        used_wildcard=0,
        sort_order=50,
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(YydsDomainSnapshot(yyds_account_id=other.id, names_json='["new.com"]'))
    db_session.commit()
    fake_yyds = FakeYyds()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_USED
    assert domain.yyds_account_id == other.id


def test_verify_fail_after_add_counts_wildcard_usage(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()

    fake_yyds.verify_domain = lambda _domain_id: {"result": "record_conflict"}  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert fake_yyds.added == ["new.com"]
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert yyds.used_wildcard == 1
    assert yyds.used_domains == 1


def test_repair_unused_bound_does_not_add_again(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_UNUSED
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert dns.applied == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id == "yd1"


def test_recent_filling_under_two_minutes_is_repaired(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_UNUSED
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.filling_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert dns.applied
    assert domain.status == STATUS_USED
    assert domain.yyds_domain_id == "yd1"
    assert domain.filling_at is None


def test_stale_filling_after_two_minutes_is_repaired(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_UNUSED
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.filling_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_USED
    assert dns.applied
    assert domain.filling_at is None


def test_stale_filling_unused_bound_is_repaired(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_UNUSED
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.filling_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_USED
    assert dns.applied


def test_fill_does_not_use_stale_audit_when_live_audit_empty(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, audit="SUCCEED")
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_audit = None
    dns.live_status = "3"
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id is None
    assert "审核" in (domain.error_reason or "") or "实名" in (domain.error_reason or "")
    assert "new.com" in dns.described


def test_repair_empty_live_audit_still_repairs(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session, audit="FAILED")
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "yyds 验证待生效: dns_propagating"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    dns.live_audit = None
    dns.live_status = "3"
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert dns.applied == []
    assert domain.status == STATUS_USED
    assert domain.yyds_domain_id == "yd1"


def test_fill_stale_failed_audit_waits_for_poll(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, audit="FAILED")
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_audit = "SUCCEED"
    dns.live_status = "3"
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED
    assert "new.com" not in dns.described


def test_fill_live_redeem_marks_error(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, domain_status="3", audit="SUCCEED")
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_status = "2"
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is True
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert "赎回" in (domain.error_reason or "")
    assert "new.com" in dns.described


def test_fill_live_ns_not_aliyun_marks_error(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, nameservers="dns9.hichina.com")
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_nameservers = ["bob.ns.cloudflare.com"]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert "NS" in (domain.error_reason or "")
    assert "new.com" in dns.described


def test_fill_matches_yyds_name_case_and_does_not_readd(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds._domains["New.COM"] = YydsDomain(id="yd-case", domain="New.COM", is_public=False, verification_token="tok", raw={})
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.yyds_domain_id == "yd-case"
    assert domain.status == STATUS_USED


def test_fill_skips_unused_with_leftover_yyds_domain_id(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    domain.yyds_domain_id = "yd-old"
    db_session.commit()
    fake_yyds = FakeYyds()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is False
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id == "yd-old"
    assert domain.yyds_account_id is None


def test_fill_does_not_sleep_during_verify_retries(db_session, settings, monkeypatch):
    settings.verify_attempts = 12
    settings.verify_retry_seconds = 15
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.verify_domain = lambda _domain_id: {"result": "dns_propagating"}  # type: ignore[method-assign]
    slept = {"n": 0}
    fake_yyds.batch_calls = []
    original_batch = fake_yyds.batch_verify

    def counted_batch(domain_ids):
        fake_yyds.batch_calls.append(list(domain_ids))
        return original_batch(domain_ids)

    fake_yyds.batch_verify = counted_batch  # type: ignore[method-assign]

    def boom_sleep(_seconds):
        slept["n"] += 1
        raise AssertionError("fill must not sleep in the worker cycle")

    monkeypatch.setattr("time.sleep", boom_sleep)
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    started = time.monotonic()
    fill_available(db_session, settings)
    elapsed = time.monotonic() - started
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert slept["n"] == 0
    assert elapsed < 2
    assert fake_yyds.added == ["new.com"]
    assert fake_yyds.batch_calls == [["yd1"]]
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert "待生效" in (domain.error_reason or "")
    assert yyds.used_wildcard == 1


def test_dns_propagating_does_not_sleep_and_stays_bound(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.verify_domain = lambda _domain_id: {"result": "dns_propagating"}  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    started = time.monotonic()
    fill_available(db_session, settings)
    elapsed = time.monotonic() - started
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert fake_yyds.added == ["new.com"]
    assert elapsed < 2
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert yyds.used_wildcard == 1
    assert domain.filling_at is None
    assert "待生效" in (domain.error_reason or "")


def test_set_private_failure_does_not_continue_as_success(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.set_private = lambda _domain_id: False  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert fake_yyds.added == ["new.com"]
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert "私有" in (domain.error_reason or "")


def test_fill_refetches_registrar_ns_before_add(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, nameservers="dns9.hichina.com")
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_nameservers = ["bob.ns.cloudflare.com"]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert "new.com" in dns.described
    assert domain.status == STATUS_ERROR
    assert "NS" in (domain.error_reason or "")


def test_fill_commits_before_releasing_session_lock(db_session, settings, monkeypatch):
    from app.worker.clients import persist_yyds_session as real_persist

    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds._cookies = [{"name": "sid", "value": "old"}]
    fake_yyds.export_cookies = lambda: list(fake_yyds._cookies)  # type: ignore[method-assign]

    def verify_and_rotate(_domain_id: str):
        fake_yyds.access_token = "rotated-token"
        fake_yyds._cookies = [{"name": "sid", "value": "new"}]
        return {"result": "dns_propagating"}

    fake_yyds.verify_domain = verify_and_rotate  # type: ignore[method-assign]
    order: list[str] = []
    original_commit = db_session.commit

    def tracking_commit():
        order.append("commit")
        return original_commit()

    def tracking_persist(*args, **kwargs):
        order.append("persist")
        return real_persist(*args, **kwargs)

    def tracking_release(lock):
        order.append("release")

    db_session.commit = tracking_commit  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", tracking_persist)
    monkeypatch.setattr("app.worker.fill.release_yyds_session_lock", tracking_release)
    fill_available(db_session, settings)
    assert "persist" in order
    assert "release" in order
    last_persist = max(i for i, item in enumerate(order) if item == "persist")
    first_release = order.index("release")
    assert any(item == "commit" for item in order[last_persist:first_release]), order


def test_fill_refetches_live_registrar_status_before_add(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, domain_status="3", audit="SUCCEED")
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_status = "2"
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert "new.com" in dns.described
    assert domain.status == STATUS_ERROR
    assert "赎回" in (domain.error_reason or "")


def test_fill_rejects_guide_without_wildcard_mx(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.dns_guide = lambda _domain_id: {  # type: ignore[method-assign]
        "records": [
            {"type": "TXT", "name": "_yydsmail-verify.new.com", "value": "tok"},
            {"type": "MX", "name": "new.com", "value": "mx.215.im", "priority": 10},
        ]
    }
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == ["new.com"]
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert "通配" in (domain.error_reason or "")


def test_fill_retries_batch_verify_without_sleep(db_session, settings, monkeypatch):
    settings.verify_attempts = 3
    settings.verify_retry_seconds = 0
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.batch_calls = []

    def verify(_domain_id: str):
        return {"id": "yd1", "isVerified": True, "isMxValid": True}

    def batch(domain_ids):
        fake_yyds.batch_calls.append(list(domain_ids))
        return {"items": [{"domainId": domain_ids[0], "domain": "new.com", "result": "dns_propagating"}]}

    fake_yyds.verify_domain = verify  # type: ignore[method-assign]
    fake_yyds.batch_verify = batch  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    started = time.monotonic()
    fill_available(db_session, settings)
    elapsed = time.monotonic() - started
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert elapsed < 2
    assert fake_yyds.batch_calls == [["yd1"], ["yd1"], ["yd1"]]
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert yyds.used_wildcard == 1
    assert "待生效" in (domain.error_reason or "")


def test_fill_batch_verify_ready_after_retry_marks_used(db_session, settings, monkeypatch):
    settings.verify_attempts = 2
    settings.verify_retry_seconds = 0
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.verify_domain = lambda _domain_id: {"id": "yd1", "isVerified": True, "isMxValid": True}  # type: ignore[method-assign]
    calls = {"n": 0}

    def batch(domain_ids):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"items": [{"domainId": domain_ids[0], "domain": "new.com", "result": "dns_propagating"}]}
        return {"items": [{"domainId": domain_ids[0], "domain": "new.com", "result": "receiving_ready"}]}

    fake_yyds.batch_verify = batch  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == ["new.com"]
    assert domain.status == STATUS_USED
    assert domain.error_reason is None


def test_fill_does_not_add_client_hold(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    dns.live_client_hold = True
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert "new.com" in dns.described
    assert dns.applied == []
    assert domain.status == STATUS_ERROR
    assert "Hold" in (domain.error_reason or "")
    assert getattr(domain, "client_hold", False) is True


def test_stored_client_hold_unused_marked_error_without_describe(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    domain.client_hold = True
    db_session.commit()
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is False
    assert fake_yyds.added == []
    assert dns.described == []
    assert dns.applied == []
    assert domain.status == STATUS_ERROR
    assert "Hold" in (domain.error_reason or "")
    assert domain.yyds_domain_id is None


def test_add_domain_409_reuses_listed_domain(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    listed = {"ready": False}

    def list_domains():
        if not listed["ready"]:
            return []
        return [
            YydsDomain(
                id="yd-existing",
                domain="new.com",
                is_public=False,
                verification_token="tok",
                raw={},
            )
        ]

    def add_domain(name: str, enable_wildcard: bool = True):
        fake_yyds.added.append(name)
        listed["ready"] = True
        raise YydsError("already exists HTTP 409", 409, {"error": "already exists"})

    fake_yyds.list_domains = list_domains  # type: ignore[method-assign]
    fake_yyds.add_domain = add_domain  # type: ignore[method-assign]
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == ["new.com"]
    assert domain.status == STATUS_USED
    assert domain.yyds_domain_id == "yd-existing"
    assert domain.error_reason is None
    assert dns.applied != []


def test_add_domain_409_not_listed_marks_error(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()

    def add_domain(name: str, enable_wildcard: bool = True):
        fake_yyds.added.append(name)
        raise YydsError("already exists HTTP 409", 409, {"error": "already exists"})

    fake_yyds.add_domain = add_domain  # type: ignore[method-assign]
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == ["new.com"]
    assert dns.applied == []
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id is None
    assert "409" in (domain.error_reason or "")


def test_add_domain_403_marks_yyds_add_failure(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()

    def add_domain(name: str, enable_wildcard: bool = True):
        fake_yyds.added.append(name)
        raise YydsError("cross_origin_request_blocked HTTP 403", 403, {"errorCode": "cross_origin_request_blocked"})

    fake_yyds.add_domain = add_domain  # type: ignore[method-assign]
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == ["new.com"]
    assert dns.applied == []
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id is None
    assert domain.error_reason == "yyds 加域名失败: cross_origin_request_blocked HTTP 403"


def test_fill_creates_wildcard_rule_without_enabling(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == ["new.com"]
    assert dns.applied != []
    assert fake_yyds.ensured == ["yd1"]
    assert fake_yyds.enabled == []
    assert domain.status == STATUS_USED
    assert domain.error_reason is None


def test_repair_skips_when_live_registrar_redeemed(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "yyds 验证待生效: dns_propagating"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    dns.live_status = "2"
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert "new.com" in dns.described
    assert dns.applied == []
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert "赎回" in (domain.error_reason or "")


def test_repair_skips_client_hold(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "yyds 验证待生效: dns_propagating"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    dns.live_client_hold = True
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert "new.com" in dns.described
    assert dns.applied == []
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert "Hold" in (domain.error_reason or "")


def test_fill_domain_shape_without_wildcard_uses_batch_verify(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.verify_domain = lambda _domain_id: {"id": "yd1", "isVerified": True, "isMxValid": True}  # type: ignore[method-assign]
    fake_yyds.batch_verify = lambda domain_ids: {  # type: ignore[method-assign]
        "items": [{"domainId": domain_ids[0], "domain": "new.com", "result": "wildcard_mx_missing"}]
    }
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == ["new.com"]
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert "待生效" in (domain.error_reason or "") or "wildcard" in (domain.error_reason or "")


def test_fill_skips_throttled_yyds_account(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    yyds.throttle_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.commit()
    fake_yyds = FakeYyds()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is False
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED


def test_fill_429_before_add_keeps_unused_and_throttles(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()

    def boom():
        raise YydsError("rate", 429, {})

    fake_yyds.ensure_session = boom  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id is None
    assert yyds.throttle_until is not None
    until = yyds.throttle_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    assert until > datetime.now(timezone.utc)


def test_repair_skips_throttled_yyds_account(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "yyds 验证待生效: dns_propagating"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    yyds.throttle_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(
        id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={}
    )
    calls = {"n": 0}
    orig_ensure = fake_yyds.ensure_session

    def counted():
        calls["n"] += 1
        return orig_ensure()

    fake_yyds.ensure_session = counted  # type: ignore[method-assign]
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is False
    assert calls["n"] == 0
    assert dns.described == []
    assert dns.applied == []
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert domain.filling_at is None


def test_fill_dns_status_verified_alias_is_not_ready(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()

    def boom_batch(_domain_ids):
        raise YydsError("batch missing", 500, {})

    fake_yyds.batch_verify = boom_batch  # type: ignore[method-assign]
    fake_yyds.dns_status = lambda _domain_id: {"status": "verified"}  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == ["new.com"]
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert domain.status != STATUS_USED


def test_fill_batch_verify_429_throttles_and_does_not_mark_used(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    dns_status_calls = {"n": 0}

    def boom_batch(_domain_ids):
        raise YydsError("rate", 429, {})

    def ready_status(_domain_id):
        dns_status_calls["n"] += 1
        return {"result": "receiving_ready"}

    fake_yyds.batch_verify = boom_batch  # type: ignore[method-assign]
    fake_yyds.dns_status = ready_status  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert fake_yyds.added == ["new.com"]
    assert dns_status_calls["n"] == 0
    assert domain.status == STATUS_ERROR
    assert domain.status != STATUS_USED
    assert domain.yyds_domain_id == "yd1"
    assert yyds.throttle_until is not None
    until = yyds.throttle_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    assert until > datetime.now(timezone.utc)


def test_fill_does_not_add_when_wildcard_occupancy_unknown(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    yyds.max_wildcard = -1
    yyds.used_wildcard = -1
    yyds.max_domains = -1
    yyds.used_domains = -1
    db_session.commit()
    fake_yyds = FakeYyds()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is False
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id is None


def test_sync_usage_fallback_keeps_unknown_occupancy():
    account = YydsAccount(
        name="y1",
        username="user",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
        max_wildcard=-1,
        used_wildcard=-1,
        max_domains=-1,
        used_domains=-1,
    )

    class BrokenClient:
        def get_me(self):
            return {}

        def get_quota(self):
            return {}

        def list_wildcard_rules(self):
            raise YydsError("fail", 500, {})

        def list_domains(self):
            raise YydsError("fail", 500, {})

    _sync_yyds_usage(account, BrokenClient(), fallback_add=True)
    assert account.used_wildcard == -1
    assert account.used_domains == -1


def test_sync_yyds_usage_return_annotation_is_dict():
    import typing

    hints = typing.get_type_hints(_sync_yyds_usage)
    origin = typing.get_origin(hints["return"]) or hints["return"]
    assert origin is dict


def test_fill_skips_older_stored_not_ready_unused(db_session, settings, monkeypatch):
    aliyun, _yyds, ready = _seed(db_session)
    older = Domain(
        name="old.com",
        display_name="old.com",
        aliyun_account_id=aliyun.id,
        status=STATUS_UNUSED,
        domain_status="2",
        audit_status="SUCCEED",
        nameservers="dns9.hichina.com",
        registration_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    db_session.add(older)
    db_session.commit()
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(ready)
    db_session.refresh(older)
    assert fake_yyds.added == ["new.com"]
    assert ready.status == STATUS_USED
    assert older.status == STATUS_ERROR
    assert "赎回" in (older.error_reason or "")
    assert older.yyds_domain_id is None


def test_fill_already_listed_does_not_increment_occupancy_when_rules_fail(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    yyds.used_wildcard = 1
    yyds.used_domains = 1
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(
        id="yd-exist",
        domain="new.com",
        is_public=False,
        verification_token="tok",
        raw={},
    )

    def boom_rules():
        raise YydsError("rules down", 500, {})

    fake_yyds.list_wildcard_rules = boom_rules  # type: ignore[method-assign]
    fake_yyds.verify_domain = lambda _domain_id: {"result": "record_conflict"}  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd-exist"
    assert yyds.used_wildcard == 1
    assert yyds.used_domains == 1


def test_fill_skips_throttled_aliyun_account(db_session, settings, monkeypatch):
    aliyun, _yyds, domain = _seed(db_session)
    aliyun.throttle_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.commit()
    fake_yyds = FakeYyds()
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is False
    assert fake_yyds.added == []
    assert dns.described == []
    assert dns.applied == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id is None
    assert domain.error_reason is None


def test_fill_skips_throttled_aliyun_and_uses_other_account(db_session, settings, monkeypatch):
    throttled, _yyds, older = _seed(db_session)
    older.name = "old.com"
    older.display_name = "old.com"
    older.registration_at = datetime.now(timezone.utc) - timedelta(days=10)
    throttled.throttle_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.flush()
    other = AliyunAccount(
        name="ak2",
        access_key_id="LTAIyyyy",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    db_session.flush()
    newer = Domain(
        name="new.com",
        display_name="new.com",
        aliyun_account_id=other.id,
        status=STATUS_UNUSED,
        domain_status="3",
        audit_status="SUCCEED",
        nameservers="dns9.hichina.com",
        registration_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(newer)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds.dns_guide = lambda _domain_id: {  # type: ignore[method-assign]
        "records": [
            {"type": "TXT", "name": "_yydsmail-verify.new.com", "value": "tok"},
            {"type": "MX", "name": "new.com", "value": "mx.215.im", "priority": 10},
            {"type": "MX", "name": "*.new.com", "value": "mx.215.im", "priority": 10},
        ]
    }
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(older)
    db_session.refresh(newer)
    assert fake_yyds.added == ["new.com"]
    assert "old.com" not in dns.described
    assert older.status == STATUS_UNUSED
    assert older.yyds_domain_id is None
    assert newer.status == STATUS_USED
    assert newer.yyds_domain_id == "yd1"


def test_fill_describe_throttle_keeps_unused_and_throttles_aliyun(db_session, settings, monkeypatch):
    aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    dns = FakeAliyun()

    def boom(name: str):
        raise AliyunError("Throttling.User", code="Throttling.User", throttled=True)

    dns.describe_registrar_domain = boom  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(aliyun)
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id is None
    assert domain.error_reason is None
    assert aliyun.throttle_until is not None
    until = aliyun.throttle_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    assert until > datetime.now(timezone.utc)


def test_fill_apply_guide_throttle_after_add_marks_bound_error(db_session, settings, monkeypatch):
    aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    dns = FakeAliyun()

    def boom(name: str, wanted):
        raise AliyunError("Throttling.User", code="Throttling.User", throttled=True)

    dns.apply_guide = boom  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(aliyun)
    assert fake_yyds.added == ["new.com"]
    assert domain.status == STATUS_ERROR
    assert domain.status != STATUS_USED
    assert domain.yyds_domain_id == "yd1"
    assert "限流" in (domain.error_reason or "")
    assert aliyun.throttle_until is not None
    until = aliyun.throttle_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    assert until > datetime.now(timezone.utc)


def test_repair_skips_throttled_aliyun_account(db_session, settings, monkeypatch):
    aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "上次写解析失败"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    aliyun.throttle_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(
        id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={}
    )
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is False
    assert dns.described == []
    assert dns.applied == []
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert domain.yyds_domain_id == "yd1"
    assert domain.filling_at is None


def test_fill_aborts_when_live_wildcard_quota_full_even_if_list_empty(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    yyds.used_wildcard = 0
    yyds.max_wildcard = 5
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds.get_quota = lambda: {  # type: ignore[method-assign]
        "wildcardRules": {"used": 5, "max": 5},
        "domains": {"used": 5, "max": 50},
    }
    fake_yyds.list_wildcard_rules = lambda: []  # type: ignore[method-assign]
    fake_yyds.list_domains = lambda: []  # type: ignore[method-assign]
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id is None
    assert yyds.used_wildcard == 5


def test_fill_aborts_when_live_list_empty_even_if_quota_has_slot(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    yyds.used_wildcard = 0
    yyds.max_wildcard = 5
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds.get_quota = lambda: {  # type: ignore[method-assign]
        "wildcardRules": {"used": 4, "max": 5},
        "domains": {"used": 4, "max": 50},
    }
    fake_yyds.list_wildcard_rules = lambda: []  # type: ignore[method-assign]
    fake_yyds.list_domains = lambda: []  # type: ignore[method-assign]
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id is None
    assert domain.error_reason is None


def test_fill_unreliable_account_does_not_block_next_yyds(db_session, settings, monkeypatch):
    from app.worker.fill import _account_views
    from app.worker.logic import select_fill_account

    aliyun, first, domain = _seed(db_session)
    first.sort_order = 10
    second = YydsAccount(
        name="y2",
        username="user2",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
        sort_order=20,
        max_wildcard=5,
        used_wildcard=0,
        max_domains=50,
        used_domains=0,
        login_error=None,
    )
    db_session.add(second)
    db_session.commit()

    class Truncated(FakeYyds):
        def get_quota(self):
            return {"wildcardRules": {"used": 4, "max": 5}, "domains": {"used": 4, "max": 50}}

        def list_domains(self):
            return []

        def list_wildcard_rules(self):
            return []

    truncated = Truncated()
    healthy = FakeYyds()
    clients = {first.id: truncated, second.id: healthy}
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda _s, account, **_k: clients[account.id])
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)

    first_did = fill_available(db_session, settings)
    db_session.refresh(first)
    db_session.refresh(second)
    db_session.refresh(domain)
    assert first_did is True
    assert truncated.added == []
    assert healthy.added == []
    assert domain.status == STATUS_UNUSED
    assert first.used_domains == -1
    picked = select_fill_account(_account_views(db_session))
    assert picked is not None
    assert picked.id == str(second.id)

    second_did = fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(second)
    assert second_did is True
    assert truncated.added == []
    assert healthy.added == ["new.com"]
    assert domain.status == STATUS_USED
    assert domain.yyds_account_id == second.id


def test_fill_aborts_when_live_domain_list_raises(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.get_quota = lambda: {  # type: ignore[method-assign]
        "wildcardRules": {"used": 4, "max": 5},
        "domains": {"used": 4, "max": 50},
    }

    def boom_list():
        raise YydsError("列表不完整", 200, {"total": 4, "items": []})

    fake_yyds.list_domains = boom_list  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: FakeAliyun())
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id is None


def test_repair_skips_within_verify_retry_window(db_session, settings, monkeypatch):
    settings.verify_retry_seconds = 30
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "yyds 验证待生效: dns_propagating"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(
        id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={}
    )
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    did = fill_available(db_session, settings)
    db_session.refresh(domain)
    assert did is False
    assert dns.applied == []
    assert fake_yyds.added == []
    assert domain.status == STATUS_ERROR
    assert "待生效" in (domain.error_reason or "")


def test_repair_runs_after_verify_retry_window(db_session, settings, monkeypatch):
    settings.verify_retry_seconds = 15
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "yyds 验证待生效: dns_propagating"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(seconds=20)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(
        id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={}
    )
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert dns.applied == []
    assert domain.status == STATUS_USED
    assert domain.error_reason is None


def test_repair_dns_propagating_does_not_rewrite_records(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "yyds 验证待生效: dns_propagating"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert dns.applied == []
    assert domain.status == STATUS_USED
    assert domain.error_reason is None


def test_repair_txt_missing_still_rewrites_records(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    domain.status = STATUS_ERROR
    domain.yyds_account_id = yyds.id
    domain.yyds_domain_id = "yd1"
    domain.error_reason = "yyds 验证待生效: txt_missing"
    domain.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["new.com"] = YydsDomain(id="yd1", domain="new.com", is_public=False, verification_token="tok", raw={})
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    assert fake_yyds.added == []
    assert dns.applied
    assert domain.status == STATUS_USED


def test_fill_aborts_when_snapshot_names_missing_and_quota_unknown(db_session, settings, monkeypatch):
    from sqlalchemy import select

    from app.db.models import EventLog

    _aliyun, yyds, domain = _seed(db_session)
    db_session.add(YydsDomainSnapshot(yyds_account_id=yyds.id, names_json='["gone.com"]'))
    db_session.commit()
    fake_yyds = FakeYyds()
    fake_yyds._domains["keep.com"] = YydsDomain(
        id="yd-keep",
        domain="keep.com",
        is_public=False,
        verification_token="tok",
        raw={},
    )
    fake_yyds.get_quota = lambda: {}  # type: ignore[method-assign]
    dns = FakeAliyun()
    monkeypatch.setattr("app.worker.fill.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.fill.aliyun_client", lambda *_a, **_k: dns)
    monkeypatch.setattr("app.worker.fill.persist_yyds_session", lambda *_a, **_k: None)
    fill_available(db_session, settings)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert fake_yyds.added == []
    assert dns.applied == []
    assert domain.status == STATUS_UNUSED
    assert domain.yyds_domain_id is None
    assert domain.filling_at is None
    assert yyds.used_domains == -1
    assert "yyds_list_unreliable" in codes
