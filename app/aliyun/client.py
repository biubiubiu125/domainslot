from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from alibabacloud_alidns20150109.client import Client as AlidnsClient
from alibabacloud_alidns20150109 import models as dns_models
from alibabacloud_domain20180129.client import Client as DomainClient
from alibabacloud_domain20180129 import models as domain_models
from alibabacloud_tea_openapi import models as open_api_models
from Tea.exceptions import TeaException

from app.aliyun.records import WantedRecord, aliyun_nameservers_ok, is_conflict_record, mailbox_cleanup_wanted


class AliyunError(Exception):
    def __init__(self, message: str, code: str | None = None, throttled: bool = False):
        super().__init__(message)
        self.code = code
        self.throttled = throttled


def _record_identity(typ: Any, rr: Any, value: Any, priority: Any) -> tuple[str, str, str, str]:
    host = str(rr or "@").strip().lower() or "@"
    return (
        str(typ or "").upper(),
        host,
        str(value or ""),
        "" if priority is None else str(priority),
    )


def _is_throttle(code: str | None, message: str) -> bool:
    blob = f"{code or ''} {message}".lower()
    return "throttl" in blob or "flowcontrol" in blob or "serviceunavailable" in blob


def _unwrap_dict_message(raw: str) -> str:
    text = (raw or "").strip()
    if not (text.startswith("{") and "message" in text.lower()):
        return text
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text
    if isinstance(parsed, dict):
        inner = parsed.get("Message") or parsed.get("message")
        if inner:
            return str(inner)
    return text


