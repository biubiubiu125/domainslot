import json
from datetime import datetime, timedelta, timezone

from app.db.models import Domain, EventLog, YydsAccount, YydsDomainSnapshot
from app.worker.logic import STATUS_ERROR, STATUS_UNUSED, STATUS_USED
from app.worker.poll_yyds import poll_one_yyds_account
from app.yyds.client import YydsDomain, YydsError


class FakeYyds:
    def __init__(self, *, rules=None, rules_error=False, quota=None, domains=None):
        self.access_token = "t"
        self._rules = rules
        self._rules_error = rules_error
        self._quota = quota if quota is not None else {"wildcardRules": {"used": 1, "max": 5}, "domains": {"used": 1, "max": 50}}
        self._domains = domains if domains is not None else [YydsDomain(id="yd1", domain="a.com", is_public=False, verification_token=None, raw={})]

    def close(self):
        return None

    def export_cookies(self):
        return []

    def ensure_session(self):
        return None

    def get_me(self):
        return {"user": {"plan": {"name": "Max", "maxWildcardRules": 5, "maxDomains": 50}}}

    def get_quota(self):
        return self._quota

    def list_domains(self):
        return list(self._domains)

    def list_wildcard_rules(self):
        if self._rules_error:
            raise YydsError("wildcard missing", 500, {})
        return list(self._rules if self._rules is not None else [{}])


def _account(session):
    account = YydsAccount(
        name="y1",
        username="user",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
    )
    session.add(account)
    session.commit()
    return account


def test_poll_yyds_empty_rules_uses_quota_when_list_shorter(db_session, settings, monkeypatch):
    account = _account(db_session)
    fake = FakeYyds(
        rules=[],
        quota={"wildcardRules": {"used": 4, "max": 5}, "domains": {"used": 4, "max": 50}},
        domains=[],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    assert account.used_wildcard == 4
    assert account.used_domains == -1


def test_poll_yyds_unknown_rules_uses_quota_used(db_session, settings, monkeypatch):
    account = _account(db_session)
    fake = FakeYyds(rules_error=True, quota={"wildcardRules": {"used": 4, "max": 5}, "domains": {"used": 4, "max": 50}})
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    assert account.max_wildcard == 5
    assert account.used_wildcard == 4


def test_poll_yyds_unknown_rules_and_used_treats_max_as_full(db_session, settings, monkeypatch):
    account = _account(db_session)
    fake = FakeYyds(rules_error=True, quota={"wildcardRules": {"max": 5}, "domains": {"max": 50}})
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    assert account.max_wildcard == 5
    assert account.used_wildcard == 5


def test_poll_yyds_keeps_known_max_when_quota_missing(db_session, settings, monkeypatch):
    account = _account(db_session)
    account.max_wildcard = 5
    account.max_domains = 50
    account.used_wildcard = 2
    account.used_domains = 2
    account.plan_name = "Max"
    db_session.commit()
    fake = FakeYyds(rules_error=True, quota={})
    fake.get_me = lambda: {"user": {"id": "u"}}  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    assert account.max_wildcard == 5
    assert account.max_domains == 50
    assert account.plan_name == "Max"
    assert account.used_wildcard == 5
    assert account.used_domains == 1


def test_poll_yyds_commits_before_releasing_session_lock(db_session, settings, monkeypatch):
    account = _account(db_session)
    fake = FakeYyds()
    order: list[str] = []
    orig_commit = db_session.commit

    def tracking_commit():
        order.append("commit")
        orig_commit()

    def tracking_release(lock):
        order.append("release")

    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    monkeypatch.setattr(db_session, "commit", tracking_commit)
    monkeypatch.setattr("app.worker.poll_yyds.release_yyds_session_lock", tracking_release)
    poll_one_yyds_account(db_session, settings, account)
    assert "commit" in order
    assert "release" in order
    assert order.index("commit") < order.index("release")


def test_poll_yyds_429_sets_throttle(db_session, settings, monkeypatch):
    account = _account(db_session)

    class Boom:
        def close(self):
            return None

        def export_cookies(self):
            return []

        def ensure_session(self):
            raise YydsError("rate", 429, {})

    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: Boom())
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    assert account.throttle_until is not None
    until = account.throttle_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    assert until > datetime.now(timezone.utc)
    assert (account.throttle_backoff_seconds or 0) >= 120
    assert not account.login_error


