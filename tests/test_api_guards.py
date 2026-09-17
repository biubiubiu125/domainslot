import os
import uuid

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import sessionmaker

from app.api import _aliyun_dict, _yyds_dict, overview, worker_health_view
from app.db.models import AliyunAccount, Base, Domain, YydsAccount, YydsDomainSnapshot
from app.worker.logic import STATUS_UNUSED
from app.security import LoginGate


PG_URL = os.environ.get("DOMAINSLOT_PG_TEST_URL", "")


def test_healthz_requires_live_worker(db_session, monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app.api import healthz

    monkeypatch.setattr(
        "app.api.worker_status",
        lambda: {"alive": "no", "last_cycle": None, "last_error": None},
    )
    with pytest.raises(HTTPException) as exc:
        healthz(session=db_session)
    assert exc.value.status_code == 503

    monkeypatch.setattr(
        "app.api.worker_status",
        lambda: {"alive": "yes", "last_cycle": "t", "last_error": "Bearer eyJabc.def"},
    )
    body = healthz(session=db_session)
    assert body["ok"] is True
    assert body["worker"]["alive"] == "yes"
    assert body["worker"]["last_cycle"] == "t"
    assert "last_error" not in body["worker"]
    assert "eyJabc" not in str(body)
    assert "oldest_yyds_success_at" in body["poll"]
    assert "oldest_aliyun_success_at" in body["poll"]


def test_health_omits_worker_last_error():
    view = worker_health_view(
        {"alive": "yes", "last_cycle": "2026-01-01T00:00:00+00:00", "last_error": "Bearer eyJabc.def"}
    )
    assert view["alive"] == "yes"
    assert view["last_cycle"]
    assert "last_error" not in view
    assert "eyJabc" not in str(view)


def test_login_gate_blocks_after_failures():
    gate = LoginGate(max_fails=3, window_seconds=60)
    now = 1_000.0
    assert gate.allow("1.1.1.1", now=now) is True
    gate.fail("1.1.1.1", now=now)
    gate.fail("1.1.1.1", now=now + 1)
    gate.fail("1.1.1.1", now=now + 2)
    assert gate.allow("1.1.1.1", now=now + 3) is False
    assert gate.allow("2.2.2.2", now=now + 3) is True
    gate.success("1.1.1.1")
    assert gate.allow("1.1.1.1", now=now + 4) is True


def test_yyds_panel_domains_come_from_snapshot(db_session):
    account = YydsAccount(
        name="y1",
        username="user",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
        max_wildcard=5,
        used_wildcard=1,
    )
    db_session.add(account)
    db_session.flush()
    db_session.add(YydsDomainSnapshot(yyds_account_id=account.id, names_json='["only-on-yyds.com", "New.COM"]'))
    db_session.commit()
    data = _yyds_dict(account, include_domains=True, session=db_session)
    assert data["domains"] == ["new.com", "only-on-yyds.com"]


def test_yyds_panel_unknown_occupancy_is_not_full(db_session):
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
    db_session.add(account)
    db_session.commit()
    data = _yyds_dict(account)
    assert data["wildcard_full"] is False
    assert data["max_wildcard"] == -1
    assert data["used_wildcard"] == -1


def test_overview_unknown_occupancy_does_not_look_fillable(db_session):
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
        login_error=None,
    )
    db_session.add(account)
    db_session.flush()
    db_session.add(
        Domain(
            name="new.com",
            display_name="new.com",
            status=STATUS_UNUSED,
            nameservers="dns9.hichina.com",
        )
    )
    db_session.commit()
    data = overview(db_session)
    assert any("配额未知" in str(item) for item in data["alerts"])
    assert not any("已满" in str(item) for item in data["alerts"])
    assert not any("有空位" in str(item) for item in data["alerts"])


def test_yyds_panel_never_synced_zero_used_is_unknown(db_session):
    account = YydsAccount(
        name="y1",
        username="user",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
        max_wildcard=None,
        used_wildcard=0,
        max_domains=None,
        used_domains=0,
        last_success_at=None,
    )
    db_session.add(account)
    db_session.commit()
    data = _yyds_dict(account)
    assert data["used_wildcard"] is None
    assert data["used_domains"] is None
    assert data["wildcard_full"] is False
    assert data["max_wildcard"] is None


def test_yyds_panel_unknown_quota_is_not_full(db_session):
    account = YydsAccount(
        name="y1",
        username="user",
        password_enc="enc",
        receive_enabled=True,
        enabled=True,
        max_wildcard=None,
        used_wildcard=0,
        max_domains=None,
        used_domains=0,
    )
    db_session.add(account)
    db_session.commit()
    data = _yyds_dict(account)
    assert data["wildcard_full"] is False
    assert data["max_wildcard"] is None


