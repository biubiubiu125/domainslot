import pytest

from Tea.exceptions import RetryError, TeaException, UnretryableException

from app.aliyun.client import AliyunClient, AliyunDomain, AliyunError, _tea_error


def _request_name(request_factory) -> str:
    return type(request_factory(1, 50)).__name__


def test_list_domains_does_not_fallback_when_advanced_throttled(monkeypatch):
    client = AliyunClient("ak", "sk")
    calls: list[str] = []

    def fake_paged(self, request_factory, caller):
        name = _request_name(request_factory)
        calls.append(name)
        if name == "QueryAdvancedDomainListRequest":
            raise AliyunError("Throttling.User", code="Throttling.User", throttled=True)
        raise AssertionError(f"simple list must not run after advanced throttle: {name}")

    monkeypatch.setattr(AliyunClient, "_paged_domain_list", fake_paged)
    with pytest.raises(AliyunError) as exc:
        client.list_domains()
    assert exc.value.throttled is True
    assert calls == ["QueryAdvancedDomainListRequest"]


def test_list_domains_falls_back_when_advanced_is_not_throttle(monkeypatch):
    client = AliyunClient("ak", "sk")
    calls: list[str] = []

    def fake_paged(self, request_factory, caller):
        name = _request_name(request_factory)
        calls.append(name)
        if name == "QueryAdvancedDomainListRequest":
            raise AliyunError("Forbidden", code="Forbidden", throttled=False)
        return [AliyunDomain(name="a.com")]

    monkeypatch.setattr(AliyunClient, "_paged_domain_list", fake_paged)
    items = client.list_domains()
    assert [item.name for item in items] == ["a.com"]
    assert calls == ["QueryAdvancedDomainListRequest", "QueryDomainListRequest"]


def test_list_domains_falls_back_when_advanced_returns_empty(monkeypatch):
    client = AliyunClient("ak", "sk")
    calls: list[str] = []

    def fake_paged(self, request_factory, caller):
        name = _request_name(request_factory)
        calls.append(name)
        if name == "QueryAdvancedDomainListRequest":
            return []
        return [AliyunDomain(name="a.com")]

    monkeypatch.setattr(AliyunClient, "_paged_domain_list", fake_paged)
    items = client.list_domains()
    assert [item.name for item in items] == ["a.com"]
    assert calls == ["QueryAdvancedDomainListRequest", "QueryDomainListRequest"]


def test_list_domains_unions_simple_list_when_advanced_is_incomplete(monkeypatch):
    client = AliyunClient("ak", "sk")
    calls: list[str] = []

    def fake_paged(self, request_factory, caller):
        name = _request_name(request_factory)
        calls.append(name)
        if name == "QueryAdvancedDomainListRequest":
            return [AliyunDomain(name="a.com", domain_status="3")]
        return [
            AliyunDomain(name="a.com", domain_status="1"),
            AliyunDomain(name="b.com", domain_status="3"),
        ]

    monkeypatch.setattr(AliyunClient, "_paged_domain_list", fake_paged)
    items = client.list_domains()
    assert [item.name for item in items] == ["a.com", "b.com"]
    assert items[0].domain_status == "3"
    assert calls == ["QueryAdvancedDomainListRequest", "QueryDomainListRequest"]


def test_list_domains_keeps_advanced_when_simple_merge_throttled(monkeypatch):
    client = AliyunClient("ak", "sk")
    calls: list[str] = []

    def fake_paged(self, request_factory, caller):
        name = _request_name(request_factory)
        calls.append(name)
        if name == "QueryAdvancedDomainListRequest":
            return [AliyunDomain(name="a.com")]
        raise AliyunError("Throttling.User", code="Throttling.User", throttled=True)

    monkeypatch.setattr(AliyunClient, "_paged_domain_list", fake_paged)
    items = client.list_domains()
    assert [item.name for item in items] == ["a.com"]
    assert calls == ["QueryAdvancedDomainListRequest", "QueryDomainListRequest"]