def _exception_text(exc: BaseException) -> str:
    parts: list[str] = []
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        for key in ("Message", "message"):
            value = data.get(key)
            if value:
                parts.append(str(value))
    for attr in ("message", "msg"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(str(value))
    inner = getattr(exc, "inner_exception", None)
    if isinstance(inner, BaseException):
        parts.append(_exception_text(inner))
    parts.append(str(exc))
    return "\n".join(part for part in parts if part)


def _preferred_raw_message(exc: BaseException) -> str:
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        for key in ("Message", "message"):
            value = data.get(key)
            if value:
                return str(value)
    inner = getattr(exc, "inner_exception", None)
    if isinstance(inner, BaseException):
        inner_msg = _preferred_raw_message(inner)
        if inner_msg:
            return inner_msg
    value = getattr(exc, "message", None)
    if value:
        return str(value)
    return str(exc)


def _network_aliyun_message(message: str, code: str | None = None) -> str | None:
    blob = f"{code or ''} {message}".lower()
    if any(token in blob for token in ("connect timeout", "connecttimeouterror", "connecttimeout")):
        return "连接阿里云超时"
    if any(token in blob for token in ("read timeout", "readtimeouterror", "readtimeout")):
        return "读取阿里云响应超时"
    if any(
        token in blob
        for token in (
            "max retries exceeded",
            "failed to establish a new connection",
            "name or service not known",
            "temporary failure in name resolution",
            "connection refused",
            "connection aborted",
        )
    ):
        return "无法连接到阿里云"
    return None


def _tea_error(exc: TeaException) -> AliyunError:
    code = None
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        code = str(data.get("Code") or data.get("code") or "") or None
    if not code and getattr(exc, "code", None):
        code = str(exc.code) or None
    if code in {"None", "none"}:
        code = None
    raw = _exception_text(exc)
    preferred = _unwrap_dict_message(_preferred_raw_message(exc))
    message = _network_aliyun_message(raw, code) or preferred or str(exc)
    return AliyunError(message, code=code, throttled=_is_throttle(code, raw))


def _int_field(value: Any, *names: str) -> int | None:
    if value is None:
        return None
    mapped = None
    for name in names:
        raw = getattr(value, name, None)
        if raw is None:
            if mapped is None:
                mapped = _to_map(value)
            raw = mapped.get(name)
        if raw in (None, ""):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _bool_field(value: Any, *names: str) -> bool | None:
    if value is None:
        return None
    mapped = None
    for name in names:
        raw = getattr(value, name, None)
        if raw is None:
            if mapped is None:
                mapped = _to_map(value)
            raw = mapped.get(name)
        if raw is True or raw is False:
            return raw
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
    return None


def _should_fetch_next_page(
    *,
    next_page: bool | None,
    page_size: int,
    raw_count: int,
    collected: int,
    total: int | None,
    page: int,
    page_cap: int,
) -> bool:
    if page >= page_cap:
        return False
    if next_page is True:
        return True
    if total is not None and collected >= total:
        return False
    if total is not None and collected < total:
        return True
    if next_page is False:
        return False
    return raw_count >= page_size


def _merge_aliyun_domains(primary: list[AliyunDomain], extra: list[AliyunDomain]) -> list[AliyunDomain]:
    merged: dict[str, AliyunDomain] = {}
    order: list[str] = []
    for source in (primary, extra):
        for item in source:
            key = (item.name or "").strip().rstrip(".").lower()
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
                order.append(key)
                continue
            if not existing.registration_at and item.registration_at:
                existing.registration_at = item.registration_at
            if not existing.domain_status and item.domain_status:
                existing.domain_status = item.domain_status
            if not existing.audit_status and item.audit_status:
                existing.audit_status = item.audit_status
            if not existing.nameservers and item.nameservers:
                existing.nameservers = list(item.nameservers)
            if not existing.client_hold and item.client_hold:
                existing.client_hold = item.client_hold
    return [merged[key] for key in order]


def _to_map(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_map"):
        mapped = value.to_map()
        return mapped if isinstance(mapped, dict) else {}
    data: dict[str, Any] = {}
    for key in dir(value):
        if key.startswith("_"):
            continue
        attr = getattr(value, key)
        if callable(attr):
            continue
        data[key] = attr
    return data


def parse_aliyun_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.replace("Z", ""), fmt.replace("Z", "") if "T" in fmt else fmt)
            return dt
        except ValueError:
            continue
    return None


@dataclass
class AliyunDomain:
    name: str
    registration_at: datetime | None = None
    domain_status: str | None = None
    audit_status: str | None = None
    nameservers: list[str] = field(default_factory=list)
    client_hold: bool = False


class AliyunClient:
    CONNECT_TIMEOUT = 10000
    READ_TIMEOUT = 30000

    def __init__(self, access_key_id: str, access_key_secret: str):
        domain_config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            connect_timeout=self.CONNECT_TIMEOUT,
            read_timeout=self.READ_TIMEOUT,
        )
        domain_config.endpoint = "domain.aliyuncs.com"
        dns_config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            connect_timeout=self.CONNECT_TIMEOUT,
            read_timeout=self.READ_TIMEOUT,
        )
        dns_config.endpoint = "alidns.aliyuncs.com"
        self.domain_api = DomainClient(domain_config)
        self.dns_api = AlidnsClient(dns_config)

    def list_domains(self) -> list[AliyunDomain]:
        try:
            items = self._paged_domain_list(
                lambda page, size: domain_models.QueryAdvancedDomainListRequest(page_num=page, page_size=size),
                self.domain_api.query_advanced_domain_list,
            )
        except AliyunError as exc:
            if exc.throttled:
                raise
            return self._paged_domain_list(
                lambda page, size: domain_models.QueryDomainListRequest(page_num=page, page_size=size),
                self.domain_api.query_domain_list,
            )
        if not items:
            return self._paged_domain_list(
                lambda page, size: domain_models.QueryDomainListRequest(page_num=page, page_size=size),
                self.domain_api.query_domain_list,
            )
        try:
            extra = self._paged_domain_list(
                lambda page, size: domain_models.QueryDomainListRequest(page_num=page, page_size=size),
                self.domain_api.query_domain_list,
            )
        except AliyunError:
            return items
        return _merge_aliyun_domains(items, extra)

    def _paged_domain_list(self, request_factory, caller) -> list[AliyunDomain]:
        page = 1
        page_size = 50
        items: list[AliyunDomain] = []
        while True:
            request = request_factory(page, page_size)
            try:
                response = caller(request)
            except TeaException as exc:
                raise _tea_error(exc) from exc
            body = getattr(response, "body", None)
            data = getattr(body, "data", None)
            raw_list = getattr(data, "domain", None) if data is not None else None
            if raw_list is None:
                mapped_body = _to_map(body or response)
                data_map = mapped_body.get("data") or mapped_body.get("Data") or mapped_body
                raw_list = data_map.get("domain") or data_map.get("Domain") or [] if isinstance(data_map, dict) else []
            if not isinstance(raw_list, list):
                raw_list = [raw_list] if raw_list else []
            for raw in raw_list:
                name = str(
                    getattr(raw, "domain_name", None)
                    or _to_map(raw).get("DomainName")
                    or _to_map(raw).get("domain_name")
                    or ""
                ).strip()
                if not name:
                    continue
                items.append(
                    AliyunDomain(
                        name=name,
                        registration_at=parse_aliyun_datetime(
                            getattr(raw, "registration_date", None)
                            or _to_map(raw).get("RegistrationDate")
                            or None
                        ),
                        domain_status=str(
                            getattr(raw, "domain_status", None)
                            or _to_map(raw).get("DomainStatus")
                            or ""
                        )
                        or None,
                        audit_status=_audit_status_from(raw),
                        nameservers=_nameservers_from(raw),
                        client_hold=_client_hold_from(raw),
                    )
                )
            next_page = _bool_field(body, "next_page", "NextPage")
            total_items = _int_field(body, "total_item_num", "TotalItemNum")
            if not _should_fetch_next_page(
                next_page=next_page,
                page_size=page_size,
                raw_count=len(raw_list),
                collected=len(items),
                total=total_items,
                page=page,
                page_cap=200,
            ):
                break
            page += 1
        return items

    def describe_registrar_domain(self, domain: str) -> AliyunDomain:
        request = domain_models.QueryDomainByDomainNameRequest(domain_name=domain)
        try:
            response = self.domain_api.query_domain_by_domain_name(request)
        except TeaException as exc:
            raise _tea_error(exc) from exc
        body = getattr(response, "body", None) or response
        mapped = _to_map(body)
        name = str(getattr(body, "domain_name", None) or mapped.get("DomainName") or domain).strip() or domain
        return AliyunDomain(
            name=name,
            registration_at=parse_aliyun_datetime(
                getattr(body, "registration_date", None) or mapped.get("RegistrationDate")
            ),
            domain_status=str(getattr(body, "domain_status", None) or mapped.get("DomainStatus") or "") or None,
            audit_status=_audit_status_from(body),
            nameservers=_nameservers_from(body),
            client_hold=_client_hold_from(body),
        )

    def describe_registrar_nameservers(self, domain: str) -> list[str]:
        return self.describe_registrar_domain(domain).nameservers

    def describe_nameservers(self, domain: str) -> list[str]:
        request = dns_models.DescribeDomainInfoRequest(domain_name=domain)
        try:
            response = self.dns_api.describe_domain_info(request)
        except TeaException as exc:
            err = _tea_error(exc)
            if err.code and "InvalidDomainName.NoExist" in err.code:
                return []
            raise err from exc
        wrap = getattr(getattr(response, "body", None), "dns_servers", None)
        values = [str(item) for item in (getattr(wrap, "dns_server", None) or [])]
        if values:
            return values
        body = _to_map(getattr(response, "body", response))
        ns_raw = body.get("DnsServers") or body.get("dns_servers") or {}
        if isinstance(ns_raw, dict):
            dns = ns_raw.get("DnsServer") or ns_raw.get("dns_server") or []
            if isinstance(dns, list):
                return [str(item) for item in dns]
        if isinstance(ns_raw, list):
            return [str(item) for item in ns_raw]
        return []

    def ensure_dns_domain(self, domain: str) -> None:
        request = dns_models.AddDomainRequest(domain_name=domain)
        try:
            self.dns_api.add_domain(request)
        except TeaException as exc:
            err = _tea_error(exc)
            code = str(err.code or "")
            if any(token in code for token in ("DomainDuplicate", "InvalidDomainName.Duplicate", "DomainAlreadyExists")):
                return
            raise err from exc

    def list_records(self, domain: str) -> list[dict[str, Any]]:
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            request = dns_models.DescribeDomainRecordsRequest(
                domain_name=domain,
                page_number=page,
                page_size=100,
            )
            try:
                response = self.dns_api.describe_domain_records(request)
            except TeaException as exc:
                raise _tea_error(exc) from exc
            wrap = getattr(getattr(response, "body", None), "domain_records", None)
            raw = list(getattr(wrap, "record", None) or [])
            if not raw:
                body = _to_map(getattr(response, "body", response))
                records_wrap = body.get("DomainRecords") or body.get("domain_records") or {}
                if isinstance(records_wrap, dict):
                    raw = records_wrap.get("Record") or records_wrap.get("record") or []
            if not isinstance(raw, list):
                raw = [raw] if raw else []
            for item in raw:
                mapped = _to_map(item)
                items.append(
                    {
                        "record_id": str(
                            getattr(item, "record_id", None) or mapped.get("RecordId") or mapped.get("record_id") or ""
                        ),
                        "rr": str(getattr(item, "rr", None) or mapped.get("RR") or mapped.get("rr") or "@"),
                        "type": str(getattr(item, "type", None) or mapped.get("Type") or mapped.get("type") or ""),
                        "value": str(getattr(item, "value", None) or mapped.get("Value") or mapped.get("value") or ""),
                        "priority": getattr(item, "priority", None) or mapped.get("Priority") or mapped.get("priority"),
                        "line": str(getattr(item, "line", None) or mapped.get("Line") or mapped.get("line") or "default"),
                    }
                )
            body = getattr(response, "body", None)
            total_count = _int_field(body, "total_count", "TotalCount")
            if not _should_fetch_next_page(
                next_page=None,
                page_size=100,
                raw_count=len(raw),
                collected=len(items),
                total=total_count,
                page=page,
                page_cap=50,
            ):
                break
            page += 1
        return items

    def delete_record(self, record_id: str) -> None:
        request = dns_models.DeleteDomainRecordRequest(record_id=record_id)
        try:
            self.dns_api.delete_domain_record(request)
        except TeaException as exc:
            raise _tea_error(exc) from exc

    def add_record(self, domain: str, wanted: WantedRecord) -> None:
        request = dns_models.AddDomainRecordRequest(
            domain_name=domain,
            rr=wanted.rr,
            type=wanted.type,
            value=wanted.value,
            ttl=600,
            line="default",
        )
        if wanted.type == "MX" and wanted.priority is not None:
            request.priority = wanted.priority
        try:
            self.dns_api.add_domain_record(request)
        except TeaException as exc:
            err = _tea_error(exc)
            code = str(err.code or "")
            if "DomainRecordDuplicate" in code:
                return
            raise err from exc

    def apply_guide(self, domain: str, wanted: list[WantedRecord]) -> dict[str, int]:
        try:
            registrar_ns = self.describe_registrar_nameservers(domain)
        except AliyunError as exc:
            raise AliyunError(f"无法读取注册商 NS: {exc}", code=exc.code or "NS_UNKNOWN", throttled=exc.throttled) from exc
        if not registrar_ns:
            raise AliyunError("无法读取注册商 NS", code="NS_UNKNOWN")
        if not aliyun_nameservers_ok(registrar_ns):
            raise AliyunError(f"NS 不是阿里云: {', '.join(registrar_ns)}", code="NS_NOT_ALIYUN")
        self.ensure_dns_domain(domain)
        records = self.list_records(domain)
        wanted_keys = {_record_identity(item.type, item.rr, item.value, item.priority) for item in wanted}
        deleted = 0
        for record in records:
            key = _record_identity(record.get("type"), record.get("rr"), record.get("value"), record.get("priority"))
            if key in wanted_keys:
                continue
            if is_conflict_record(record["rr"], record["type"], wanted) and record["record_id"]:
                self.delete_record(record["record_id"])
                deleted += 1
        added = 0
        existing_after = self.list_records(domain)
        existing_keys = {
            _record_identity(item.get("type"), item.get("rr"), item.get("value"), item.get("priority"))
            for item in existing_after
        }
        for item in wanted:
            key = _record_identity(item.type, item.rr, item.value, item.priority)
            if key in existing_keys:
                continue
            self.add_record(domain, item)
            added += 1
        return {"deleted": deleted, "added": added}

    def delete_mailbox_records(self, domain: str, wanted: list[WantedRecord] | None = None) -> dict[str, int]:
        records = self.list_records(domain)
        wanted = list(wanted or mailbox_cleanup_wanted())
        deleted = 0
        for record in records:
            if is_conflict_record(record["rr"], record["type"], wanted) and record["record_id"]:
                self.delete_record(record["record_id"])
                deleted += 1
        return {"deleted": deleted}


