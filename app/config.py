from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "yyds邮箱域名监测"
    panel_password: str = Field(default="", alias="PANEL_PASSWORD")
    panel_cookie_secure: bool = Field(default=False, alias="PANEL_COOKIE_SECURE")
    secret_key: str = Field(default="", alias="SECRET_KEY")
    database_url: str = Field(
        default="postgresql+psycopg://domainslot:domainslot@127.0.0.1:5432/domainslot",
        alias="DATABASE_URL",
    )
    yyds_api_base: str = Field(default="https://maliapi.215.im/v1", alias="YYDS_API_BASE")
    yyds_poll_seconds: float = Field(default=20, alias="YYDS_POLL_SECONDS")
    aliyun_poll_seconds: float = Field(default=60, alias="ALIYUN_POLL_SECONDS")
    verify_attempts: int = Field(default=12, alias="VERIFY_ATTEMPTS")
    verify_retry_seconds: float = Field(default=15, alias="VERIFY_RETRY_SECONDS")
    timezone: str = Field(default="Asia/Shanghai", alias="TZ")
    listen_host: str = Field(default="0.0.0.0", alias="LISTEN_HOST")
    listen_port: int = Field(default=8788, alias="LISTEN_PORT")

    @property
    def version(self) -> str:
        from app import read_version

        return read_version()


@lru_cache
def get_settings() -> Settings:
    return Settings()


ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