def test_list_domains_raises_when_empty_advanced_fallback_throttled(monkeypatch):
    client = AliyunClient("ak", "sk")
    calls: list[str] = []

    def fake_paged(self, request_factory, caller):
        name = _request_name(request_factory)
        calls.append(name)
        if name == "QueryAdvancedDomainListRequest":
            return []
        raise AliyunError("Throttling.User", code="Throttling.User", throttled=True)

    monkeypatch.setattr(AliyunClient, "_paged_domain_list", fake_paged)
    with pytest.raises(AliyunError) as exc:
        client.list_domains()
    assert exc.value.throttled is True
    assert calls == ["QueryAdvancedDomainListRequest", "QueryDomainListRequest"]


class _FakeListBody:
    def __init__(self, names, *, next_page=None, total_item_num=None):
        self.data = type("Data", (), {"domain": [_FakeListItem(name) for name in names]})()
        self.next_page = next_page
        self.total_item_num = total_item_num


class _FakeListItem:
    def __init__(self, name: str):
        self.domain_name = name
        self.registration_date = None
        self.domain_status = "3"


def _run_paged_domain_list(client, fake_call):
    return client._paged_domain_list(
        lambda page, size: type("Req", (), {"page_num": page, "page_size": size})(),
        fake_call,
    )


def test_paged_domain_list_follows_next_page_even_if_short():
    client = AliyunClient("ak", "sk")
    pages: list[int] = []

    def fake_call(request):
        page = request.page_num
        pages.append(page)
        if page == 1:
            body = _FakeListBody(["a.com"], next_page=True, total_item_num=2)
        else:
            body = _FakeListBody(["b.com"], next_page=False, total_item_num=2)
        return type("Resp", (), {"body": body})()

    items = _run_paged_domain_list(client, fake_call)
    assert [item.name for item in items] == ["a.com", "b.com"]
    assert pages == [1, 2]


def test_paged_domain_list_uses_total_item_num_when_next_page_false():
    client = AliyunClient("ak", "sk")
    pages: list[int] = []

    def fake_call(request):
        page = request.page_num
        pages.append(page)
        if page == 1:
            body = _FakeListBody(["a.com"], next_page=False, total_item_num=2)
        else:
            body = _FakeListBody(["b.com"], next_page=False, total_item_num=2)
        return type("Resp", (), {"body": body})()

    items = _run_paged_domain_list(client, fake_call)
    assert [item.name for item in items] == ["a.com", "b.com"]
    assert pages == [1, 2]


def test_paged_domain_list_follows_next_page_even_if_total_already_collected():
    client = AliyunClient("ak", "sk")
    pages: list[int] = []

    def fake_call(request):
        page = request.page_num
        pages.append(page)
        if page == 1:
            body = _FakeListBody(["a.com"], next_page=True, total_item_num=1)
        else:
            body = _FakeListBody(["b.com"], next_page=False, total_item_num=1)
        return type("Resp", (), {"body": body})()

    items = _run_paged_domain_list(client, fake_call)
    assert [item.name for item in items] == ["a.com", "b.com"]
    assert pages == [1, 2]


def test_list_records_uses_total_count_even_if_short_page():
    client = AliyunClient("ak", "sk")
    pages: list[int] = []

    def fake_call(request):
        page = request.page_number
        pages.append(page)
        if page == 1:
            recs = [
                type(
                    "Rec",
                    (),
                    {"record_id": "1", "rr": "@", "type": "MX", "value": "mx.215.im", "priority": 10, "line": "default"},
                )()
            ]
            body = type("Body", (), {"domain_records": type("Wrap", (), {"record": recs})(), "total_count": 2})()
        else:
            recs = [
                type(
                    "Rec",
                    (),
                    {"record_id": "2", "rr": "*", "type": "MX", "value": "mx.215.im", "priority": 10, "line": "default"},
                )()
            ]
            body = type("Body", (), {"domain_records": type("Wrap", (), {"record": recs})(), "total_count": 2})()
        return type("Resp", (), {"body": body})()

    client.dns_api.describe_domain_records = fake_call
    items = client.list_records("a.com")
    assert [item["record_id"] for item in items] == ["1", "2"]
    assert pages == [1, 2]