def _client_hold_from(raw: Any) -> bool:
    mapped = _to_map(raw)
    value = getattr(raw, "email_verification_client_hold", None)
    if value is None:
        value = mapped.get("EmailVerificationClientHold")
    if value is None:
        value = mapped.get("email_verification_client_hold")
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return value is True


def _audit_status_from(raw: Any) -> str | None:
    mapped = _to_map(raw)
    values: list[str] = []
    for item in (
        getattr(raw, "domain_audit_status", None),
        mapped.get("DomainAuditStatus"),
        getattr(raw, "real_name_status", None),
        mapped.get("RealNameStatus"),
        getattr(raw, "domain_name_verification_status", None),
        mapped.get("DomainNameVerificationStatus"),
        mapped.get("domain_name_verification_status"),
    ):
        text = str(item or "").strip()
        if text and text not in values:
            values.append(text)
    return ",".join(values) or None


def _nameservers_from(raw: Any) -> list[str]:
    wrap = getattr(raw, "dns_list", None)
    values = getattr(wrap, "dns", None) if wrap is not None else None
    if isinstance(values, list) and values:
        return [str(item) for item in values if item]
    mapped = _to_map(raw)
    ns = mapped.get("DnsList") or mapped.get("dns_list") or {}
    if isinstance(ns, dict):
        vals = ns.get("Dns") or ns.get("dns") or []
        if isinstance(vals, list):
            return [str(item) for item in vals if item]
    if isinstance(ns, list):
        return [str(item) for item in ns if item]
    return []


