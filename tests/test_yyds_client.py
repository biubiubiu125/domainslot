import json

import httpx
import pytest

from app.yyds.client import YydsClient, YydsDomain, YydsError, _as_list, _unwrap, parse_retry_after


def _client() -> YydsClient:
    return YydsClient("https://example.invalid/v1", "u", "p")


def test_set_private_tries_empty_patch_first():
    client = _client()
    calls: list[tuple[str, str, dict]] = []
    public = True

    def fake_request(method: str, path: str, **kwargs):
        nonlocal public
        calls.append((method, path, kwargs))
        if method == "GET" and path == "me/domains":
            return [{"id": "yd1", "domain": "a.com", "isPublic": public}]
        if method == "PATCH":
            if "json" in kwargs:
                raise AssertionError("OpenAPI PATCH /me/domains/{id} has no requestBody")
            public = False
            return {}
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.set_private("yd1") is True
    finally:
        client.close()
    patch_calls = [item for item in calls if item[0] == "PATCH"]
    assert patch_calls
    assert "json" not in patch_calls[0][2]
    assert calls[-1][:2] == ("GET", "me/domains")


def test_set_private_falls_back_to_is_public_body():
    client = _client()
    calls: list[tuple[str, str, dict]] = []
    public = True

    def fake_request(method: str, path: str, **kwargs):
        nonlocal public
        calls.append((method, path, kwargs))
        if method == "GET" and path == "me/domains":
            return [{"id": "yd1", "domain": "a.com", "isPublic": public}]
        if method == "PATCH" and "json" not in kwargs:
            return {}
        if method == "PATCH" and kwargs.get("json") == {"isPublic": False}:
            public = False
            return {}
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.set_private("yd1") is True
    finally:
        client.close()
    patch_calls = [item for item in calls if item[0] == "PATCH"]
    assert "json" not in patch_calls[0][2]
    assert any(item[2].get("json") == {"isPublic": False} for item in patch_calls)


def test_set_private_retries_without_body_on_400():
    client = _client()
    calls: list[tuple[str, str, dict]] = []
    public = True

    def fake_request(method: str, path: str, **kwargs):
        nonlocal public
        calls.append((method, path, kwargs))
        if method == "GET" and path == "me/domains":
            return [{"id": "yd1", "domain": "a.com", "isPublic": public}]
        if method == "PATCH" and kwargs.get("json") == {"isPublic": False}:
            raise YydsError("bad request", 400, {})
        if method == "PATCH":
            public = False
            return {}
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.set_private("yd1") is True
    finally:
        client.close()
    assert ("PATCH", "me/domains/yd1") in [(item[0], item[1]) for item in calls]
    assert any(item[0] == "PATCH" and "json" not in item[2] for item in calls)
    assert calls[-1][:2] == ("GET", "me/domains")


def test_set_private_does_not_trust_patch_200():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        if method == "PATCH":
            return {"success": True}
        if method == "GET" and path == "me/domains":
            return [{"id": "yd1", "domain": "a.com", "isPublic": True}]
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.set_private("yd1") is False
    finally:
        client.close()


def test_set_private_skips_patch_when_already_private():
    client = _client()
    calls: list[str] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append(method)
        if method == "GET" and path == "me/domains":
            return [{"id": "yd1", "domain": "a.com", "isPublic": False}]
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.set_private("yd1") is True
    finally:
        client.close()
    assert calls == ["GET"]


def test_set_private_nobody_patch_still_rereads():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        if method == "GET" and path == "me/domains":
            return [{"id": "yd1", "domain": "a.com", "isPublic": True}]
        if method == "PATCH" and kwargs.get("json") == {"isPublic": False}:
            raise YydsError("bad request", 400, {})
        if method == "PATCH":
            return {}
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.set_private("yd1") is False
    finally:
        client.close()


def test_set_private_accepts_already_private_after_patch_fails():
    client = _client()
    public = True

    def fake_request(method: str, path: str, **kwargs):
        nonlocal public
        if method == "PATCH":
            public = False
            raise YydsError("bad request", 400, {})
        if method == "GET" and path == "me/domains":
            return [{"id": "yd1", "domain": "a.com", "isPublic": public}]
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        client.list_domains = lambda: [  # type: ignore[method-assign]
            YydsDomain(id="yd1", domain="a.com", is_public=public, verification_token=None, raw={})
        ]
        assert client.set_private("yd1") is True
    finally:
        client.close()


