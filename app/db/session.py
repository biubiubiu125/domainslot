from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base


engine = None
SessionLocal = None


def init_engine() -> None:
    global engine, SessionLocal
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    if engine is None:
        init_engine()
    Base.metadata.create_all(bind=engine)
    ensure_schema()


SCHEMA_STATEMENTS = (
    "ALTER TABLE aliyun_accounts ADD COLUMN IF NOT EXISTS throttle_backoff_seconds INTEGER NOT NULL DEFAULT 120",
    "ALTER TABLE aliyun_accounts ADD COLUMN IF NOT EXISTS first_sync_names TEXT",
    "ALTER TABLE aliyun_accounts ADD COLUMN IF NOT EXISTS first_synced_at TIMESTAMPTZ",
    "ALTER TABLE aliyun_accounts ALTER COLUMN access_key_id TYPE TEXT",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS audit_status VARCHAR(64)",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS last_registrar_checked_at TIMESTAMPTZ",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS client_hold BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE yyds_accounts ADD COLUMN IF NOT EXISTS throttle_until TIMESTAMPTZ",
    "ALTER TABLE yyds_accounts ADD COLUMN IF NOT EXISTS throttle_backoff_seconds INTEGER NOT NULL DEFAULT 120",
    "ALTER TABLE yyds_accounts ADD COLUMN IF NOT EXISTS twofa_code_enc TEXT",
    "ALTER TABLE yyds_accounts ADD COLUMN IF NOT EXISTS first_synced_at TIMESTAMPTZ",
)


def ensure_schema() -> None:
    if engine is None or engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for sql in SCHEMA_STATEMENTS:
            conn.execute(text(sql))


def get_session() -> Session:
    if SessionLocal is None:
        init_engine()
    return SessionLocal()


def db_session() -> Generator[Session, None, None]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