def domain_ready_for_fill(
    item: AliyunDomain,
    *,
    require_nameservers: bool = False,
    require_audit: bool = False,
) -> tuple[bool, str | None]:
    if getattr(item, "client_hold", False) is True:
        return False, "域名处于 ClientHold"
    status = str(item.domain_status or "").strip().lower()
    if status == "2":
        return False, "域名处于赎回状态"
    if any(token in status for token in ("hold", "pending", "forbid", "clienthold", "serverhold")):
        return False, f"域名状态异常: {item.domain_status}"
    audit_raw = (item.audit_status or "").strip()
    if require_audit and not audit_raw:
        return False, "无法读取实名审核状态"
    tokens = {
        part.strip().upper()
        for part in audit_raw.replace(",", " ").replace(";", " ").split()
        if part.strip()
    }
    if tokens & {"FAILED", "NONAUDIT", "AUDITING"}:
        return False, f"未实名或审核未通过: {item.audit_status}"
    if any(token in audit_raw for token in ("未实名", "审核失败", "审核中", "未认证")):
        return False, f"未实名或审核未通过: {item.audit_status}"
    if require_nameservers and not item.nameservers:
        return False, "无法读取注册商 NS"
    if item.nameservers and not aliyun_nameservers_ok(item.nameservers):
        return False, f"NS 不是阿里云: {', '.join(item.nameservers)}"
    return True, None