def test_ensure_wildcard_rule_posts_without_json_body():
    client = _client()
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return []
        return {}

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.ensure_wildcard_rule("yd1") is True
    finally:
        client.close()
    assert calls[0][:2] == ("GET", "me/domains/yd1/wildcard-rules")
    assert calls[1][:2] == ("POST", "me/domains/yd1/wildcard-rules")
    assert "json" not in calls[1][2]


def test_ensure_wildcard_unparseable_list_does_not_post():
    client = _client()
    calls: list[str] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append(method)
        if method == "GET":
            return {"foo": 1}
        raise AssertionError("unparseable wildcard list must not POST")

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="无法解析列表"):
            client.ensure_wildcard_rule("yd1")
    finally:
        client.close()
    assert calls == ["GET"]


def test_ensure_wildcard_get_error_does_not_post():
    client = _client()
    calls: list[str] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append(method)
        if method == "GET":
            raise YydsError("wildcard missing", 500, {})
        raise AssertionError("failed wildcard GET must not POST")

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="wildcard missing"):
            client.ensure_wildcard_rule("yd1")
    finally:
        client.close()
    assert calls == ["GET"]


def test_ensure_wildcard_409_without_rule_raises():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        if method == "GET":
            return []
        raise YydsError("already exists", 409, {})

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="通配规则"):
            client.ensure_wildcard_rule("yd1")
    finally:
        client.close()


def test_ensure_wildcard_409_with_existing_rule_ok():
    client = _client()
    gets = {"n": 0}

    def fake_request(method: str, path: str, **kwargs):
        if method == "GET":
            gets["n"] += 1
            if gets["n"] == 1:
                return []
            return [{"id": "r1", "pattern": "*.a.com"}]
        raise YydsError("already exists", 409, {})

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.ensure_wildcard_rule("yd1") is True
    finally:
        client.close()
    assert gets["n"] >= 2


def _invalid_body() -> YydsError:
    return YydsError(
        "invalid_request_body HTTP 400",
        400,
        {"success": False, "errorCode": "invalid_request_body"},
    )


def _patch_kwargs_are_query_and_json_state(kwargs: dict) -> bool:
    return (
        kwargs.get("params") == {"state": "active"}
        and kwargs.get("json") == {"state": "active"}
        and "data" not in kwargs
    )


def _patch_kwargs_are_query_and_empty_json(kwargs: dict) -> bool:
    return (
        kwargs.get("params") == {"state": "active"}
        and kwargs.get("json") == {}
        and "data" not in kwargs
    )


def test_enable_wildcard_rules_patches_active_state():
    client = _client()
    calls: list[tuple[str, str, dict]] = []
    state = {"value": "draft"}

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and path == "me/domains/yd1/wildcard-rules":
            return [{"id": "r1", "state": state["value"], "pattern": "*.a.com"}]
        if method == "PATCH" and path == "me/domains/yd1/wildcard-rules/r1":
            if not _patch_kwargs_are_query_and_json_state(kwargs):
                raise AssertionError("send state as query and JSON body together")
            state["value"] = "active"
            return None
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.enable_wildcard_rules("yd1") == ["r1"]
    finally:
        client.close()
    assert [item[:2] for item in calls] == [
        ("GET", "me/domains/yd1/wildcard-rules"),
        ("PATCH", "me/domains/yd1/wildcard-rules/r1"),
        ("GET", "me/domains/yd1/wildcard-rules"),
    ]
    assert _patch_kwargs_are_query_and_json_state(calls[1][2])


def test_enable_wildcard_rules_skips_already_active():
    client = _client()
    calls: list[str] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append(method)
        if method == "GET":
            return [{"id": "r1", "state": "active", "pattern": "*.a.com"}]
        raise AssertionError("active rule must not PATCH")

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.enable_wildcard_rules("yd1") == []
    finally:
        client.close()
    assert calls == ["GET"]


