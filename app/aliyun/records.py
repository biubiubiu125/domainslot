from __future__ import annotations

from dataclasses import dataclass

from app.domainutil import domains_match


ALIYUN_NS_MARKERS = (
    "hichina.com",
    "alidns.com",
    "aliyun.com",
    "aliyun-dns.com",
    "aliyundns.com",
)

PROTECTED_TYPES = {"NS", "SOA"}


@dataclass(frozen=True)
class WantedRecord:
    type: str
    rr: str
    value: str
    priority: int | None = None


def mailbox_cleanup_wanted() -> list[WantedRecord]:
    return [
        WantedRecord("TXT", "_yydsmail-verify", ""),
        WantedRecord("MX", "@", "", 10),
        WantedRecord("MX", "*", "", 10),
    ]


def aliyun_nameservers_ok(nameservers: list[str] | None) -> bool:
    if not nameservers:
        return False
    blob = " ".join(item.lower() for item in nameservers)
    return any(marker in blob for marker in ALIYUN_NS_MARKERS)


def to_aliyun_rr(name: str, domain: str) -> str:
    host = (name or "").strip().rstrip(".").lower()
    apex = (domain or "").strip().rstrip(".").lower()
    if host in {"", "@", apex}:
        return "@"
    if host.startswith("*."):
        rest = host[2:]
        if rest == apex or rest == "":
            return "*"
    suffix = f".{apex}"
    if host.endswith(suffix):
        prefix = host[: -len(suffix)]
        return prefix or "@"
    return host or "@"


def _record_in_zone(name: str, domain: str) -> bool:
    host = (name or "").strip().rstrip(".").lower()
    apex = (domain or "").strip().rstrip(".").lower()
    if not host or host in {"@", "*"}:
        return True
    if host == apex or host == f"*.{apex}":
        return True
    if host.endswith(f".{apex}"):
        return True
    if "." not in host:
        return True
    return False


def _is_relative_owner(name: str) -> bool:
    host = (name or "").strip().rstrip(".").lower()
    return not host or host in {"@", "*"} or "." not in host


def _guide_item_in_zone(item: dict, domain: str | None, *, allow_relative: bool = True) -> bool:
    if not domain:
        return True
    zone = item.get("domain") or item.get("zone") or item.get("apex")
    if zone:
        return domains_match(str(zone), domain)
    if item.get("type") or item.get("recordType"):
        name = str(item.get("name") or item.get("rr") or item.get("host") or "")
        if not allow_relative and _is_relative_owner(name):
            return False
        return _record_in_zone(name, domain)
    return True


def _guide_record_dicts(payload: object, domain: str | None = None) -> list[dict]:
    data = payload
    if isinstance(payload, dict) and payload.get("data") is not None:
        data = payload["data"]
    records: list[dict] = []

    def walk(rows: object, *, allow_relative: bool) -> None:
        if isinstance(rows, dict):
            nested = rows.get("records") or rows.get("requiredRecords") or rows.get("items")
            if isinstance(nested, list):
                walk(nested, allow_relative=allow_relative)
            return
        if not isinstance(rows, list):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            if not _guide_item_in_zone(item, domain, allow_relative=allow_relative):
                continue
            if item.get("type") or item.get("recordType"):
                records.append(item)
            nested_allow = allow_relative
            if item.get("domain") or item.get("zone") or item.get("apex"):
                nested_allow = True
            for key in ("records", "requiredRecords", "dnsRecords"):
                nested = item.get(key)
                if nested is not None:
                    walk(nested, allow_relative=nested_allow)

    if isinstance(data, dict):
        for key in ("records", "requiredRecords", "dnsRecords"):
            walk(data.get(key), allow_relative=True)
        walk(data.get("items"), allow_relative=False)
    elif isinstance(data, list):
        walk(data, allow_relative=False)
    return records


def required_guide_missing(wanted: list[WantedRecord]) -> str | None:
    if not any(item.type == "TXT" for item in wanted):
        return "官方 dns-guide 缺少 TXT"
    if not any(item.type == "MX" and item.rr == "@" for item in wanted):
        return "官方 dns-guide 缺少 MX"
    if not any(item.type == "MX" and item.rr == "*" for item in wanted):
        return "官方 dns-guide 缺少通配 MX"
    return None


def parse_guide_records(payload: object, domain: str) -> list[WantedRecord]:
    wanted: list[WantedRecord] = []
    for item in _guide_record_dicts(payload, domain):
        typ = str(item.get("type") or item.get("recordType") or "").strip().upper()
        if typ not in {"TXT", "MX"}:
            continue
        name = str(item.get("name") or item.get("host") or item.get("rr") or "")
        if not _record_in_zone(name, domain):
            continue
        value = str(item.get("value") or item.get("content") or item.get("record") or "").strip()
        if not value:
            continue
        priority_raw = item.get("priority") or item.get("mxPriority") or item.get("preference")
        try:
            priority = int(priority_raw) if priority_raw is not None and typ == "MX" else None
        except (TypeError, ValueError):
            priority = 10 if typ == "MX" else None
        if typ == "MX" and priority is None:
            priority = 10
        wanted.append(
            WantedRecord(
                type=typ,
                rr=to_aliyun_rr(name, domain),
                value=value,
                priority=priority,
            )
        )
    return wanted


def is_conflict_record(rr: str, typ: str, wanted: list[WantedRecord]) -> bool:
    record_type = (typ or "").upper()
    host = (rr or "@").strip().lower() or "@"
    if record_type in PROTECTED_TYPES:
        return False
    if record_type in {"CNAME", "A", "AAAA"} and host in {"@", "*"}:
        return True
    if record_type == "MX":
        if host in {"@", "*"}:
            return True
        return any(item.type == "MX" and item.rr.lower() == host for item in wanted)
    if record_type == "TXT":
        wanted_txt = {item.rr.lower() for item in wanted if item.type == "TXT"}
        if host in wanted_txt:
            return True
        if host == "_yydsmail-verify" or host.startswith("_yydsmail-verify"):
            return True
    return False