def test_poll_health_view_throttled_yyds_is_not_ok(db_session):
    from datetime import datetime, timedelta, timezone

    from app.api import poll_health_view

    now = datetime.now(timezone.utc)
    db_session.add(
        YydsAccount(
            name="y1",
            username="user",
            password_enc="enc",
            last_success_at=now,
            login_error=None,
            throttle_until=now + timedelta(minutes=10),
        )
    )
    db_session.commit()
    view = poll_health_view(db_session)
    assert view["yyds_total"] == 1
    assert view["yyds_ok"] == 0
    assert view["oldest_yyds_success_at"] is None


def test_poll_health_view_omits_errors(db_session):
    from datetime import datetime, timezone

    from app.api import poll_health_view
    from app.db.models import AliyunAccount

    now = datetime.now(timezone.utc)
    db_session.add(
        YydsAccount(
            name="y1",
            username="user",
            password_enc="enc",
            last_success_at=now,
            login_error=None,
        )
    )
    db_session.add(
        AliyunAccount(
            name="ak1",
            access_key_id="LTAIxxxx",
            access_key_secret_enc="enc",
            last_success_at=now,
            last_error="Bearer eyJabc.def",
        )
    )
    db_session.commit()
    view = poll_health_view(db_session)
    assert view["yyds_total"] == 1
    assert view["yyds_ok"] == 1
    assert view["aliyun_total"] == 1
    assert view["aliyun_ok"] == 0
    assert "last_error" not in view
    assert "eyJabc" not in str(view)
    assert view["oldest_yyds_success_at"]


def test_overview_shows_redacted_worker_last_error(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.api.worker_status",
        lambda: {"alive": "yes", "last_cycle": "t", "last_error": "[redacted] cycle boom"},
    )
    data = overview(session=db_session, _=None)
    assert data["worker"]["last_error"] == "[redacted] cycle boom"
    assert any("工作线程" in item and "cycle boom" in item for item in data["alerts"])


def test_aliyun_dict_hides_full_access_key():
    row = AliyunAccount(name="ak", access_key_id="LTAI1234567890abcd", access_key_secret_enc="enc")
    data = _aliyun_dict(row)
    assert data.get("access_key_id") != "LTAI1234567890abcd"
    assert "LTAI1234567890abcd" not in str(data)
    assert "****" in data["access_key_id_masked"]


def test_create_aliyun_encrypts_access_key_id(db_session, settings, monkeypatch):
    from sqlalchemy import select

    from app.api import AliyunBody, create_aliyun
    from app.crypto import decrypt_text

    monkeypatch.setattr("app.api.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.request_scan", lambda: None)
    create_aliyun(
        AliyunBody(name="ak", access_key_id="LTAI1234567890abcd", access_key_secret="s3cret"),
        session=db_session,
        _=None,
    )
    row = db_session.scalars(select(AliyunAccount)).one()
    assert row.access_key_id != "LTAI1234567890abcd"
    assert decrypt_text(settings.secret_key, row.access_key_id) == "LTAI1234567890abcd"
    data = _aliyun_dict(row)
    assert "LTAI1234567890abcd" not in str(data)
    assert data["access_key_id_masked"].endswith("abcd")
    assert "gAAAA" not in data["access_key_id_masked"]


def test_aliyun_client_decrypts_access_key_id(settings, monkeypatch):
    from app.crypto import encrypt_text
    from app.worker.clients import aliyun_client

    captured: dict[str, str] = {}

    class FakeAliyunClient:
        def __init__(self, access_key_id: str, access_key_secret: str):
            captured["id"] = access_key_id
            captured["secret"] = access_key_secret

    monkeypatch.setattr("app.worker.clients.AliyunClient", FakeAliyunClient)
    account = AliyunAccount(
        name="ak",
        access_key_id=encrypt_text(settings.secret_key, "LTAI1234567890abcd"),
        access_key_secret_enc=encrypt_text(settings.secret_key, "s3cret"),
    )
    aliyun_client(settings, account)
    assert captured["id"] == "LTAI1234567890abcd"
    assert captured["secret"] == "s3cret"


def test_poll_health_view_empty_list_warning_keeps_aliyun_ok(db_session):
    from datetime import datetime, timezone

    from app.api import poll_health_view

    now = datetime.now(timezone.utc)
    db_session.add(
        AliyunAccount(
            name="ak1",
            access_key_id="LTAIxxxx",
            access_key_secret_enc="enc",
            last_success_at=now,
            last_error="阿里云列表为空，已有库存未改",
        )
    )
    db_session.commit()
    view = poll_health_view(db_session)
    assert view["aliyun_total"] == 1
    assert view["aliyun_ok"] == 1


