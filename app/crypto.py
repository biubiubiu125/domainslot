from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_text(secret_key: str, value: str) -> str:
    if value is None:
        return ""
    return _fernet(secret_key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(secret_key: str, value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet(secret_key).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ValueError("secret decrypt failed") from exc


def _looks_like_fernet(value: str) -> bool:
    return value.startswith("gAAAA") and len(value) > 80


def reveal_secret(secret_key: str, stored: str | None) -> str:
    if not stored:
        return ""
    try:
        return decrypt_text(secret_key, stored)
    except ValueError:
        if _looks_like_fernet(stored):
            raise
        return stored


def secrets_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    left_bytes = left.encode("utf-8")
    right_bytes = right.encode("utf-8")
    if len(left_bytes) != len(right_bytes):
        hmac.compare_digest(left_bytes, left_bytes)
        return False
    return hmac.compare_digest(left_bytes, right_bytes)


def encrypt_json(secret_key: str, value: Any) -> str:
    return encrypt_text(secret_key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def decrypt_json(secret_key: str, value: str, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(decrypt_text(secret_key, value))


def hash_password(password: str, salt: bytes | None = None) -> str:
    raw_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), raw_salt, 120000)
    return f"pbkdf2${raw_salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