def test_poll_yyds_429_uses_retry_after(db_session, settings, monkeypatch):
    account = _account(db_session)
    now = datetime.now(timezone.utc)

    class Boom:
        def close(self):
            return None

        def export_cookies(self):
            return []

        def ensure_session(self):
            error = YydsError("rate", 429, {})
            error.retry_after_seconds = 7
            raise error

    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: Boom())
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    until = account.throttle_until
    assert until is not None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    delta = (until - now).total_seconds()
    assert 4 <= delta <= 15
    assert not account.login_error


def test_poll_yyds_skips_while_throttled(db_session, settings, monkeypatch):
    account = _account(db_session)
    account.throttle_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.commit()
    called = {"n": 0}

    class Boom:
        def close(self):
            return None

        def ensure_session(self):
            called["n"] += 1
            raise AssertionError("throttled account should skip login")

    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: Boom())
    poll_one_yyds_account(db_session, settings, account)
    assert called["n"] == 0


def test_poll_yyds_unknown_list_does_not_shrink(db_session, settings, monkeypatch):
    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com"]')
    row = Domain(
        name="a.com",
        display_name="a.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd1",
    )
    db_session.add_all([snap, row])
    db_session.commit()

    class Fake:
        def close(self):
            return None

        def export_cookies(self):
            return []

        def ensure_session(self):
            return None

        def get_me(self):
            return {"user": {"plan": {"name": "Max", "maxWildcardRules": 5, "maxDomains": 50}}}

        def get_quota(self):
            return {"wildcardRules": {"used": 1, "max": 5}, "domains": {"used": 1, "max": 50}}

        def list_domains(self):
            raise YydsError("无法解析列表响应")

        def list_wildcard_rules(self):
            return [{}]

    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: Fake())
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(snap)
    db_session.refresh(row)
    assert json.loads(snap.names_json) == ["a.com"]
    assert row.status == STATUS_USED
    assert row.yyds_domain_id == "yd1"
    assert account.login_error