def test_overview_unused_not_ready_does_not_look_fillable(db_session):
    db_session.add(
        YydsAccount(
            name="y1",
            username="user",
            password_enc="enc",
            receive_enabled=True,
            enabled=True,
            max_wildcard=5,
            used_wildcard=1,
            max_domains=50,
            used_domains=1,
            login_error=None,
        )
    )
    db_session.add(
        Domain(
            name="old.com",
            display_name="old.com",
            status=STATUS_UNUSED,
            domain_status="2",
            audit_status="SUCCEED",
            nameservers="dns9.hichina.com",
        )
    )
    db_session.commit()
    data = overview(db_session)
    assert any("都不能补位" in str(item) for item in data["alerts"])
    assert not any("有空位但没有未使用" in str(item) for item in data["alerts"])
    assert not any("yyds 已满" in str(item) for item in data["alerts"])


def test_overview_throttled_aliyun_unused_does_not_look_fillable(db_session):
    from datetime import datetime, timedelta, timezone

    aliyun = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        throttle_until=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(aliyun)
    db_session.flush()
    db_session.add(
        YydsAccount(
            name="y1",
            username="user",
            password_enc="enc",
            receive_enabled=True,
            enabled=True,
            max_wildcard=5,
            used_wildcard=1,
            max_domains=50,
            used_domains=1,
            login_error=None,
        )
    )
    db_session.add(
        Domain(
            name="ready.com",
            display_name="ready.com",
            status=STATUS_UNUSED,
            domain_status="3",
            audit_status="SUCCEED",
            nameservers="dns9.hichina.com",
            aliyun_account_id=aliyun.id,
        )
    )
    db_session.commit()
    data = overview(db_session)
    assert any("都不能补位" in str(item) for item in data["alerts"])


def test_overview_empty_list_warning_is_not_alert(db_session):
    from datetime import datetime, timezone

    from app.worker.logic import ALIYUN_EMPTY_LIST_WARNING

    row = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        last_success_at=datetime.now(timezone.utc),
        last_error=ALIYUN_EMPTY_LIST_WARNING,
    )
    db_session.add(row)
    db_session.commit()
    data = overview(db_session)
    assert not any(ALIYUN_EMPTY_LIST_WARNING in str(item) for item in data["alerts"])
    assert _aliyun_dict(row)["last_error"] is None


def test_create_yyds_stores_twofa_code(db_session, settings, monkeypatch):
    from sqlalchemy import select

    from app.api import YydsBody, create_yyds, _yyds_dict
    from app.crypto import decrypt_text

    monkeypatch.setattr("app.api.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.request_scan", lambda: None)
    create_yyds(
        YydsBody(name="y1", username="user", password="pw", twofa_code="123456"),
        session=db_session,
        _=None,
    )
    row = db_session.scalars(select(YydsAccount)).one()
    assert row.twofa_code_enc
    assert decrypt_text(settings.secret_key, row.twofa_code_enc) == "123456"
    assert "123456" not in str(_yyds_dict(row))


def test_yyds_client_passes_twofa_code(settings, monkeypatch):
    from app.crypto import encrypt_text
    from app.worker.clients import yyds_client

    captured: dict[str, object] = {}

    class FakeYydsClient:
        def __init__(
            self,
            api_base,
            username,
            password,
            cookies=None,
            access_token=None,
            timeout=30,
            session_key=None,
            on_session_change=None,
            twofa_code=None,
            turnstile_token=None,
        ):
            captured["twofa_code"] = twofa_code
            captured["password"] = password
            self.on_session_change = on_session_change
            self.access_token = access_token or ""
            self.export_cookies = lambda: []

    monkeypatch.setattr("app.worker.clients.YydsClient", FakeYydsClient)
    account = YydsAccount(
        name="y1",
        username="user",
        password_enc=encrypt_text(settings.secret_key, "pw"),
        twofa_code_enc=encrypt_text(settings.secret_key, "654321"),
    )
    yyds_client(settings, account)
    assert captured["twofa_code"] == "654321"
    assert captured["password"] == "pw"


def test_persist_yyds_session_keeps_unused_twofa_code(settings):
    from app.crypto import encrypt_text
    from app.worker.clients import persist_yyds_session

    class FakeClient:
        access_token = "tok"
        twofa_consumed = False

        def export_cookies(self):
            return [{"name": "sid", "value": "v"}]

    account = YydsAccount(
        name="y1",
        username="user",
        password_enc=encrypt_text(settings.secret_key, "pw"),
        twofa_code_enc=encrypt_text(settings.secret_key, "654321"),
    )
    persist_yyds_session(settings, account, FakeClient())
    assert account.twofa_code_enc is not None


