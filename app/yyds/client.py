from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx


class YydsError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _unwrap(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if payload.get("success") is False:
        raise YydsError(str(payload.get("errorCode") or payload.get("error") or "yyds_error"), payload=payload)
    if "data" in payload:
        return payload.get("data")
    return payload


def _as_list(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "domains", "records", "rules", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if "data" in payload:
            return _as_list(payload.get("data"))
    return []


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

    def pick(*names: str) -> Any:
        for source in sources:
            for name in names:
                if source.get(name) not in (None, ""):
                    return source.get(name)
        return None

    def pick_used(names: tuple[str, ...], nested_keys: tuple[str, ...], fallback: int) -> int:
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
        return fallback

    max_wildcard = pick("maxWildcardRules", "wildcardRulesLimit", "max_wildcard_rules")
    max_domains = pick("maxDomains", "domainsLimit", "max_domains")
    plan_name = pick("planName", "displayName")
    if plan_name is None:
        plan_name = pick("plan")
    if isinstance(plan_name, dict):
        plan_name = plan_name.get("name") or plan_name.get("id") or plan_name.get("planName")

    used_wildcard = pick_used(
        ("usedWildcardRules", "wildcardRulesUsed", "used_wildcard_rules"),
        ("wildcardRules", "wildcard"),
        len(wildcard_rules),
    )
    used_domains = pick_used(
        ("usedDomains", "domainsUsed", "used_domains"),
        ("domains",),
        len(domains),
    )

    def as_limit(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if number < 0:
            return None
        return number

    return {
        "plan_name": str(plan_name or "").strip() or None,
        "max_wildcard": as_limit(max_wildcard),
        "used_wildcard": used_wildcard,
        "max_domains": as_limit(max_domains),
        "used_domains": used_domains,
    }


@dataclass
class YydsDomain:
    id: str
    domain: str
    is_public: bool | None
    verification_token: str | None
    raw: dict[str, Any]


class YydsClient:
    def __init__(
        self,
        api_base: str,
        username: str,
        password: str,
        cookies: list[dict[str, str]] | None = None,
        access_token: str | None = None,
        timeout: float = 30,
    ):
        self.api_base = api_base.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.access_token = access_token or ""
        self._refresh_lock = threading.Lock()
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

    def login(self) -> dict[str, Any]:
        response = self.http.post("auth/login", json={"username": self.username, "password": self.password})
        payload = self._decode(response)
        data = _unwrap(payload)
        if isinstance(data, dict):
            token = str(data.get("access_token") or data.get("accessToken") or "")
            if token:
                self.access_token = token
        if response.status_code >= 400:
            raise YydsError(f"登录失败 HTTP {response.status_code}", response.status_code, payload)
        return data if isinstance(data, dict) else {"raw": data}

    def refresh(self) -> None:
        response = self.http.post("auth/token/refresh")
        if response.status_code >= 400:
            self.login()
            return
        data = _unwrap(self._decode(response))
        if isinstance(data, dict):
            token = str(data.get("access_token") or data.get("accessToken") or "")
            if token:
                self.access_token = token

    def ensure_session(self) -> None:
        with self._refresh_lock:
            if self.access_token or self._has_cookie("access"):
                try:
                    self.get_me()
                    return
                except YydsError:
                    pass
            if self._has_cookie("refresh"):
                try:
                    self.refresh()
                    self.get_me()
                    return
                except YydsError:
                    pass
            self.login()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.ensure_session()
        response = self._send(method, path, **kwargs)
        if response.status_code == 401:
            with self._refresh_lock:
                try:
                    self.refresh()
                except YydsError:
                    self.login()
            response = self._send(method, path, **kwargs)
        if response.status_code == 204:
            return None
        payload = self._decode(response)
        if response.status_code >= 400:
            message = "yyds 请求失败"
            if isinstance(payload, dict):
                message = str(payload.get("errorCode") or payload.get("error") or message)
            raise YydsError(f"{message} HTTP {response.status_code}", response.status_code, payload)
        return _unwrap(payload)

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
        if response.status_code == 401:
            raise YydsError("未登录", 401, self._decode(response))
        if response.status_code >= 400:
            raise YydsError(f"GET /me HTTP {response.status_code}", response.status_code, self._decode(response))
        return _unwrap(self._decode(response))

    def get_quota(self) -> Any:
        return self.request("GET", "me/quota")

    def list_domains(self) -> list[YydsDomain]:
        payload = self.request("GET", "me/domains")
        items: list[YydsDomain] = []
        for raw in _as_list(payload):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("domain") or raw.get("name") or "").strip()
            domain_id = str(raw.get("id") or "")
            if not name or not domain_id:
                continue
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
        return [item for item in _as_list(payload) if isinstance(item, dict)]

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

    def set_private(self, domain_id: str) -> None:
        try:
            self.request("PATCH", f"me/domains/{domain_id}", json={"isPublic": False})
        except YydsError:
            return

    def ensure_wildcard_rule(self, domain_id: str) -> None:
        try:
            existing = self.request("GET", f"me/domains/{domain_id}/wildcard-rules")
            if _as_list(existing):
                return
        except YydsError:
            pass
        try:
            self.request("POST", f"me/domains/{domain_id}/wildcard-rules", json={})
        except YydsError:
            return

    def verify_ready(self, payload: Any) -> tuple[bool, str]:
        data = payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            data = payload["data"]
        if not isinstance(data, dict):
            return False, "unknown"
        result = str(data.get("result") or data.get("status") or "").strip().lower()
        dns = data.get("dnsRecords") if isinstance(data.get("dnsRecords"), dict) else data
        if data.get("receivingReady") is True or (isinstance(dns, dict) and dns.get("receivingReady") is True):
            return True, result or "receiving_ready"
        if result in {"receiving_ready", "verified", "ok", "healthy"}:
            return True, result
        if result:
            return False, result
        if isinstance(dns, dict):
            if dns.get("wildcardMxRequired") and not dns.get("wildcardMxValid"):
                return False, "wildcard_mx_missing"
            if dns.get("mxValid") is False:
                return False, "mx_missing"
            if dns.get("ownershipValid") is False:
                return False, "txt_missing"
        return False, "dns_propagating"