def test_poll_yyds_quota_ahead_of_list_does_not_shrink(db_session, settings, monkeypatch):
    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com", "b.com"]')
    row_a = Domain(
        name="a.com",
        display_name="a.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd1",
    )
    row_b = Domain(
        name="b.com",
        display_name="b.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd2",
    )
    db_session.add_all([snap, row_a, row_b])
    db_session.commit()
    fake = FakeYyds(
        rules=[{}, {}],
        quota={"wildcardRules": {"used": 2, "max": 5}, "domains": {"used": 2, "max": 50}},
        domains=[YydsDomain(id="yd1", domain="a.com", is_public=False, verification_token=None, raw={})],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(snap)
    db_session.refresh(row_b)
    db_session.refresh(account)
    assert shrunk is False
    assert json.loads(snap.names_json) == ["a.com", "b.com"]
    assert row_b.status == STATUS_USED
    assert row_b.yyds_domain_id == "yd2"
    assert account.used_domains == -1
    assert not account.login_error


def test_poll_yyds_removed_clears_bind_and_keeps_used(db_session, settings, monkeypatch):
    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com", "gone.com"]')
    kept = Domain(
        name="a.com",
        display_name="a.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd1",
    )
    gone = Domain(
        name="gone.com",
        display_name="gone.com",
        status=STATUS_ERROR,
        error_reason="上次验证失败",
        yyds_account_id=account.id,
        yyds_domain_id="yd2",
        filling_at=datetime.now(timezone.utc),
    )
    db_session.add_all([snap, kept, gone])
    db_session.commit()
    fake = FakeYyds(
        rules=[{}],
        quota={"wildcardRules": {"used": 1, "max": 5}, "domains": {"used": 1, "max": 50}},
        domains=[YydsDomain(id="yd1", domain="a.com", is_public=False, verification_token=None, raw={})],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(gone)
    db_session.refresh(snap)
    assert shrunk is True
    assert json.loads(snap.names_json) == ["a.com"]
    assert gone.status == STATUS_USED
    assert gone.yyds_account_id is None
    assert gone.yyds_domain_id is None
    assert gone.filling_at is None


def test_poll_yyds_empty_list_quota_failed_does_not_shrink(db_session, settings, monkeypatch):
    from sqlalchemy import select

    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com"]')
    row = Domain(
        name="a.com",
        display_name="a.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd1",
    )
    db_session.add_all([snap, row])
    db_session.commit()

    class BoomQuota(FakeYyds):
        def get_quota(self):
            raise YydsError("quota down", 500, {})

    fake = BoomQuota(rules=[], quota={}, domains=[])
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(snap)
    db_session.refresh(row)
    db_session.refresh(account)
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert shrunk is False
    assert json.loads(snap.names_json) == ["a.com"]
    assert row.status == STATUS_USED
    assert row.yyds_domain_id == "yd1"
    assert row.yyds_account_id == account.id
    assert "yyds_list_unreliable" in codes or "yyds_list_short" in codes
    assert account.used_domains != 0 or account.used_domains == -1


def test_poll_yyds_empty_list_quota_zero_does_shrink(db_session, settings, monkeypatch):
    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com"]')
    row = Domain(
        name="a.com",
        display_name="a.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd1",
    )
    db_session.add_all([snap, row])
    db_session.commit()
    fake = FakeYyds(
        rules=[],
        quota={"wildcardRules": {"used": 0, "max": 5}, "domains": {"used": 0, "max": 50}},
        domains=[],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(snap)
    db_session.refresh(row)
    assert shrunk is True
    assert json.loads(snap.names_json) == []
    assert row.status == STATUS_USED
    assert row.yyds_account_id is None
    assert row.yyds_domain_id is None


def test_poll_yyds_unlimited_unknown_occupancy_does_not_look_empty(db_session, settings, monkeypatch):
    account = _account(db_session)
    fake = FakeYyds(rules_error=True, quota={})
    fake.get_me = lambda: {"maxWildcardRules": -1, "maxDomains": -1}  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    assert account.max_wildcard == -1
    assert account.used_wildcard == -1


def test_poll_yyds_unreliable_event_not_repeated(db_session, settings, monkeypatch):
    from sqlalchemy import select

    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com"]')
    row = Domain(
        name="a.com",
        display_name="a.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd1",
    )
    db_session.add_all([snap, row])
    db_session.commit()

    class BoomQuota(FakeYyds):
        def get_quota(self):
            raise YydsError("quota down", 500, {})

    fake = BoomQuota(rules=[], quota={}, domains=[])
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert codes.count("yyds_list_unreliable") + codes.count("yyds_list_short") == 1


def test_poll_yyds_removed_keeps_manual_unused(db_session, settings, monkeypatch):
    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["gone.com"]')
    gone = Domain(
        name="gone.com",
        display_name="gone.com",
        status=STATUS_UNUSED,
        yyds_account_id=account.id,
        yyds_domain_id="yd2",
    )
    db_session.add_all([snap, gone])
    db_session.commit()
    fake = FakeYyds(
        rules=[],
        quota={"wildcardRules": {"used": 0, "max": 5}, "domains": {"used": 0, "max": 50}},
        domains=[],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(gone)
    from sqlalchemy import select

    messages = [item.message for item in db_session.scalars(select(EventLog)).all() if item.code == "yyds_removed"]
    assert shrunk is True
    assert gone.status == STATUS_UNUSED
    assert gone.yyds_account_id is None
    assert gone.yyds_domain_id is None
    assert messages
    assert any("保持未使用" in text for text in messages)
    assert all("保持已使用" not in text for text in messages)


def test_poll_yyds_first_poll_short_list_does_not_snapshot(db_session, settings, monkeypatch):
    from sqlalchemy import select

    account = _account(db_session)
    fake = FakeYyds(
        rules=[],
        quota={"wildcardRules": {"used": 4, "max": 5}, "domains": {"used": 4, "max": 50}},
        domains=[],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    snap = db_session.scalar(select(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id == account.id))
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert shrunk is False
    assert snap is None
    assert account.first_synced_at is None
    assert account.used_domains == -1
    assert account.used_wildcard == 4
    assert "yyds_list_short" in codes


def test_poll_yyds_short_list_known_quota_is_not_fillable(db_session, settings, monkeypatch):
    from sqlalchemy import select

    from app.worker.fill import _account_views
    from app.worker.logic import select_fill_account

    account = _account(db_session)
    fake = FakeYyds(
        rules=[],
        quota={"wildcardRules": {"used": 4, "max": 5}, "domains": {"used": 4, "max": 50}},
        domains=[],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert account.used_domains == -1
    assert "yyds_list_short" in codes
    assert select_fill_account(_account_views(db_session)) is None


def test_poll_yyds_first_poll_empty_unknown_quota_does_not_snapshot(db_session, settings, monkeypatch):
    from sqlalchemy import select

    from app.worker.fill import _account_views
    from app.worker.logic import select_fill_account

    account = _account(db_session)
    fake = FakeYyds(rules=[], quota={}, domains=[])
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    snap = db_session.scalar(select(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id == account.id))
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert shrunk is False
    assert snap is None
    assert account.first_synced_at is None
    assert account.used_domains == -1
    assert account.used_wildcard == -1
    assert "yyds_list_unreliable" in codes
    assert select_fill_account(_account_views(db_session)) is None


def test_poll_yyds_shorter_list_unknown_quota_does_not_shrink(db_session, settings, monkeypatch):
    from sqlalchemy import select

    from app.worker.fill import _account_views
    from app.worker.logic import select_fill_account

    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com", "b.com"]')
    row_a = Domain(
        name="a.com",
        display_name="a.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd1",
    )
    row_b = Domain(
        name="b.com",
        display_name="b.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd2",
    )
    db_session.add_all([snap, row_a, row_b])
    db_session.commit()
    fake = FakeYyds(
        rules=[{}],
        quota={},
        domains=[YydsDomain(id="yd1", domain="a.com", is_public=False, verification_token=None, raw={})],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(snap)
    db_session.refresh(row_b)
    db_session.refresh(account)
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    messages = [item.message for item in db_session.scalars(select(EventLog)).all() if item.code == "yyds_list_unreliable"]
    assert shrunk is False
    assert json.loads(snap.names_json) == ["a.com", "b.com"]
    assert row_b.status == STATUS_USED
    assert row_b.yyds_account_id == account.id
    assert row_b.yyds_domain_id == "yd2"
    assert account.used_domains == -1
    assert not account.login_error
    assert "yyds_removed" not in codes
    assert "yyds_list_unreliable" in codes
    assert any("配额占用未知" in text for text in messages)
    assert all("列表为空" not in text for text in messages)
    assert select_fill_account(_account_views(db_session)) is None


def test_poll_yyds_same_count_missing_name_unknown_quota_does_not_shrink(db_session, settings, monkeypatch):
    from sqlalchemy import select

    account = _account(db_session)
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com", "b.com"]')
    row_b = Domain(
        name="b.com",
        display_name="b.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd2",
    )
    db_session.add_all([snap, row_b])
    db_session.commit()
    fake = FakeYyds(
        rules=[{}, {}],
        quota={},
        domains=[
            YydsDomain(id="yd1", domain="a.com", is_public=False, verification_token=None, raw={}),
            YydsDomain(id="yd3", domain="c.com", is_public=False, verification_token=None, raw={}),
        ],
    )
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(snap)
    db_session.refresh(row_b)
    db_session.refresh(account)
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert shrunk is False
    assert json.loads(snap.names_json) == ["a.com", "b.com"]
    assert row_b.status == STATUS_USED
    assert row_b.yyds_account_id == account.id
    assert row_b.yyds_domain_id == "yd2"
    assert account.used_domains == -1
    assert not account.login_error
    assert "yyds_removed" not in codes
    assert "yyds_list_unreliable" in codes


def test_poll_yyds_list_incomplete_does_not_look_like_login_failed(db_session, settings, monkeypatch):
    from sqlalchemy import select

    from app.worker.fill import _account_views
    from app.worker.logic import select_fill_account

    account = _account(db_session)
    account.used_domains = 1
    account.max_domains = 50
    account.used_wildcard = 1
    account.max_wildcard = 5
    snap = YydsDomainSnapshot(yyds_account_id=account.id, names_json='["a.com"]')
    row = Domain(
        name="a.com",
        display_name="a.com",
        status=STATUS_USED,
        yyds_account_id=account.id,
        yyds_domain_id="yd1",
    )
    db_session.add_all([snap, row])
    db_session.commit()

    class Truncated(FakeYyds):
        def list_domains(self):
            raise YydsError("列表不完整，拒绝按截断结果对账")

    fake = Truncated()
    monkeypatch.setattr("app.worker.poll_yyds.yyds_client", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.worker.poll_yyds.persist_yyds_session", lambda *_a, **_k: None)
    shrunk = poll_one_yyds_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(snap)
    db_session.refresh(row)
    db_session.refresh(account)
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert shrunk is False
    assert json.loads(snap.names_json) == ["a.com"]
    assert row.status == STATUS_USED
    assert row.yyds_domain_id == "yd1"
    assert not account.login_error
    assert account.used_domains == -1
    assert "yyds_login_failed" not in codes
    assert "yyds_list_unreliable" in codes
    assert select_fill_account(_account_views(db_session)) is None