def test_persist_yyds_session_clears_consumed_twofa_code(settings):
    from app.crypto import encrypt_text
    from app.worker.clients import persist_yyds_session

    class FakeClient:
        access_token = "tok"
        twofa_consumed = True

        def export_cookies(self):
            return [{"name": "sid", "value": "v"}]

    account = YydsAccount(
        name="y1",
        username="user",
        password_enc=encrypt_text(settings.secret_key, "pw"),
        twofa_code_enc=encrypt_text(settings.secret_key, "654321"),
    )
    persist_yyds_session(settings, account, FakeClient())
    assert account.twofa_code_enc is None


def test_yyds_client_bad_cookies_fall_back_to_empty(settings, monkeypatch):
    from app.crypto import encrypt_text
    from app.worker.clients import yyds_client

    captured: dict[str, object] = {}

    class FakeYydsClient:
        def __init__(self, api_base, username, password, cookies=None, access_token=None, timeout=30, session_key=None, on_session_change=None, twofa_code=None, turnstile_token=None):
            captured["password"] = password
            captured["cookies"] = cookies
            captured["access_token"] = access_token
            self.on_session_change = on_session_change
            self.access_token = access_token or ""

    monkeypatch.setattr("app.worker.clients.YydsClient", FakeYydsClient)
    account = YydsAccount(
        name="y1",
        username="user",
        password_enc=encrypt_text(settings.secret_key, "pw"),
        cookies_enc="not-a-fernet-token",
        access_token_enc="also-bad",
    )
    yyds_client(settings, account)
    assert captured["password"] == "pw"
    assert captured["cookies"] == []
    assert captured["access_token"] in (None, "")


def test_delete_aliyun_requests_scan(db_session, monkeypatch):
    from app.api import delete_aliyun
    from app.db.models import AliyunAccount

    calls = {"n": 0}
    monkeypatch.setattr("app.api.request_scan", lambda: calls.__setitem__("n", calls["n"] + 1))
    row = AliyunAccount(name="ak", access_key_id="id", access_key_secret_enc="enc")
    db_session.add(row)
    db_session.flush()
    delete_aliyun(row.id, session=db_session, _=None)
    assert calls["n"] == 1
    assert row in db_session.deleted


def test_delete_yyds_requests_scan(db_session, monkeypatch):
    from app.api import delete_yyds
    from app.db.models import YydsAccount

    calls = {"n": 0}
    monkeypatch.setattr("app.api.request_scan", lambda: calls.__setitem__("n", calls["n"] + 1))
    row = YydsAccount(name="y1", username="user", password_enc="enc")
    db_session.add(row)
    db_session.flush()
    delete_yyds(row.id, session=db_session, _=None)
    assert calls["n"] == 1
    assert row in db_session.deleted


def test_delete_yyds_marks_bound_unused_as_used(db_session, monkeypatch):
    from app.api import delete_yyds
    from app.worker.logic import STATUS_UNUSED, STATUS_USED

    monkeypatch.setattr("app.api.request_scan", lambda: None)
    account = YydsAccount(name="y1", username="user", password_enc="enc")
    other = YydsAccount(name="y2", username="user2", password_enc="enc")
    db_session.add_all([account, other])
    db_session.flush()
    bound = Domain(
        name="bound.com",
        display_name="bound.com",
        status=STATUS_UNUSED,
        yyds_account_id=account.id,
        yyds_domain_id="yd-bound",
    )
    unbound = Domain(
        name="free.com",
        display_name="free.com",
        status=STATUS_UNUSED,
        yyds_account_id=account.id,
    )
    other_bound = Domain(
        name="other.com",
        display_name="other.com",
        status=STATUS_UNUSED,
        yyds_account_id=other.id,
        yyds_domain_id="yd-other",
    )
    db_session.add_all([bound, unbound, other_bound])
    db_session.commit()
    delete_yyds(account.id, session=db_session, _=None)
    db_session.commit()
    db_session.refresh(bound)
    db_session.refresh(unbound)
    db_session.refresh(other_bound)
    assert db_session.get(YydsAccount, account.id) is None
    assert bound.status == STATUS_USED
    assert bound.yyds_account_id is None
    assert bound.yyds_domain_id == "yd-bound"
    assert unbound.status == STATUS_UNUSED
    assert unbound.yyds_account_id is None
    assert other_bound.status == STATUS_UNUSED
    assert other_bound.yyds_account_id == other.id


