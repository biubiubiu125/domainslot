from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from alibabacloud_alidns20150109.client import Client as AlidnsClient
from alibabacloud_alidns20150109 import models as dns_models
from alibabacloud_domain20180129.client import Client as DomainClient
from alibabacloud_domain20180129 import models as domain_models
from alibabacloud_tea_openapi import models as open_api_models
from Tea.exceptions import TeaException

from app.aliyun.records import WantedRecord, aliyun_nameservers_ok, is_conflict_record


class AliyunError(Exception):
    def __init__(self, message: str, code: str | None = None, throttled: bool = False):
        super().__init__(message)
        self.code = code
        self.throttled = throttled


def _is_throttle(code: str | None, message: str) -> bool:
    blob = f"{code or ''} {message}".lower()
    return "throttl" in blob or "flowcontrol" in blob or "serviceunavailable" in blob


def _tea_error(exc: TeaException) -> AliyunError:
    code = None
    message = str(exc)
    if getattr(exc, "data", None) and isinstance(exc.data, dict):
        code = str(exc.data.get("Code") or exc.data.get("code") or "")
        message = str(exc.data.get("Message") or exc.data.get("message") or message)
    elif getattr(exc, "code", None):
        code = str(exc.code)
    return AliyunError(message, code=code, throttled=_is_throttle(code, message))


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


class AliyunClient:
    def __init__(self, access_key_id: str, access_key_secret: str):
        domain_config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )
        domain_config.endpoint = "domain.aliyuncs.com"
        dns_config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )
        dns_config.endpoint = "alidns.aliyuncs.com"
        self.domain_api = DomainClient(domain_config)
        self.dns_api = AlidnsClient(dns_config)

    def list_domains(self) -> list[AliyunDomain]:
        page = 1
        page_size = 50
        items: list[AliyunDomain] = []
        while True:
            request = domain_models.QueryDomainListRequest(page_num=page, page_size=page_size)
            try:
                response = self.domain_api.query_domain_list(request)
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
                        audit_status=str(
                            getattr(raw, "domain_audit_status", None)
                            or _to_map(raw).get("DomainAuditStatus")
                            or ""
                        )
                        or None,
                        nameservers=[],
                    )
                )
            next_page = getattr(body, "next_page", None)
            if next_page is False or len(raw_list) < page_size:
                break
            page += 1
            if page > 200:
                break
        return items

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
            if len(raw) < 100:
                break
            page += 1
            if page > 50:
                break
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
        nameservers = self.describe_nameservers(domain)
        if not nameservers:
            self.ensure_dns_domain(domain)
            nameservers = self.describe_nameservers(domain)
        if nameservers and not aliyun_nameservers_ok(nameservers):
            raise AliyunError(f"NS 不是阿里云: {', '.join(nameservers)}", code="NS_NOT_ALIYUN")
        records = self.list_records(domain)
        deleted = 0
        for record in records:
            if is_conflict_record(record["rr"], record["type"], wanted) and record["record_id"]:
                self.delete_record(record["record_id"])
                deleted += 1
        added = 0
        existing_after = self.list_records(domain)
        existing_keys = {
            (item["type"].upper(), item["rr"].lower(), item["value"], str(item.get("priority") or ""))
            for item in existing_after
        }
        for item in wanted:
            key = (item.type.upper(), item.rr.lower(), item.value, str(item.priority or ""))
            if key in existing_keys:
                continue
            self.add_record(domain, item)
            added += 1
        return {"deleted": deleted, "added": added}


def domain_ready_for_fill(item: AliyunDomain) -> tuple[bool, str | None]:
    status = str(item.domain_status or "").strip().lower()
    if status == "2":
        return False, "域名处于赎回状态"
    if any(token in status for token in ("hold", "pending", "forbid", "clienthold", "serverhold")):
        return False, f"域名状态异常: {item.domain_status}"
    audit = (item.audit_status or "").upper()
    if audit in {"FAILED", "NONAUDIT", "AUDITING"}:
        return False, f"未实名或审核未通过: {item.audit_status}"
    if item.nameservers and not aliyun_nameservers_ok(item.nameservers):
        return False, f"NS 不是阿里云: {', '.join(item.nameservers)}"
    return True, None
