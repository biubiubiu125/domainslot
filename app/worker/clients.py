from __future__ import annotations

from app.aliyun.client import AliyunClient
from app.config import Settings
from app.crypto import decrypt_json, decrypt_text
from app.db.models import AliyunAccount, YydsAccount
from app.yyds.client import YydsClient


def aliyun_client(settings: Settings, account: AliyunAccount) -> AliyunClient:
    secret = decrypt_text(settings.secret_key, account.access_key_secret_enc)
    return AliyunClient(account.access_key_id, secret)


def yyds_client(settings: Settings, account: YydsAccount) -> YydsClient:
    password = decrypt_text(settings.secret_key, account.password_enc)
    cookies = decrypt_json(settings.secret_key, account.cookies_enc or "", default=[]) or []
    token = decrypt_text(settings.secret_key, account.access_token_enc or "")
    return YydsClient(
        api_base=settings.yyds_api_base,
        username=account.username,
        password=password,
        cookies=cookies if isinstance(cookies, list) else [],
        access_token=token or None,
    )