def test_aliyun_client_sets_sdk_timeouts_in_milliseconds():
    client = AliyunClient("ak", "sk")
    assert AliyunClient.CONNECT_TIMEOUT == 10000
    assert AliyunClient.READ_TIMEOUT == 30000
    assert client.domain_api._connect_timeout == 10000
    assert client.domain_api._read_timeout == 30000
    assert client.dns_api._connect_timeout == 10000
    assert client.dns_api._read_timeout == 30000


def test_tea_error_maps_connect_timeout_to_chinese():
    exc = TeaException(
        {
            "message": (
                "HTTPSConnectionPool(host='domain.aliyuncs.com', port=443): "
                "Max retries exceeded with url: /?PageNum=1&PageSize=50 "
                "(Caused by ConnectTimeoutError(<HTTPSConnectionPool(host='domain.aliyuncs.com', port=443)>, "
                "'Connection to domain.aliyuncs.com timed out. (connect timeout=0.01)'))"
            )
        }
    )
    err = _tea_error(exc)
    assert str(err) == "连接阿里云超时"
    assert err.throttled is False


def test_tea_error_maps_retry_error_connect_timeout():
    inner = RetryError(
        "Connection to domain.aliyuncs.com timed out. (connect timeout=0.01)"
    )
    err = _tea_error(UnretryableException(object(), inner))
    assert str(err) == "连接阿里云超时"


def test_tea_error_unwraps_retry_error_without_class_prefix():
    inner = RetryError("SSL: CERTIFICATE_VERIFY_FAILED")
    err = _tea_error(UnretryableException(object(), inner))
    assert str(err) == "SSL: CERTIFICATE_VERIFY_FAILED"
    assert "RetryError" not in str(err)


def test_tea_error_keeps_aliyun_api_message():
    exc = TeaException(
        {
            "code": "InvalidAccessKeyId.NotFound",
            "message": "Specified access key is not found.",
            "data": {
                "Code": "InvalidAccessKeyId.NotFound",
                "Message": "Specified access key is not found.",
            },
        }
    )
    err = _tea_error(exc)
    assert str(err) == "Specified access key is not found."
    assert err.code == "InvalidAccessKeyId.NotFound"


def test_apply_guide_keeps_matching_wanted_records(monkeypatch):
    from app.aliyun.records import WantedRecord

    client = AliyunClient("ak", "sk")
    deleted: list[str] = []
    added: list[object] = []
    monkeypatch.setattr(client, "describe_registrar_nameservers", lambda domain: ["dns9.hichina.com"])
    monkeypatch.setattr(client, "ensure_dns_domain", lambda domain: None)
    monkeypatch.setattr(
        client,
        "list_records",
        lambda domain: [
            {"record_id": "1", "rr": "@", "type": "MX", "value": "mx.215.im", "priority": 10},
            {"record_id": "2", "rr": "*", "type": "MX", "value": "mx.215.im", "priority": 10},
            {"record_id": "3", "rr": "_yydsmail-verify", "type": "TXT", "value": "tok", "priority": None},
            {"record_id": "4", "rr": "@", "type": "A", "value": "1.2.3.4", "priority": None},
        ],
    )
    monkeypatch.setattr(client, "delete_record", lambda rid: deleted.append(rid))
    monkeypatch.setattr(client, "add_record", lambda domain, wanted: added.append(wanted))
    wanted = [
        WantedRecord("TXT", "_yydsmail-verify", "tok"),
        WantedRecord("MX", "@", "mx.215.im", 10),
        WantedRecord("MX", "*", "mx.215.im", 10),
    ]
    result = client.apply_guide("new.com", wanted)
    assert deleted == ["4"]
    assert added == []
    assert result["deleted"] == 1
    assert result["added"] == 0
