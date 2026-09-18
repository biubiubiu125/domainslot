from datetime import datetime, timedelta, timezone

from app.aliyun.client import AliyunDomain, AliyunError
from app.db.models import AliyunAccount, Domain, EventLog
from app.worker.logic import STATUS_ERROR, STATUS_UNUSED, STATUS_USED
from app.worker.poll_aliyun import poll_one_aliyun_account


def test_existing_unused_not_ready_marks_error(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="x.com",
        display_name="x.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        from_first_snapshot=False,
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="x.com",
                    domain_status="2",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_ERROR
    assert row.error_reason and "赎回" in row.error_reason


def test_first_sync_not_ready_marks_error(db_session, settings, monkeypatch):
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=None,
    )
    db_session.add(account)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="old.com",
                    domain_status="3",
                    audit_status="NONAUDIT",
                    nameservers=["dns9.hichina.com"],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    from sqlalchemy import select

    row = db_session.scalars(select(Domain)).one()
    assert row.from_first_snapshot is True
    assert row.status == STATUS_ERROR
    assert row.error_reason and "未实名" in row.error_reason
    assert account.first_synced_at is not None


def test_used_unbound_not_ready_marks_error(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="old.com",
        display_name="old.com",
        aliyun_account_id=account.id,
        status=STATUS_USED,
        from_first_snapshot=True,
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="old.com",
                    domain_status="2",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_ERROR
    assert "赎回" in (row.error_reason or "")


def test_used_bound_not_ready_stays_used(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    yyds_id = __import__("uuid").uuid4()
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="live.com",
        display_name="live.com",
        aliyun_account_id=account.id,
        yyds_account_id=yyds_id,
        yyds_domain_id="yd1",
        status=STATUS_USED,
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="live.com",
                    domain_status="3",
                    audit_status="NONAUDIT",
                    nameservers=["bob.ns.cloudflare.com"],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_USED


def test_unused_list_without_ns_stays_unused(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="ns.com",
        display_name="ns.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="ns.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=[],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_UNUSED
    assert row.nameservers == "dns9.hichina.com"
    assert row.error_reason is None


def test_first_sync_list_without_ns_marks_used(db_session, settings, monkeypatch):
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=None,
    )
    db_session.add(account)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="old.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=[],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    from sqlalchemy import select

    row = db_session.scalars(select(Domain)).one()
    assert row.status == STATUS_USED
    assert row.error_reason is None
    assert row.from_first_snapshot is True


def test_error_unbound_becomes_unused_when_ready(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="x.com",
        display_name="x.com",
        aliyun_account_id=account.id,
        status=STATUS_ERROR,
        from_first_snapshot=False,
        error_reason="域名处于赎回状态",
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="x.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_UNUSED
    assert row.error_reason is None


def test_error_first_snapshot_becomes_used_when_ready(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="old.com",
        display_name="old.com",
        aliyun_account_id=account.id,
        status=STATUS_ERROR,
        from_first_snapshot=True,
        error_reason="未实名或审核未通过: NONAUDIT",
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="old.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_USED
    assert row.error_reason is None


def test_yyds_fill_error_stays_when_first_snapshot_aliyun_ready(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="brzw.asia",
        display_name="brzw.asia",
        aliyun_account_id=account.id,
        status=STATUS_ERROR,
        from_first_snapshot=True,
        error_reason="yyds 加域名失败: cross_origin_request_blocked HTTP 403",
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="brzw.asia",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_ERROR
    assert row.error_reason == "yyds 加域名失败: cross_origin_request_blocked HTTP 403"


def test_yyds_fill_error_stays_when_new_domain_aliyun_ready(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="new.com",
        display_name="new.com",
        aliyun_account_id=account.id,
        status=STATUS_ERROR,
        from_first_snapshot=False,
        error_reason="yyds 加域名失败: cross_origin_request_blocked HTTP 403",
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="new.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_ERROR
    assert "403" in (row.error_reason or "")


def test_first_sync_client_hold_from_describe_marks_error(db_session, settings, monkeypatch):
    from sqlalchemy import select

    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=None,
    )
    db_session.add(account)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="hold.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            return AliyunDomain(
                name=name,
                domain_status="clientHold",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com", "dns10.hichina.com"],
                client_hold=True,
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    row = db_session.scalars(select(Domain)).one()
    assert described == ["hold.com"]
    assert row.status == STATUS_ERROR
    assert row.error_reason and "Hold" in row.error_reason
    assert row.from_first_snapshot is True
    assert row.domain_status == "clientHold"
    assert row.nameservers == "dns9.hichina.com,dns10.hichina.com"
    assert getattr(row, "client_hold", False) is True


def test_unused_client_hold_from_describe_marks_error(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="hold.com",
        display_name="hold.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        from_first_snapshot=False,
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="hold.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            return AliyunDomain(
                name=name,
                domain_status="clientHold",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com", "dns10.hichina.com"],
                client_hold=True,
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_ERROR
    assert row.error_reason and "Hold" in row.error_reason
    assert row.domain_status == "clientHold"
    assert row.nameservers == "dns9.hichina.com,dns10.hichina.com"
    assert getattr(row, "client_hold", False) is True


def test_used_bound_client_hold_from_describe_marks_error(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    yyds_id = __import__("uuid").uuid4()
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="live.com",
        display_name="live.com",
        aliyun_account_id=account.id,
        yyds_account_id=yyds_id,
        yyds_domain_id="yd1",
        status=STATUS_USED,
    )
    db_session.add(row)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="live.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            return AliyunDomain(
                name=name,
                domain_status="clientHold",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
                client_hold=True,
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert described == ["live.com"]
    assert row.status == STATUS_ERROR
    assert row.error_reason and "Hold" in row.error_reason
    assert getattr(row, "client_hold", False) is True


def test_used_bound_empty_live_audit_does_not_inherit_list_nonaudit(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    yyds_id = __import__("uuid").uuid4()
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="live.com",
        display_name="live.com",
        aliyun_account_id=account.id,
        yyds_account_id=yyds_id,
        yyds_domain_id="yd1",
        status=STATUS_USED,
    )
    db_session.add(row)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="live.com",
                    domain_status="3",
                    audit_status="NONAUDIT",
                    nameservers=["bob.ns.cloudflare.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status=None,
                nameservers=["dns9.hichina.com"],
                client_hold=False,
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert described == ["live.com"]
    assert row.status == STATUS_USED
    assert row.error_reason is None
    assert row.audit_status != "NONAUDIT"
    assert (row.nameservers or "").find("cloudflare") < 0


def test_used_bound_empty_live_ns_does_not_persist_list_cloudflare(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    yyds_id = __import__("uuid").uuid4()
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="live.com",
        display_name="live.com",
        aliyun_account_id=account.id,
        yyds_account_id=yyds_id,
        yyds_domain_id="yd1",
        status=STATUS_USED,
    )
    db_session.add(row)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="live.com",
                    domain_status="3",
                    audit_status="NONAUDIT",
                    nameservers=["bob.ns.cloudflare.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status=None,
                nameservers=[],
                client_hold=False,
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert described == ["live.com"]
    assert row.status == STATUS_USED
    assert row.error_reason is None
    assert row.audit_status != "NONAUDIT"
    assert (row.nameservers or "").find("cloudflare") < 0


def test_used_unbound_client_hold_from_describe_marks_error(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="hold.com",
        display_name="hold.com",
        aliyun_account_id=account.id,
        status=STATUS_USED,
        from_first_snapshot=True,
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="hold.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
                client_hold=True,
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert described == ["hold.com"]
    assert row.status == STATUS_ERROR
    assert row.error_reason and "Hold" in row.error_reason


def test_first_sync_describe_throttle_keeps_later_purchase_unused(db_session, settings, monkeypatch):
    from sqlalchemy import select

    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=None,
    )
    db_session.add(account)
    db_session.commit()

    class FirstClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="old1.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                ),
                AliyunDomain(
                    name="old2.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                ),
            ]

        def describe_registrar_domain(self, name: str):
            if name == "old2.com":
                raise AliyunError("Throttling.User", code="Throttling", throttled=True)
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FirstClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    assert account.first_synced_at is None
    names = {row.name: row for row in db_session.scalars(select(Domain)).all()}
    assert "old1.com" in names
    assert names["old1.com"].status == STATUS_USED
    assert names["old1.com"].from_first_snapshot is True

    class SecondClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="old1.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                ),
                AliyunDomain(
                    name="old2.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                ),
                AliyunDomain(
                    name="new3.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                ),
            ]

        def describe_registrar_domain(self, name: str):
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    account.throttle_until = None
    db_session.commit()
    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: SecondClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    names = {row.name: row for row in db_session.scalars(select(Domain)).all()}
    assert names["old2.com"].status == STATUS_USED
    assert names["old2.com"].from_first_snapshot is True
    assert names["new3.com"].status == STATUS_UNUSED
    assert names["new3.com"].from_first_snapshot is False
    assert account.first_synced_at is not None


def test_describe_crash_skips_new_domain(db_session, settings, monkeypatch):
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
        first_sync_names='["old.com"]',
    )
    db_session.add(account)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="new.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            raise RuntimeError("sdk boom")

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    names = [row.name for row in db_session.scalars(select(Domain)).all()]
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert described == ["new.com"]
    assert "new.com" not in names
    assert "aliyun_describe_failed" in codes


def test_bound_unused_describes_registrar(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    yyds_id = __import__("uuid").uuid4()
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="hold.com",
        display_name="hold.com",
        aliyun_account_id=account.id,
        yyds_account_id=yyds_id,
        yyds_domain_id="yd1",
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="hold.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            return AliyunDomain(
                name=name,
                domain_status="clientHold",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
                client_hold=True,
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert described == ["hold.com"]
    assert row.status == STATUS_ERROR
    assert row.error_reason and "Hold" in row.error_reason


def _bound_used_row(session, account, name="live.com"):
    yyds_id = __import__("uuid").uuid4()
    row = Domain(
        name=name,
        display_name=name,
        aliyun_account_id=account.id,
        yyds_account_id=yyds_id,
        yyds_domain_id="yd1",
        status=STATUS_USED,
        nameservers="dns9.hichina.com",
    )
    session.add(row)
    session.commit()
    return row


def test_used_bound_live_redeem_marks_error(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = _bound_used_row(db_session, account)

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="live.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

        def describe_registrar_domain(self, name: str):
            return AliyunDomain(
                name=name,
                domain_status="2",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_ERROR
    assert "赎回" in (row.error_reason or "")
    assert row.yyds_domain_id == "yd1"


def test_used_bound_live_ns_not_aliyun_marks_error(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = _bound_used_row(db_session, account)

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="live.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

        def describe_registrar_domain(self, name: str):
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["bob.ns.cloudflare.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_ERROR
    assert "NS" in (row.error_reason or "")
    assert row.yyds_domain_id == "yd1"


def test_used_bound_live_nonaudit_marks_error(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = _bound_used_row(db_session, account)

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="live.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

        def describe_registrar_domain(self, name: str):
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="NONAUDIT",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_ERROR
    assert "未实名" in (row.error_reason or "")
    assert row.yyds_domain_id == "yd1"


def test_unused_missing_from_nonempty_list_marks_error(db_session, settings, monkeypatch):
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    kept = Domain(
        name="keep.com",
        display_name="keep.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    gone = Domain(
        name="gone.com",
        display_name="gone.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    db_session.add_all([kept, gone])
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="keep.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

        def describe_registrar_domain(self, name: str):
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(kept)
    db_session.refresh(gone)
    names = {row.name for row in db_session.scalars(select(Domain)).all()}
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert "gone.com" in names
    assert kept.status == STATUS_UNUSED
    assert gone.status == STATUS_ERROR
    assert "不存在" in (gone.error_reason or "")
    assert "aliyun_missing" in codes


def test_used_bound_missing_from_nonempty_list_stays_used(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = _bound_used_row(db_session, account, name="bound.com")
    other = Domain(
        name="keep.com",
        display_name="keep.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    db_session.add(other)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="keep.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

        def describe_registrar_domain(self, name: str):
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert row.status == STATUS_USED
    assert row.yyds_account_id is not None
    assert row.yyds_domain_id == "yd1"


def test_empty_aliyun_list_does_not_mark_existing_unused_missing(db_session, settings, monkeypatch):
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="keep.com",
        display_name="keep.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return []

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert row.status == STATUS_UNUSED
    assert row.error_reason is None
    assert "aliyun_list_unreliable" in codes
    assert "aliyun_missing" not in codes


def test_describe_throttle_does_not_mark_unlisted_unused_missing(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    listed = Domain(
        name="keep.com",
        display_name="keep.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    gone = Domain(
        name="gone.com",
        display_name="gone.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    db_session.add_all([listed, gone])
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="keep.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

        def describe_registrar_domain(self, name: str):
            raise AliyunError("Throttling.User", code="Throttling", throttled=True)

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(gone)
    db_session.refresh(listed)
    assert gone.status == STATUS_UNUSED
    assert listed.status == STATUS_UNUSED


def test_describe_throttle_next_poll_skips_already_checked_domains(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    first = _bound_used_row(db_session, account, name="a.com")
    second = _bound_used_row(db_session, account, name="b.com")
    described: list[str] = []
    throttle_b = {"once": True}

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="a.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                ),
                AliyunDomain(
                    name="b.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                ),
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            if name == "b.com" and throttle_b["once"]:
                throttle_b["once"] = False
                raise AliyunError("Throttling.User", code="Throttling", throttled=True)
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    db_session.refresh(first)
    assert described[0] == "a.com"
    assert "b.com" in described
    assert getattr(first, "last_registrar_checked_at", None) is not None
    assert account.last_success_at is None

    described.clear()
    account.throttle_until = None
    db_session.commit()
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(second)
    db_session.refresh(account)
    assert described == ["b.com"]
    assert getattr(second, "last_registrar_checked_at", None) is not None
    assert account.last_success_at is not None


def test_skip_describe_does_not_clobber_live_audit_with_list(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    first = _bound_used_row(db_session, account, name="a.com")
    _bound_used_row(db_session, account, name="b.com")
    throttle_b = {"once": True}

    class FakeClient:
        def list_domains(self):
            audit = "SUCCEED" if throttle_b["once"] else "NONAUDIT"
            return [
                AliyunDomain(
                    name="a.com",
                    domain_status="3",
                    audit_status=audit,
                    nameservers=["dns9.hichina.com"],
                ),
                AliyunDomain(
                    name="b.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                ),
            ]

        def describe_registrar_domain(self, name: str):
            if name == "b.com" and throttle_b["once"]:
                throttle_b["once"] = False
                raise AliyunError("Throttling.User", code="Throttling", throttled=True)
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(first)
    assert first.audit_status == "SUCCEED"
    account.throttle_until = None
    db_session.commit()
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(first)
    assert first.audit_status == "SUCCEED"
    assert first.status == STATUS_USED


def test_empty_aliyun_list_does_not_repeat_unreliable_event(db_session, settings, monkeypatch):
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="keep.com",
        display_name="keep.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
    )
    db_session.add(row)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return []

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert codes.count("aliyun_list_unreliable") == 1


def test_first_empty_aliyun_list_does_not_complete_first_sync(db_session, settings, monkeypatch):
    from sqlalchemy import select

    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=None,
        first_sync_names=None,
    )
    db_session.add(account)
    db_session.commit()

    class FakeClient:
        def list_domains(self):
            return []

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    codes = [item.code for item in db_session.scalars(select(EventLog)).all()]
    assert account.first_synced_at is None
    assert account.first_sync_names is None
    assert "aliyun_first_sync" not in codes
    assert db_session.scalars(select(Domain)).first() is None


def test_domains_after_empty_first_poll_are_first_snapshot_used(db_session, settings, monkeypatch):
    from sqlalchemy import select

    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=None,
    )
    db_session.add(account)
    db_session.commit()
    listed = {"items": []}

    class FakeClient:
        def list_domains(self):
            return list(listed["items"])

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    listed["items"] = [
        AliyunDomain(
            name="later.com",
            domain_status="3",
            audit_status="SUCCEED",
            nameservers=["dns9.hichina.com"],
        )
    ]
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(account)
    row = db_session.scalars(select(Domain)).one()
    assert account.first_synced_at is not None
    assert row.name == "later.com"
    assert row.from_first_snapshot is True
    assert row.status == STATUS_USED


def test_used_bound_skips_describe_within_cooldown(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
        last_success_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = _bound_used_row(db_session, account, name="a.com")
    row.last_registrar_checked_at = now
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="a.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    assert described == []


def test_unused_still_describes_within_used_cooldown(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=now,
        last_success_at=now,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="fresh.com",
        display_name="fresh.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        nameservers="dns9.hichina.com",
        last_registrar_checked_at=now,
    )
    db_session.add(row)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="fresh.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            return AliyunDomain(
                name=name,
                domain_status="3",
                audit_status="SUCCEED",
                nameservers=["dns9.hichina.com"],
            )

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    assert described == ["fresh.com"]


def test_stale_client_hold_cleared_when_list_not_hold_and_describe_skipped(db_session, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    checked = now - timedelta(seconds=5)
    success = now - timedelta(seconds=30)
    account = AliyunAccount(
        name="ak1",
        access_key_id="LTAIxxxx",
        access_key_secret_enc="enc",
        enabled=True,
        first_synced_at=success,
        last_success_at=success,
    )
    db_session.add(account)
    db_session.flush()
    row = Domain(
        name="hold.com",
        display_name="hold.com",
        aliyun_account_id=account.id,
        status=STATUS_UNUSED,
        domain_status="3",
        audit_status="SUCCEED",
        nameservers="dns9.hichina.com",
        client_hold=True,
        last_registrar_checked_at=checked,
    )
    db_session.add(row)
    db_session.commit()
    described: list[str] = []

    class FakeClient:
        def list_domains(self):
            return [
                AliyunDomain(
                    name="hold.com",
                    domain_status="3",
                    audit_status="SUCCEED",
                    nameservers=["dns9.hichina.com"],
                    client_hold=False,
                )
            ]

        def describe_registrar_domain(self, name: str):
            described.append(name)
            raise RuntimeError("describe should be skipped")

    monkeypatch.setattr("app.worker.poll_aliyun.aliyun_client", lambda _settings, _account: FakeClient())
    poll_one_aliyun_account(db_session, settings, account)
    db_session.commit()
    db_session.refresh(row)
    assert described == []
    assert row.client_hold is False
    assert row.status == STATUS_UNUSED