def test_enable_wildcard_rules_creates_draft_then_enables():
    client = _client()
    calls: list[tuple[str, str, dict]] = []
    created = {"done": False}
    state = {"value": "draft"}

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and path == "me/domains/yd1/wildcard-rules":
            if created["done"]:
                return [{"id": "r1", "state": state["value"], "pattern": "*.a.com"}]
            return []
        if method == "POST" and path == "me/domains/yd1/wildcard-rules":
            created["done"] = True
            return {"id": "r1", "state": "draft"}
        if method == "PATCH" and path == "me/domains/yd1/wildcard-rules/r1":
            if not _patch_kwargs_are_query_and_json_state(kwargs):
                raise AssertionError((method, path, kwargs))
            state["value"] = "active"
            return None
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.enable_wildcard_rules("yd1") == ["r1"]
    finally:
        client.close()
    assert [item[:2] for item in calls] == [
        ("GET", "me/domains/yd1/wildcard-rules"),
        ("POST", "me/domains/yd1/wildcard-rules"),
        ("GET", "me/domains/yd1/wildcard-rules"),
        ("PATCH", "me/domains/yd1/wildcard-rules/r1"),
        ("GET", "me/domains/yd1/wildcard-rules"),
    ]
    assert "json" not in calls[1][2]
    assert _patch_kwargs_are_query_and_json_state(calls[3][2])


def test_enable_wildcard_rules_enables_paused():
    client = _client()
    calls: list[tuple[str, str, dict]] = []
    state = {"value": "paused"}

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return [{"id": "r1", "state": state["value"], "pauseReason": "user_paused"}]
        if method == "PATCH":
            if not _patch_kwargs_are_query_and_json_state(kwargs):
                raise AssertionError((method, path, kwargs))
            state["value"] = "active"
            return None
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.enable_wildcard_rules("yd1") == ["r1"]
    finally:
        client.close()
    assert [item[:2] for item in calls] == [
        ("GET", "me/domains/yd1/wildcard-rules"),
        ("PATCH", "me/domains/yd1/wildcard-rules/r1"),
        ("GET", "me/domains/yd1/wildcard-rules"),
    ]
    assert _patch_kwargs_are_query_and_json_state(calls[1][2])


def test_enable_wildcard_rules_falls_back_to_empty_json_when_body_invalid():
    client = _client()
    calls: list[tuple[str, str, dict]] = []
    state = {"value": "draft"}

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return [{"id": "r1", "state": state["value"], "pattern": "*.a.com"}]
        if method == "PATCH" and _patch_kwargs_are_query_and_json_state(kwargs):
            raise _invalid_body()
        if method == "PATCH" and _patch_kwargs_are_query_and_empty_json(kwargs):
            state["value"] = "active"
            return None
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.enable_wildcard_rules("yd1") == ["r1"]
    finally:
        client.close()
    patch_calls = [item for item in calls if item[0] == "PATCH"]
    assert [_patch_kwargs_are_query_and_json_state(item[2]) for item in patch_calls] == [True, False]
    assert _patch_kwargs_are_query_and_empty_json(patch_calls[1][2])
    assert calls[-1][:2] == ("GET", "me/domains/yd1/wildcard-rules")


def test_enable_wildcard_rules_retries_empty_json_when_combined_patch_does_not_activate():
    client = _client()
    calls: list[tuple[str, str, dict]] = []
    state = {"value": "draft"}

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return [{"id": "r1", "state": state["value"], "pattern": "*.a.com"}]
        if method == "PATCH" and _patch_kwargs_are_query_and_json_state(kwargs):
            return None
        if method == "PATCH" and _patch_kwargs_are_query_and_empty_json(kwargs):
            state["value"] = "active"
            return None
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.enable_wildcard_rules("yd1") == ["r1"]
    finally:
        client.close()
    patch_calls = [item for item in calls if item[0] == "PATCH"]
    assert len(patch_calls) == 2
    assert _patch_kwargs_are_query_and_json_state(patch_calls[0][2])
    assert _patch_kwargs_are_query_and_empty_json(patch_calls[1][2])


def test_enable_wildcard_rules_raises_if_rule_stays_inactive():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        if method == "GET":
            return [{"id": "r1", "state": "draft", "pattern": "*.a.com"}]
        if method == "PATCH":
            return None
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="仍未生效"):
            client.enable_wildcard_rules("yd1")
    finally:
        client.close()


