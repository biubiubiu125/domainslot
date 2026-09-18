from datetime import datetime, timezone

import pytest

from app.aliyun.client import AliyunError
from app.db.models import AliyunAccount, Domain, EventLog, YydsAccount, YydsDomainSnapshot
from app.worker.delete_yyds import YydsDomainDeleteError, delete_bound_yyds_domain
from app.worker.logic import STATUS_ERROR, STATUS_UNUSED, STATUS_USED
from app.yyds.client import YydsError


def _seed(session, *, status=STATUS_USED, filling=False, bound=True, with_aliyun=True, error_reason=None):
    now = datetime.now(timezone.utc)
    aliyun = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
    )
    yyds = YydsAccount(
        name="y1",
        username="user",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
        used_wildcard=1,
        used_domains=1,
    )
    session.add_all([aliyun, yyds])
    session.flush()
    if error_reason is None and status == STATUS_ERROR:
        error_reason = "old-error"
    domain = Domain(
        name="new.com",
        display_name="new.com",
        aliyun_account_id=aliyun.id if with_aliyun else None,
        yyds_account_id=yyds.id if bound else None,
        yyds_domain_id="yd1" if bound else None,
        status=status,
        filling_at=now if filling else None,
        error_reason=error_reason,
    )
    session.add(domain)
    session.add(YydsDomainSnapshot(yyds_account_id=yyds.id, names_json='["new.com","keep.com"]'))
    session.commit()
    return aliyun, yyds, domain


class FakeYyds:
    def __init__(self):
        self.deleted = []
        self.guides = []
        self.access_token = "t"
        self.fail_status = None

    def close(self):
        return None

    def export_cookies(self):
        return []

    def dns_guide(self, domain_id: str):
        self.guides.append(domain_id)
        return {
            "records": [
                {"type": "TXT", "name": "_yydsmail-verify.new.com", "value": "tok"},
                {"type": "MX", "name": "new.com", "value": "mx.215.im", "priority": 10},
                {"type": "MX", "name": "*.new.com", "value": "mx.215.im", "priority": 10},
            ]
        }

    def delete_domain(self, domain_id: str):
        self.deleted.append(domain_id)
        if self.fail_status == 404:
            raise YydsError("not found HTTP 404", 404, {"errorCode": "not_found"})
        if self.fail_status:
            raise YydsError(f"blocked HTTP {self.fail_status}", self.fail_status, {"errorCode": "blocked"})
        return {"ok": True}


class FakeAliyun:
    def __init__(self):
        self.cleaned = []
        self.wanted = None
        self.fail = False

    def delete_mailbox_records(self, domain: str, wanted=None):
        if self.fail:
            raise AliyunError("阿里云接口限流", code="Throttling.User", throttled=True)
        self.cleaned.append(domain)
        self.wanted = wanted
        return {"deleted": 3}


def _patch_clients(monkeypatch, fake_yyds, fake_aliyun):
    monkeypatch.setattr("app.worker.delete_yyds.yyds_client", lambda *_a, **_k: fake_yyds)
    monkeypatch.setattr("app.worker.delete_yyds.aliyun_client", lambda *_a, **_k: fake_aliyun)
    monkeypatch.setattr("app.worker.delete_yyds.persist_yyds_session", lambda *_a, **_k: None)


