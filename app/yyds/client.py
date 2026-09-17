from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from app.domainutil import domains_match, normalize_domain

_session_locks: dict[str, threading.RLock] = {}
_session_locks_guard = threading.Lock()


def account_session_lock(key: str) -> threading.RLock:
    name = (key or "").strip() or "_"
    with _session_locks_guard:
        lock = _session_locks.get(name)
        if lock is None:
            lock = threading.RLock()
            _session_locks[name] = lock
        return lock


class YydsError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: Any = None,
        retry_after_seconds: int | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.retry_after_seconds = retry_after_seconds
        self.code = code


def parse_retry_after(headers: Any) -> int | None:
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if text.isdigit():
        seconds = int(text)
    else:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return None
    return min(seconds, 3600)


def _unwrap(payload: Any, status_code: int | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload
    if payload.get("success") is False:
        raise YydsError(
            str(payload.get("errorCode") or payload.get("error") or "yyds_error"),
            status_code=status_code,
            payload=payload,
        )
    assert_list_complete(payload)
    if "data" in payload:
        inner = payload.get("data")
        if isinstance(inner, dict):
            assert_list_complete(inner)
        return inner
    return payload


def _http_error(payload: Any, status_code: int, default: str, headers: Any = None) -> YydsError:
    message = default
    if isinstance(payload, dict):
        message = str(payload.get("errorCode") or payload.get("error") or default)
    return YydsError(
        f"{message} HTTP {status_code}",
        status_code,
        payload,
        retry_after_seconds=parse_retry_after(headers),
    )


_TWOFA_MARKERS = ("twofa", "two_fa", "2fa", "totp", "two-factor", "two factor")
_CAPTCHA_MARKERS = ("turnstile", "captcha", "hcaptcha", "recaptcha")


def _login_error_blob(exc: YydsError) -> str:
    parts = [str(exc), str(getattr(exc, "code", "") or "")]
    payload = exc.payload
    if isinstance(payload, dict):
        parts.extend(str(payload.get(key) or "") for key in ("error", "errorCode", "message", "msg"))
    else:
        parts.append(str(payload or ""))
    return " ".join(parts).lower()


def _clarify_login_error(exc: YydsError) -> YydsError:
    blob = _login_error_blob(exc)
    if any(marker in blob for marker in _TWOFA_MARKERS):
        return YydsError(
            "该账号开启了 2FA，请在面板填一次性二验码后再试。已停止对该账号加域",
            status_code=exc.status_code,
            payload=exc.payload,
            retry_after_seconds=exc.retry_after_seconds,
            code="TWO_FA_REQUIRED",
        )
    if any(marker in blob for marker in _CAPTCHA_MARKERS):
        return YydsError(
            "登录需要验证码（Turnstile），本监控不能自动过验证码。已停止对该账号加域",
            status_code=exc.status_code,
            payload=exc.payload,
            retry_after_seconds=exc.retry_after_seconds,
            code="TURNSTILE_REQUIRED",
        )
    return exc


def _is_auth_expired(exc: YydsError) -> bool:
    return exc.status_code == 401


def assert_list_complete(payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    for flag in (
        "hasMore",
        "has_more",
        "hasNextPage",
        "has_next_page",
        "isTruncated",
        "is_truncated",
        "truncated",
        "incomplete",
    ):
        if payload.get(flag) is True:
            raise YydsError("列表不完整，拒绝按截断结果对账", code="LIST_INCOMPLETE")
    next_cursor = payload.get("nextCursor")
    if next_cursor in (None, ""):
        next_cursor = payload.get("next_cursor")
    if next_cursor in (None, ""):
        next_cursor = payload.get("nextPage") or payload.get("next_page")
    if next_cursor not in (None, "", False, 0):
        raise YydsError("列表不完整，拒绝按截断结果对账", code="LIST_INCOMPLETE")
    items = None
    for key in ("items", "domains", "records", "rules", "list", "wildcardRules", "wildcard_rules"):
        value = payload.get(key)
        if isinstance(value, list):
            items = value
            break
    if items is None and isinstance(payload.get("data"), list):
        items = payload.get("data")
    total = payload.get("total")
    if total is None:
        total = payload.get("totalCount")
    if total is None:
        total = payload.get("total_count")
    if items is None or total in (None, ""):
        return
    try:
        total_n = int(total)
    except (TypeError, ValueError):
        return
    if total_n > len(items):
        raise YydsError("列表不完整，拒绝按截断结果对账", code="LIST_INCOMPLETE")


def try_as_list(payload: Any) -> list[Any]:
    if payload is None:
        raise YydsError("无法解析列表响应")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        found_empty = False
        for key in ("items", "domains", "records", "rules", "list", "wildcardRules", "wildcard_rules"):
            value = payload.get(key)
            if isinstance(value, list):
                if value:
                    return value
                found_empty = True
        if "data" in payload:
            return try_as_list(payload.get("data"))
        if found_empty:
            return []
        raise YydsError("无法解析列表响应")
    raise YydsError("无法解析列表响应")


def _as_list(payload: Any) -> list[Any]:
    return try_as_list(payload)


def extract_quota(me: Any, quota_payload: Any, wildcard_rules: list[Any], domains: list[Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for item in (me, quota_payload):
        if isinstance(item, dict):
            sources.append(item)
            for key in ("quota", "plan", "user", "limits", "usage"):
                nested = item.get(key)
                if isinstance(nested, dict):
                    sources.append(nested)
                    inner_plan = nested.get("plan")
                    if isinstance(inner_plan, dict):
                        sources.append(inner_plan)

    def pick(*names: str, nested_keys: tuple[str, ...] = ()) -> Any:
        for source in sources:
            for name in names:
                if source.get(name) not in (None, ""):
                    return source.get(name)
            for key in nested_keys:
                block = source.get(key)
                if not isinstance(block, dict):
                    continue
                for field in ("max", "limit", "quota"):
                    if block.get(field) not in (None, ""):
                        return block.get(field)
        return None

    def pick_used(names: tuple[str, ...], nested_keys: tuple[str, ...]) -> int | None:
        for source in sources:
            for name in names:
                if source.get(name) is not None:
                    try:
                        return int(source[name])
                    except (TypeError, ValueError):
                        pass
            for key in nested_keys:
                block = source.get(key)
                if isinstance(block, dict) and block.get("used") is not None:
                    try:
                        return int(block["used"])
                    except (TypeError, ValueError):
                        pass
        return None

    max_wildcard = pick(
        "maxWildcardRules",
        "wildcardRulesLimit",
        "max_wildcard_rules",
        nested_keys=("wildcardRules", "wildcard"),
    )
    max_domains = pick(
        "maxDomains",
        "domainsLimit",
        "max_domains",
        nested_keys=("domains",),
    )
    plan_name = pick("planName")
    if plan_name is None:
        plan_obj = pick("plan")
        if isinstance(plan_obj, dict):
            plan_name = plan_obj.get("name") or plan_obj.get("planName") or plan_obj.get("title")
        elif isinstance(plan_obj, str):
            plan_name = plan_obj
    if isinstance(plan_name, dict):
        plan_name = plan_name.get("name") or plan_name.get("planName") or plan_name.get("title")

    used_wildcard_from_quota = pick_used(
        ("usedWildcardRules", "wildcardRulesUsed", "used_wildcard_rules"),
        ("wildcardRules", "wildcard"),
    )
    used_domains_from_quota = pick_used(
        ("usedDomains", "domainsUsed", "used_domains"),
        ("domains",),
    )
    used_wildcard = used_wildcard_from_quota if used_wildcard_from_quota is not None else len(wildcard_rules)
    used_domains = used_domains_from_quota if used_domains_from_quota is not None else len(domains)

    def as_limit(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number

    return {
        "plan_name": str(plan_name or "").strip() or None,
        "max_wildcard": as_limit(max_wildcard),
        "used_wildcard": used_wildcard,
        "used_wildcard_from_quota": used_wildcard_from_quota,
        "max_domains": as_limit(max_domains),
        "used_domains": used_domains,
        "used_domains_from_quota": used_domains_from_quota,
    }


@dataclass
class YydsDomain:
    id: str
    domain: str
    is_public: bool | None
    verification_token: str | None
    raw: dict[str, Any]


def find_listed_domain(items: list[YydsDomain], name: str) -> YydsDomain | None:
    for item in items:
        if domains_match(item.domain, name):
            return item
    return None


class YydsClient:
    def __init__(
        self,
        api_base: str,
        username: str,
        password: str,
        cookies: list[dict[str, str]] | None = None,
        access_token: str | None = None,
        timeout: float = 30,
        session_key: str | None = None,
        on_session_change: Callable[[], None] | None = None,
        twofa_code: str | None = None,
        turnstile_token: str | None = None,
    ):
        self.api_base = api_base.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.access_token = access_token or ""
        self.session_key = session_key or username
        self.on_session_change = on_session_change
        self.twofa_code = (twofa_code or "").strip() or None
        self.turnstile_token = (turnstile_token or "").strip() or None
        self.twofa_consumed = False
        self.http = httpx.Client(
            base_url=self.api_base,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "domainslot/0.0.1"},
            follow_redirects=True,
        )
        if cookies:
            for item in cookies:
                name = item.get("name")
                value = item.get("value")
                if not name or value is None:
                    continue
                self.http.cookies.set(
                    name,
                    value,
                    domain=item.get("domain") or None,
                    path=item.get("path") or "/",
                )

    def close(self) -> None:
        self.http.close()

    def export_cookies(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for cookie in self.http.cookies.jar:
            items.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or "",
                    "path": cookie.path or "/",
                }
            )
        return items

    def _cookie_names(self) -> list[str]:
        return [cookie.name.lower() for cookie in self.http.cookies.jar]

    def _has_cookie(self, *needles: str) -> bool:
        names = self._cookie_names()
        return any(any(needle in name for needle in needles) for name in names)

    def _notify_session(self) -> None:
        callback = self.on_session_change
        if callback is not None:
            callback()

    def login(self) -> dict[str, Any]:
        with account_session_lock(self.session_key):
            body: dict[str, Any] = {"username": self.username, "password": self.password}
            if self.twofa_code:
                body["twofaCode"] = self.twofa_code
            if self.turnstile_token:
                body["turnstileToken"] = self.turnstile_token
            response = self.http.post("auth/login", json=body)
            payload = self._decode(response)
            try:
                if response.status_code >= 400:
                    raise _http_error(payload, response.status_code, "登录失败", response.headers)
                data = _unwrap(payload, status_code=response.status_code)
            except YydsError as exc:
                raise _clarify_login_error(exc) from exc
            if isinstance(data, dict):
                token = str(data.get("access_token") or data.get("accessToken") or "")
                if token:
                    self.access_token = token
            if self.twofa_code:
                self.twofa_consumed = True
            self._notify_session()
            return data if isinstance(data, dict) else {"raw": data}

    def refresh(self) -> None:
        with account_session_lock(self.session_key):
            response = self.http.post("auth/token/refresh")
            if response.status_code == 401:
                self.login()
                return
            payload = self._decode(response)
            if response.status_code >= 400:
                raise _http_error(payload, response.status_code, "刷新失败", response.headers)
            data = _unwrap(payload, status_code=response.status_code)
            if isinstance(data, dict):
                token = str(data.get("access_token") or data.get("accessToken") or "")
                if token:
                    self.access_token = token
            self._notify_session()

    def ensure_session(self) -> None:
        with account_session_lock(self.session_key):
            if self.access_token or self._has_cookie("access"):
                try:
                    self.get_me()
                    return
                except YydsError as exc:
                    if not _is_auth_expired(exc):
                        raise
            if self._has_cookie("refresh"):
                try:
                    self.refresh()
                    self.get_me()
                    return
                except YydsError as exc:
                    if not _is_auth_expired(exc):
                        raise
            self.login()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        with account_session_lock(self.session_key):
            self.ensure_session()
            response = self._send(method, path, **kwargs)
            if response.status_code == 401:
                try:
                    self.refresh()
                except YydsError as exc:
                    if not _is_auth_expired(exc):
                        raise
                    self.login()
                response = self._send(method, path, **kwargs)
        if response.status_code == 204:
            return None
        payload = self._decode(response)
        if response.status_code >= 400:
            raise _http_error(payload, response.status_code, "yyds 请求失败", response.headers)
        return _unwrap(payload, status_code=response.status_code)

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.access_token:
            headers.setdefault("Authorization", f"Bearer {self.access_token}")
        url = path.lstrip("/")
        return self.http.request(method.upper(), url, headers=headers, **kwargs)

    def _decode(self, response: httpx.Response) -> Any:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text[:500]}

    def get_me(self) -> Any:
        response = self._send("GET", "me")
        payload = self._decode(response)
        if response.status_code == 401:
            raise YydsError("未登录", 401, payload)
        if response.status_code >= 400:
            raise _http_error(payload, response.status_code, "GET /me 失败", response.headers)
        return _unwrap(payload, status_code=response.status_code)

    def get_quota(self) -> Any:
        return self.request("GET", "me/quota")

    def list_domains(self) -> list[YydsDomain]:
        payload = self.request("GET", "me/domains")
        assert_list_complete(payload)
        items: list[YydsDomain] = []
        for raw in try_as_list(payload):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("domain") or raw.get("name") or "").strip()
            domain_id = str(raw.get("id") or "")
            if not name or not domain_id:
                continue
            try:
                name = normalize_domain(name)
            except ValueError:
                name = name.rstrip(".").lower()
            items.append(
                YydsDomain(
                    id=domain_id,
                    domain=name,
                    is_public=raw.get("isPublic") if isinstance(raw.get("isPublic"), bool) else None,
                    verification_token=str(raw.get("verificationToken") or "") or None,
                    raw=raw,
                )
            )
        return items

    def list_wildcard_rules(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "me/wildcard-rules")
        assert_list_complete(payload)
        return [item for item in try_as_list(payload) if isinstance(item, dict)]

    def add_domain(self, domain: str, enable_wildcard: bool = True) -> dict[str, Any]:
        payload = self.request(
            "POST",
            "me/domains",
            json={"domain": domain, "enableWildcardRule": enable_wildcard},
        )
        return payload if isinstance(payload, dict) else {"raw": payload}

    def dns_guide(self, domain_id: str) -> Any:
        return self.request("GET", f"me/domains/{domain_id}/dns-guide")

    def dns_status(self, domain_id: str) -> Any:
        return self.request("GET", f"me/domains/{domain_id}/dns-status")

    def verify_domain(self, domain_id: str) -> Any:
        return self.request("POST", f"me/domains/{domain_id}/verify")

    def batch_verify(self, domain_ids: list[str]) -> Any:
        ids = [str(item).strip() for item in domain_ids if str(item).strip()]
        return self.request("POST", "me/domains/batch-verify", json={"domainIds": ids})

    def _domain_is_private(self, domain_id: str) -> bool:
        for item in self.list_domains():
            if str(item.id) == str(domain_id):
                return item.is_public is False
        return False

    def set_private(self, domain_id: str) -> bool:
        if self._domain_is_private(domain_id):
            return True
        body_errors = {400, 404, 405, 415, 422}
        try:
            self.request("PATCH", f"me/domains/{domain_id}")
        except YydsError as exc:
            if exc.status_code not in body_errors:
                raise
        if self._domain_is_private(domain_id):
            return True
        try:
            self.request("PATCH", f"me/domains/{domain_id}", json={"isPublic": False})
        except YydsError as exc:
            if exc.status_code not in body_errors:
                raise
        return self._domain_is_private(domain_id)

    def ensure_wildcard_rule(self, domain_id: str) -> bool:
        existing = self.request("GET", f"me/domains/{domain_id}/wildcard-rules")
        if try_as_list(existing):
            return True
        try:
            self.request("POST", f"me/domains/{domain_id}/wildcard-rules")
            return True
        except YydsError as exc:
            if exc.status_code != 409:
                raise
            existing = self.request("GET", f"me/domains/{domain_id}/wildcard-rules")
            if try_as_list(existing):
                return True
            raise YydsError("通配规则冲突且列表里没有该规则", 409, exc.payload) from exc

    def verify_ready(self, payload: Any) -> tuple[bool, str]:
        data = payload
        if isinstance(payload, dict) and payload.get("data") is not None:
            inner = payload.get("data")
            if isinstance(inner, (dict, list)):
                data = inner
        if isinstance(data, list):
            data = {"items": data}
        if not isinstance(data, dict):
            return False, "dns_propagating"
        items = data.get("items")
        top_result = data.get("result") or data.get("status") or data.get("receivingReady")
        if isinstance(items, list) and items and top_result in (None, ""):
            last = (False, "dns_propagating")
            for item in items:
                if not isinstance(item, dict):
                    continue
                last = self.verify_ready(item)
                if last[0]:
                    return last
            return last
        result = str(data.get("result") or data.get("status") or "").strip().lower()
        dns = data.get("dnsRecords") if isinstance(data.get("dnsRecords"), dict) else data
        if data.get("receivingReady") is True or (isinstance(dns, dict) and dns.get("receivingReady") is True):
            return True, result or "receiving_ready"
        if result == "receiving_ready":
            return True, result
        if result in {"dns_propagating", "txt_missing", "mx_missing", "wildcard_mx_missing", "record_conflict", "check_failed"}:
            return False, result
        if result:
            return False, "dns_propagating"
        mx_valid = data.get("isMxValid")
        if mx_valid is None and isinstance(dns, dict):
            mx_valid = dns.get("isMxValid")
            if mx_valid is None:
                mx_valid = dns.get("mxValid")
        verified = data.get("isVerified")
        if verified is None and isinstance(dns, dict):
            verified = dns.get("isVerified")
        if mx_valid is False:
            return False, "mx_missing"
        if isinstance(dns, dict):
            if dns.get("wildcardMxRequired") is True and dns.get("wildcardMxValid") is not True:
                return False, "wildcard_mx_missing"
            if dns.get("ownershipValid") is False:
                return False, "txt_missing"
        if verified is True:
            return False, "dns_propagating"
        if verified is False:
            return False, "txt_missing"
        return False, "dns_propagating"