def test_enable_wildcard_rules_does_not_fallback_on_mx_not_ready():
    client = _client()
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return [{"id": "r1", "state": "draft", "pattern": "*.a.com"}]
        if method == "PATCH":
            raise YydsError(
                "wildcard_rule_mx_not_ready HTTP 400",
                400,
                {"success": False, "errorCode": "wildcard_rule_mx_not_ready"},
            )
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="wildcard_rule_mx_not_ready"):
            client.enable_wildcard_rules("yd1")
    finally:
        client.close()
    patch_calls = [item for item in calls if item[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert _patch_kwargs_are_query_and_json_state(patch_calls[0][2])


def test_enable_wildcard_rules_sends_state_query_on_the_wire():
    captured: list[httpx.Request] = []
    patched = {"ok": False}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/me"):
            return httpx.Response(200, json={"success": True, "data": {"id": "u1"}})
        if request.method == "GET" and path.endswith("/wildcard-rules"):
            state = "active" if patched["ok"] else "draft"
            return httpx.Response(
                200,
                json={"success": True, "data": [{"id": "r1", "state": state, "pattern": "*.a.com"}]},
            )
        if request.method == "PATCH" and path.endswith("/wildcard-rules/r1"):
            patched["ok"] = True
            return httpx.Response(204)
        return httpx.Response(500, json={"success": False, "errorCode": "unexpected", "path": path})

    client = _client()
    client.access_token = "tok"
    client.http.close()
    client.http = httpx.Client(
        base_url=client.api_base,
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )
    try:
        assert client.enable_wildcard_rules("yd1") == ["r1"]
    finally:
        client.close()
    patch = next(item for item in captured if item.method == "PATCH")
    assert patch.url.params.get("state") == "active"
    assert json.loads(patch.content) == {"state": "active"}
    assert (patch.headers.get("content-type") or "").startswith("application/json")
    rule_gets = [item for item in captured if item.method == "GET" and item.url.path.endswith("/wildcard-rules")]
    assert len(rule_gets) == 2


def test_delete_domain_sends_delete():
    client = _client()
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "DELETE" and path == "me/domains/yd1":
            return {"ok": True}
        raise AssertionError((method, path, kwargs))

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.delete_domain("yd1") == {"ok": True}
    finally:
        client.close()
    assert calls == [("DELETE", "me/domains/yd1", {})]


def test_as_list_reads_wildcard_rules_key():
    assert _as_list({"wildcardRules": [{"id": "r1"}, {"id": "r2"}]}) == [{"id": "r1"}, {"id": "r2"}]
    assert _as_list({"items": [], "wildcardRules": [{"id": "r1"}]}) == [{"id": "r1"}]


def test_as_list_does_not_swallow_parse_error():
    with pytest.raises(YydsError):
        _as_list({"foo": 1})


def test_list_wildcard_rules_reads_wildcard_rules_key():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        assert method == "GET"
        return {"wildcardRules": [{"id": "r1", "pattern": "*.a.com"}]}

    try:
        client.request = fake_request  # type: ignore[method-assign]
        rules = client.list_wildcard_rules()
        assert len(rules) == 1
        assert rules[0]["id"] == "r1"
    finally:
        client.close()


def test_list_domains_unknown_payload_raises():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        assert method == "GET"
        return {"total": 3, "page": 1}

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="无法解析列表"):
            client.list_domains()
    finally:
        client.close()


def test_list_wildcard_rules_unknown_payload_raises():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {"foo": 1}

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="无法解析列表"):
            client.list_wildcard_rules()
    finally:
        client.close()


def test_list_wildcard_rules_rejects_truncated_total():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {
            "wildcardRules": [{"id": "r1", "pattern": "*.a.com"}],
            "total": 3,
        }

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="列表不完整"):
            client.list_wildcard_rules()
    finally:
        client.close()


def test_list_wildcard_rules_rejects_has_more():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {
            "items": [{"id": "r1"}],
            "hasMore": True,
        }

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="列表不完整"):
            client.list_wildcard_rules()
    finally:
        client.close()


def test_list_domains_empty_items_is_empty():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {"items": []}

    try:
        client.request = fake_request  # type: ignore[method-assign]
        assert client.list_domains() == []
    finally:
        client.close()


def test_list_domains_rejects_truncated_total():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {
            "items": [{"id": "yd1", "domain": "a.com"}],
            "total": 3,
        }

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="列表不完整"):
            client.list_domains()
    finally:
        client.close()


def test_list_domains_rejects_has_more():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {
            "items": [{"id": "yd1", "domain": "a.com"}],
            "hasMore": True,
        }

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="列表不完整"):
            client.list_domains()
    finally:
        client.close()


def test_list_domains_complete_when_total_matches():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {
            "items": [{"id": "yd1", "domain": "a.com"}],
            "total": 1,
            "hasMore": False,
        }

    try:
        client.request = fake_request  # type: ignore[method-assign]
        items = client.list_domains()
        assert len(items) == 1
        assert items[0].domain == "a.com"
    finally:
        client.close()


