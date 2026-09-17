from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class AdminAuth(Base):
    __tablename__ = "admin_auth"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    password_hash: Mapped[str] = mapped_column(String(255))
    session_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AliyunAccount(Base):
    __tablename__ = "aliyun_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100))
    access_key_id: Mapped[str] = mapped_column(Text)
    access_key_secret_enc: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    first_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_sync_names: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    throttle_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    throttle_backoff_seconds: Mapped[int] = mapped_column(Integer, default=120)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    domains: Mapped[list["Domain"]] = relationship(back_populates="aliyun_account")


class YydsAccount(Base):
    __tablename__ = "yyds_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100))
    username: Mapped[str] = mapped_column(String(255))
    password_enc: Mapped[str] = mapped_column(Text)
    twofa_code_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    cookies_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    receive_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    login_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    max_wildcard: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_wildcard: Mapped[int] = mapped_column(Integer, default=0)
    max_domains: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_domains: Mapped[int] = mapped_column(Integer, default=0)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    throttle_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    throttle_backoff_seconds: Mapped[int] = mapped_column(Integer, default=120)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    domains: Mapped[list["Domain"]] = relationship(back_populates="yyds_account")
    snapshot: Mapped["YydsDomainSnapshot | None"] = relationship(
        back_populates="account",
        uselist=False,
        cascade="all, delete-orphan",
    )


class Domain(Base):
    __tablename__ = "domains"
    __table_args__ = (UniqueConstraint("name", name="uq_domains_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    aliyun_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("aliyun_accounts.id", ondelete="SET NULL"), nullable=True
    )
    yyds_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("yyds_accounts.id", ondelete="SET NULL"), nullable=True
    )
    yyds_domain_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="used")
    from_first_snapshot: Mapped[bool] = mapped_column(Boolean, default=False)
    registration_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filling_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audit_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nameservers: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_on_aliyun_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_on_yyds_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_registrar_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    client_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    aliyun_account: Mapped[AliyunAccount | None] = relationship(back_populates="domains")
    yyds_account: Mapped[YydsAccount | None] = relationship(back_populates="domains")


class YydsDomainSnapshot(Base):
    __tablename__ = "yyds_domain_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    yyds_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("yyds_accounts.id", ondelete="CASCADE"), unique=True
    )
    names_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    account: Mapped[YydsAccount] = relationship(back_populates="snapshot")


class EventLog(Base):
    __tablename__ = "event_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(String(16), default="info")
    code: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text)
    domain_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    aliyun_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    yyds_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
