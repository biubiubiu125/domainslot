from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session


FILL_LOCK_KEY = 88220011


def yyds_session_lock_key(account_id: UUID | str) -> int:
    digest = hashlib.sha256(f"domainslot-yyds-session:{account_id}".encode("utf-8")).digest()
    key = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
    if key in {0, FILL_LOCK_KEY}:
        key = FILL_LOCK_KEY + 1
    return key


def acquire_fill_lock(session: Session) -> Connection | None:
    return _acquire_advisory_lock(session, FILL_LOCK_KEY)


def release_fill_lock(lock_conn: Connection | None) -> None:
    _release_advisory_lock(lock_conn, FILL_LOCK_KEY)


def acquire_yyds_session_lock(session: Session, account_id: UUID | str) -> Connection | None:
    return _acquire_advisory_lock(session, yyds_session_lock_key(account_id))


def release_yyds_session_lock(lock_conn: Connection | None) -> None:
    if lock_conn is None:
        return
    key = lock_conn.info.get("domainslot_advisory_key", FILL_LOCK_KEY + 1)
    _release_advisory_lock(lock_conn, key)


def _acquire_advisory_lock(session: Session, key: int) -> Connection | None:
    bind = session.get_bind()
    dialect = getattr(bind, "dialect", None)
    if dialect is None or dialect.name != "postgresql":
        return None
    engine = bind if isinstance(bind, Engine) else getattr(bind, "engine", None)
    conn = engine.connect() if engine is not None else bind.connect()
    try:
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        conn.commit()
        conn.info["domainslot_advisory_key"] = key
    except Exception:
        conn.close()
        raise
    return conn


def _release_advisory_lock(lock_conn: Connection | None, key: int) -> None:
    if lock_conn is None:
        return
    try:
        lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        lock_conn.commit()
    finally:
        lock_conn.close()