def _http_client(handler) -> YydsClient:
    client = YydsClient("https://example.invalid/v1", "u", "p")
    client.http.close()
    client.http = httpx.Client(
        base_url="https://example.invalid/v1/",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
        follow_redirects=True,
    )
    return client


def test_unwrap_false_success_keeps_http_status():
    with pytest.raises(YydsError) as exc:
        _unwrap(
            {"success": False, "error": "too many requests", "errorCode": "rate_limited"},
            status_code=429,
        )
    assert exc.value.status_code == 429
    assert "rate_limited" in str(exc.value)


def test_login_includes_twofa_and_turnstile_when_set():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": {"access_token": "t"}})

    client = YydsClient(
        "https://example.invalid/v1",
        "u",
        "p",
        twofa_code="123456",
        turnstile_token="cf-turnstile",
    )
    client.http.close()
    client.http = httpx.Client(
        base_url="https://example.invalid/v1/",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        client.login()
    finally:
        client.close()
    assert captured["json"] == {
        "username": "u",
        "password": "p",
        "twofaCode": "123456",
        "turnstileToken": "cf-turnstile",
    }
    assert client.twofa_consumed is True


def test_login_without_twofa_does_not_mark_consumed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"access_token": "t"}})

    client = _http_client(handler)
    try:
        client.login()
        assert getattr(client, "twofa_consumed", False) is False
    finally:
        client.close()


def test_login_twofa_required_uses_clear_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"success": False, "error": "twofaCode required", "errorCode": "TWO_FA_REQUIRED"},
        )

    client = _http_client(handler)
    try:
        with pytest.raises(YydsError) as exc:
            client.login()
        assert exc.value.status_code == 400
        assert "2FA" in str(exc.value)
        assert "停止" in str(exc.value)
        assert getattr(client, "twofa_consumed", False) is False
    finally:
        client.close()


def test_login_turnstile_required_uses_clear_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"success": False, "error": "turnstile token required", "errorCode": "TURNSTILE_REQUIRED"},
        )

    client = _http_client(handler)
    try:
        with pytest.raises(YydsError) as exc:
            client.login()
        assert exc.value.status_code == 403
        assert "Turnstile" in str(exc.value) or "验证码" in str(exc.value)
        assert "停止" in str(exc.value)
    finally:
        client.close()


def test_login_429_envelope_keeps_status_code():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("auth/login")
        return httpx.Response(
            429,
            json={"success": False, "error": "Too many requests", "errorCode": "rate_limited"},
        )

    client = _http_client(handler)
    try:
        with pytest.raises(YydsError) as exc:
            client.login()
        assert exc.value.status_code == 429
    finally:
        client.close()


def test_login_429_reads_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json={"success": False, "error": "Too many requests", "errorCode": "rate_limited"},
        )

    client = _http_client(handler)
    try:
        with pytest.raises(YydsError) as exc:
            client.login()
        assert exc.value.status_code == 429
        assert getattr(exc.value, "retry_after_seconds", None) == 7
    finally:
        client.close()


def test_ensure_session_does_not_login_after_me_429():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.rstrip("/").endswith("/me") or request.url.path.endswith("me"):
            return httpx.Response(
                429,
                json={"success": False, "error": "Too many requests", "errorCode": "rate_limited"},
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    client = _http_client(handler)
    client.access_token = "tok"
    try:
        with pytest.raises(YydsError) as exc:
            client.ensure_session()
        assert exc.value.status_code == 429
    finally:
        client.close()
    assert calls
    assert not any("login" in item for item in calls)
    assert not any("refresh" in item for item in calls)


def test_refresh_429_does_not_login():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            429,
            json={"success": False, "error": "Too many requests", "errorCode": "rate_limited"},
        )

    client = _http_client(handler)
    try:
        with pytest.raises(YydsError) as exc:
            client.refresh()
        assert exc.value.status_code == 429
    finally:
        client.close()
    assert not any("login" in item for item in calls)