def test_delete_bound_yyds_domain_cleans_dns_and_unbinds(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    row = delete_bound_yyds_domain(db_session, settings, domain.id)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    snap = db_session.query(YydsDomainSnapshot).one()
    assert fake_yyds.guides == ["yd1"]
    assert fake_yyds.deleted == ["yd1"]
    assert fake_aliyun.cleaned == ["new.com"]
    assert fake_aliyun.wanted
    assert row.status == STATUS_USED
    assert domain.yyds_account_id is None
    assert domain.yyds_domain_id is None
    assert domain.filling_at is None
    assert domain.error_reason is None
    assert "new.com" not in snap.names_json
    assert "keep.com" in snap.names_json
    events = list(db_session.query(EventLog).all())
    assert any(item.code == "yyds_domain_deleted" for item in events)
    assert yyds.used_wildcard == 0
    assert yyds.used_domains == 0


def test_delete_bound_yyds_404_still_cleans_dns(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session, status=STATUS_ERROR)
    fake_yyds = FakeYyds()
    fake_yyds.fail_status = 404
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    delete_bound_yyds_domain(db_session, settings, domain.id)
    db_session.refresh(domain)
    db_session.refresh(yyds)
    assert fake_yyds.deleted == ["yd1"]
    assert fake_aliyun.cleaned == ["new.com"]
    assert domain.status == STATUS_USED
    assert domain.yyds_account_id is None
    assert domain.error_reason is None
    assert yyds.used_wildcard == 1
    assert yyds.used_domains == 1


def test_delete_bound_yyds_409_does_not_touch_dns(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.fail_status = 409
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    with pytest.raises(YydsDomainDeleteError, match="409"):
        delete_bound_yyds_domain(db_session, settings, domain.id)
    db_session.refresh(domain)
    assert fake_aliyun.cleaned == []
    assert domain.yyds_domain_id == "yd1"
    assert domain.yyds_account_id is not None
    assert domain.status == STATUS_USED


def test_delete_bound_yyds_dns_failure_still_unbinds(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_aliyun = FakeAliyun()
    fake_aliyun.fail = True
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    delete_bound_yyds_domain(db_session, settings, domain.id)
    db_session.refresh(domain)
    assert fake_yyds.deleted == ["yd1"]
    assert domain.yyds_account_id is None
    assert domain.yyds_domain_id is None
    assert domain.status == STATUS_USED
    assert "yyds 已删除，阿里云解析清理失败" in (domain.error_reason or "")


def test_delete_unbound_domain_rejected(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, bound=False)
    _patch_clients(monkeypatch, FakeYyds(), FakeAliyun())
    with pytest.raises(YydsDomainDeleteError, match="未绑定"):
        delete_bound_yyds_domain(db_session, settings, domain.id)


def test_delete_filling_domain_rejected(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, filling=True)
    fake_yyds = FakeYyds()
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    with pytest.raises(YydsDomainDeleteError, match="补位"):
        delete_bound_yyds_domain(db_session, settings, domain.id)
    assert fake_yyds.deleted == []
    assert fake_aliyun.cleaned == []


def test_delete_domain_from_yyds_api_requests_scan(db_session, settings, monkeypatch):
    from app.api import delete_domain_from_yyds

    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    monkeypatch.setattr("app.api.get_settings", lambda: settings)
    calls = {"n": 0}
    monkeypatch.setattr("app.api.request_scan", lambda: calls.__setitem__("n", calls["n"] + 1))
    payload = delete_domain_from_yyds(domain.id, session=db_session, _=None)
    assert calls["n"] == 1
    assert payload["yyds_account_id"] is None
    assert payload["status"] == STATUS_USED
    assert fake_yyds.deleted == ["yd1"]
    assert fake_aliyun.cleaned == ["new.com"]


def test_delete_yyds_401_is_not_http_401(db_session, settings, monkeypatch):
    from fastapi import HTTPException

    from app.api import delete_domain_from_yyds

    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.fail_status = 401
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    monkeypatch.setattr("app.api.get_settings", lambda: settings)
    with pytest.raises(YydsDomainDeleteError) as worker_exc:
        delete_bound_yyds_domain(db_session, settings, domain.id)
    assert worker_exc.value.status_code == 502
    assert fake_aliyun.cleaned == []
    with pytest.raises(HTTPException) as api_exc:
        delete_domain_from_yyds(domain.id, session=db_session, _=None)
    assert api_exc.value.status_code == 502


def test_delete_yyds_403_maps_to_502(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_yyds.fail_status = 403
    _patch_clients(monkeypatch, fake_yyds, FakeAliyun())
    with pytest.raises(YydsDomainDeleteError) as exc:
        delete_bound_yyds_domain(db_session, settings, domain.id)
    assert exc.value.status_code == 502


def test_delete_without_aliyun_account_records_dns_error(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session, with_aliyun=False)
    fake_yyds = FakeYyds()
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    delete_bound_yyds_domain(db_session, settings, domain.id)
    db_session.refresh(domain)
    assert fake_yyds.deleted == ["yd1"]
    assert fake_aliyun.cleaned == []
    assert domain.yyds_account_id is None
    assert domain.status == STATUS_USED
    assert "阿里云账号不存在" in (domain.error_reason or "")


def test_delete_retries_dns_cleanup_after_unbind_failure(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(
        db_session,
        bound=False,
        error_reason="yyds 已删除，阿里云解析清理失败: 阿里云接口限流",
    )
    fake_yyds = FakeYyds()
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)
    delete_bound_yyds_domain(db_session, settings, domain.id)
    db_session.refresh(domain)
    assert fake_yyds.deleted == []
    assert fake_aliyun.cleaned == ["new.com"]
    assert domain.error_reason is None
    assert domain.status == STATUS_USED
    assert domain.yyds_account_id is None


def test_delete_rechecks_filling_after_lock(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    fake_yyds = FakeYyds()
    fake_aliyun = FakeAliyun()
    _patch_clients(monkeypatch, fake_yyds, fake_aliyun)

    def acquire(session):
        live = session.get(Domain, domain.id)
        live.filling_at = datetime.now(timezone.utc)
        session.flush()
        return None

    monkeypatch.setattr("app.worker.delete_yyds.acquire_fill_lock", acquire)
    with pytest.raises(YydsDomainDeleteError, match="补位"):
        delete_bound_yyds_domain(db_session, settings, domain.id)
    assert fake_yyds.deleted == []
    assert fake_aliyun.cleaned == []


def test_delete_commits_before_releasing_fill_lock(db_session, settings, monkeypatch):
    _aliyun, _yyds, domain = _seed(db_session)
    _patch_clients(monkeypatch, FakeYyds(), FakeAliyun())
    order: list[str] = []
    monkeypatch.setattr(
        "app.worker.delete_yyds.acquire_fill_lock",
        lambda session: order.append("lock") or "held",
    )
    monkeypatch.setattr(
        "app.worker.delete_yyds.release_fill_lock",
        lambda conn: order.append("unlock"),
    )
    original = db_session.commit

    def wrapped():
        order.append("commit")
        return original()

    monkeypatch.setattr(db_session, "commit", wrapped)
    delete_bound_yyds_domain(db_session, settings, domain.id)
    assert "commit" in order
    assert order.index("lock") < order.index("commit") < order.index("unlock")


def test_delete_acquires_yyds_session_lock(db_session, settings, monkeypatch):
    _aliyun, yyds, domain = _seed(db_session)
    _patch_clients(monkeypatch, FakeYyds(), FakeAliyun())
    locked: list[object] = []
    monkeypatch.setattr(
        "app.worker.delete_yyds.acquire_yyds_session_lock",
        lambda session, account_id: locked.append(("lock", account_id)) or "sess",
        raising=False,
    )
    monkeypatch.setattr(
        "app.worker.delete_yyds.release_yyds_session_lock",
        lambda conn: locked.append(("unlock", conn)),
        raising=False,
    )
    delete_bound_yyds_domain(db_session, settings, domain.id)
    assert locked == [("lock", yyds.id), ("unlock", "sess")]
