from __future__ import annotations

from dataclasses import dataclass


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


def parse_guide_records(payload: object, domain: str) -> list[WantedRecord]:
    data = payload
    if isinstance(payload, dict) and "data" in payload and payload.get("data") is not None:
        data = payload["data"]
    records: list[object] = []
    if isinstance(data, dict):
        raw = data.get("records") or data.get("requiredRecords") or data.get("items") or []
        nested = data.get("dnsRecords")
        if isinstance(nested, dict):
            raw = nested.get("records") or raw
        elif isinstance(nested, list) and not raw:
            raw = nested
        records = raw if isinstance(raw, list) else []
    elif isinstance(data, list):
        records = data

    wanted: list[WantedRecord] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or item.get("recordType") or "").strip().upper()
        if typ not in {"TXT", "MX"}:
            continue
        name = str(item.get("name") or item.get("host") or item.get("rr") or "")
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
    if record_type == "CNAME" and host in {"@", "www", "*"}:
        return True
    if record_type == "MX":
        return True
    if record_type == "TXT":
        wanted_txt = {item.rr.lower() for item in wanted if item.type == "TXT"}
        if host in wanted_txt:
            return True
        if host == "_yydsmail-verify" or host.startswith("_yydsmail-verify"):
            return True
    return False