def test_request_does_not_login_when_refresh_is_429():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.rstrip("/").endswith("/me") or path.endswith("me"):
            return httpx.Response(200, json={"success": True, "data": {"id": "u"}})
        if "quota" in path:
            return httpx.Response(401, json={"success": False, "error": "unauthorized"})
        if "refresh" in path:
            return httpx.Response(
                429,
                json={"success": False, "error": "Too many requests", "errorCode": "rate_limited"},
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    client = _http_client(handler)
    client.access_token = "tok"
    try:
        with pytest.raises(YydsError) as exc:
            client.request("GET", "me/quota")
        assert exc.value.status_code == 429
    finally:
        client.close()
    assert not any("login" in item for item in calls)


def test_batch_verify_posts_domain_ids():
    client = _client()
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        return {"items": [{"domainId": "d1", "domain": "a.com", "result": "receiving_ready"}]}

    try:
        client.request = fake_request  # type: ignore[method-assign]
        payload = client.batch_verify(["d1", "d2"])
    finally:
        client.close()
    assert calls == [("POST", "me/domains/batch-verify", {"json": {"domainIds": ["d1", "d2"]}})]
    assert payload["items"][0]["result"] == "receiving_ready"


def test_parse_retry_after_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = datetime.now(timezone.utc) + timedelta(seconds=45)
    seconds = parse_retry_after({"Retry-After": format_datetime(when, usegmt=True)})
    assert seconds is not None
    assert 20 <= seconds <= 60
    assert parse_retry_after({"Retry-After": "7"}) == 7


def test_list_domains_rejects_has_next_page():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {
            "items": [{"id": "yd1", "domain": "a.com"}],
            "hasNextPage": True,
        }

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="列表不完整"):
            client.list_domains()
    finally:
        client.close()


def test_list_domains_rejects_truncated_flag():
    client = _client()

    def fake_request(method: str, path: str, **kwargs):
        return {
            "items": [{"id": "yd1", "domain": "a.com"}],
            "isTruncated": True,
        }

    try:
        client.request = fake_request  # type: ignore[method-assign]
        with pytest.raises(YydsError, match="列表不完整"):
            client.list_domains()
    finally:
        client.close()


def test_list_domains_rejects_envelope_has_more():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        if path.endswith("/me") or path.endswith("me"):
            return httpx.Response(200, json={"success": True, "data": {"id": "u"}})
        if path.endswith("/me/domains") or path.endswith("me/domains"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": "yd1", "domain": "a.com"}],
                    "hasMore": True,
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    client = _http_client(handler)
    client.access_token = "tok"
    try:
        with pytest.raises(YydsError, match="列表不完整"):
            client.list_domains()
    finally:
        client.close()


def test_list_domains_rejects_envelope_total_mismatch():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        if path.endswith("/me") or path.endswith("me"):
            return httpx.Response(200, json={"success": True, "data": {"id": "u"}})
        if path.endswith("/me/domains") or path.endswith("me/domains"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": "yd1", "domain": "a.com"}],
                    "total": 3,
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    client = _http_client(handler)
    client.access_token = "tok"
    try:
        with pytest.raises(YydsError, match="列表不完整"):
            client.list_domains()
    finally:
        client.close()


def test_request_origin_uses_vip_console_for_official_api():
    from app.yyds.client import request_origin

    assert request_origin("https://maliapi.215.im/v1") == "https://vip.215.im"
    assert request_origin("https://maliapi.215.im/v1/") == "https://vip.215.im"
    assert request_origin("https://example.invalid/v1") == "https://example.invalid"


def test_login_sends_console_origin_for_official_api():
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["origin"] = request.headers.get("origin")
        captured["referer"] = request.headers.get("referer")
        return httpx.Response(200, json={"success": True, "data": {"access_token": "t"}})

    client = YydsClient("https://maliapi.215.im/v1", "u", "p")
    client.http.close()
    client.http = httpx.Client(
        base_url="https://maliapi.215.im/v1/",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        client.login()
    finally:
        client.close()
    assert captured["origin"] == "https://vip.215.im"
    assert captured["referer"] == "https://vip.215.im/"


def test_add_domain_sends_console_origin_for_official_api():
    origins: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        origins.append((request.method, request.headers.get("origin")))
        path = request.url.path.rstrip("/")
        if path.endswith("/me") or path.endswith("me"):
            return httpx.Response(200, json={"success": True, "data": {"id": "u"}})
        if request.method == "POST" and path.endswith("/me/domains"):
            return httpx.Response(201, json={"success": True, "data": {"id": "yd1", "domain": "a.com"}})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    client = YydsClient("https://maliapi.215.im/v1", "u", "p")
    client.http.close()
    client.http = httpx.Client(
        base_url="https://maliapi.215.im/v1/",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
        follow_redirects=True,
    )
    client.access_token = "tok"
    try:
        client.add_domain("a.com")
    finally:
        client.close()
    assert ("POST", "https://vip.215.im") in origins
    assert all(origin == "https://vip.215.im" for _method, origin in origins)
