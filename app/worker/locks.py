from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


FILL_LOCK_KEY = 88220011


def acquire_fill_lock(session: Session) -> None:
    session.execute(text("SELECT pg_advisory_lock(:key)"), {"key": FILL_LOCK_KEY})


def release_fill_lock(session: Session) -> None:
    session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": FILL_LOCK_KEY})
