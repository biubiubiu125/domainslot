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
    except InvalidToken as exc:
        raise ValueError("secret decrypt failed") from exc


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
