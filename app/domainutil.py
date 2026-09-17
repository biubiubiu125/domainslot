from __future__ import annotations


def normalize_domain(value: str) -> str:
    name = (value or "").strip().rstrip(".").lower()
    if name.startswith("*."):
        name = name[2:]
    if not name:
        raise ValueError("empty domain")
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid domain: {value}") from exc


def domains_match(left: str, right: str) -> bool:
    try:
        return normalize_domain(left) == normalize_domain(right)
    except ValueError:
        return (left or "").strip().rstrip(".").lower() == (right or "").strip().rstrip(".").lower()


def display_domain(ascii_name: str) -> str:
    name = (ascii_name or "").strip().rstrip(".").lower()
    if not name:
        return ascii_name
    try:
        return name.encode("ascii").decode("idna")
    except UnicodeError:
        return ascii_name
