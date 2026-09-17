from __future__ import annotations

import json

from app.aliyun.client import AliyunClient
from app.config import Settings
from app.crypto import decrypt_json, decrypt_text, encrypt_json, encrypt_text, reveal_secret
from app.db.models import AliyunAccount, YydsAccount
from app.yyds.client import YydsClient


def _optional_decrypt_json(secret_key: str, value: str, default):
    try:
        loaded = decrypt_json(secret_key, value, default=default)
    except (ValueError, json.JSONDecodeError):
        return default
    return loaded if loaded is not None else default


def _optional_decrypt_text(secret_key: str, value: str) -> str:
    try:
        return decrypt_text(secret_key, value)
    except ValueError:
        return ""


def aliyun_client(settings: Settings, account: AliyunAccount) -> AliyunClient:
    access_key_id = reveal_secret(settings.secret_key, account.access_key_id)
    secret = decrypt_text(settings.secret_key, account.access_key_secret_enc)
    return AliyunClient(access_key_id, secret)


def yyds_client(settings: Settings, account: YydsAccount) -> YydsClient:
    password = decrypt_text(settings.secret_key, account.password_enc)
    cookies = _optional_decrypt_json(settings.secret_key, account.cookies_enc or "", []) or []
    token = _optional_decrypt_text(settings.secret_key, account.access_token_enc or "")
    twofa = _optional_decrypt_text(settings.secret_key, getattr(account, "twofa_code_enc", None) or "")
    client = YydsClient(
        api_base=settings.yyds_api_base,
        username=account.username,
        password=password,
        cookies=cookies if isinstance(cookies, list) else [],
        access_token=token or None,
        session_key=str(account.id),
        twofa_code=twofa or None,
    )

    def _persist() -> None:
        persist_yyds_session(settings, account, client)

    client.on_session_change = _persist
    return client


def persist_yyds_session(settings: Settings, account: YydsAccount, client) -> None:
    account.cookies_enc = encrypt_json(settings.secret_key, client.export_cookies())
    if client.access_token:
        account.access_token_enc = encrypt_text(settings.secret_key, client.access_token)
    if getattr(client, "twofa_consumed", False) and getattr(account, "twofa_code_enc", None):
        account.twofa_code_enc = None