def test_delete_yyds_commits_when_snapshot_exists(db_session, monkeypatch):
    from app.api import delete_yyds
    from app.worker.fill import _occupying_yyds_account
    from app.worker.logic import STATUS_UNUSED, STATUS_USED

    monkeypatch.setattr("app.api.request_scan", lambda: None)
    account = YydsAccount(name="y1", username="user", password_enc="enc")
    other = YydsAccount(name="y2", username="user2", password_enc="enc")
    db_session.add_all([account, other])
    db_session.flush()
    bound = Domain(
        name="bound.com",
        display_name="bound.com",
        status=STATUS_UNUSED,
        yyds_account_id=account.id,
        yyds_domain_id="yd-bound",
    )
    db_session.add(bound)
    db_session.add(YydsDomainSnapshot(yyds_account_id=account.id, names_json='["bound.com"]'))
    other_snap = YydsDomainSnapshot(yyds_account_id=other.id, names_json='["keep.com"]')
    db_session.add(other_snap)
    db_session.commit()
    db_session.expire_all()

    delete_yyds(account.id, session=db_session, _=None)
    db_session.commit()

    assert db_session.get(YydsAccount, account.id) is None
    leftover = list(
        db_session.scalars(select(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id == account.id))
    )
    assert leftover == []
    remaining = db_session.scalars(select(YydsDomainSnapshot)).all()
    assert [item.yyds_account_id for item in remaining] == [other.id]
    db_session.refresh(bound)
    assert bound.status == STATUS_USED
    assert bound.yyds_account_id is None
    assert bound.yyds_domain_id == "yd-bound"
    assert _occupying_yyds_account(db_session, "bound.com") is None
    assert _occupying_yyds_account(db_session, "keep.com") == other.id


@pytest.mark.skipif(not PG_URL, reason="no DOMAINSLOT_PG_TEST_URL")
def test_pg_delete_yyds_commits_when_snapshot_exists(monkeypatch):
    from app.api import delete_yyds
    from app.worker.fill import _occupying_yyds_account
    from app.worker.logic import STATUS_UNUSED, STATUS_USED

    from app.db.session import SCHEMA_STATEMENTS

    monkeypatch.setattr("app.api.request_scan", lambda: None)
    engine = create_engine(PG_URL, pool_pre_ping=True, future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for sql in SCHEMA_STATEMENTS:
            conn.execute(text(sql))
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    suffix = uuid.uuid4().hex[:8]
    account_id = None
    other_id = None
    bound_id = None
    try:
        account = YydsAccount(name=f"y1-{suffix}", username=f"user-{suffix}", password_enc="enc")
        other = YydsAccount(name=f"y2-{suffix}", username=f"user2-{suffix}", password_enc="enc")
        session.add_all([account, other])
        session.flush()
        account_id = account.id
        other_id = other.id
        bound = Domain(
            name=f"bound-{suffix}.com",
            display_name=f"bound-{suffix}.com",
            status=STATUS_UNUSED,
            yyds_account_id=account.id,
            yyds_domain_id="yd-bound",
        )
        session.add(bound)
        session.flush()
        bound_id = bound.id
        session.add(YydsDomainSnapshot(yyds_account_id=account.id, names_json=f'["bound-{suffix}.com"]'))
        other_snap = YydsDomainSnapshot(
            yyds_account_id=other.id,
            names_json=f'["keep-{suffix}.com"]',
        )
        session.add(other_snap)
        session.commit()
        session.expire_all()

        delete_yyds(account.id, session=session, _=None)
        session.commit()

        assert session.get(YydsAccount, account.id) is None
        leftover = list(
            session.scalars(select(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id == account.id))
        )
        assert leftover == []
        remaining = session.get(YydsDomainSnapshot, other_snap.id)
        assert remaining is not None
        session.refresh(bound)
        assert bound.status == STATUS_USED
        assert bound.yyds_account_id is None
        assert bound.yyds_domain_id == "yd-bound"
        assert _occupying_yyds_account(session, bound.name) is None
        assert _occupying_yyds_account(session, f"keep-{suffix}.com") == other.id
    finally:
        session.rollback()
        ids = [pk for pk in (account_id, other_id) if pk is not None]
        if ids:
            session.execute(delete(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id.in_(ids)))
            session.execute(delete(YydsAccount).where(YydsAccount.id.in_(ids)))
        if bound_id is not None:
            session.execute(delete(Domain).where(Domain.id == bound_id))
        session.commit()
        session.close()
        engine.dispose()
