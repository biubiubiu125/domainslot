from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.worker.locks import (
    FILL_LOCK_KEY,
    acquire_fill_lock,
    acquire_yyds_session_lock,
    release_fill_lock,
    release_yyds_session_lock,
    yyds_session_lock_key,
)


PG_URL = os.environ.get("DOMAINSLOT_PG_TEST_URL", "")


def test_sqlite_advisory_lock_is_noop(db_session):
    lock = acquire_fill_lock(db_session)
    assert lock is None
    release_fill_lock(lock)


@pytest.mark.skipif(not PG_URL, reason="no DOMAINSLOT_PG_TEST_URL")
def test_pg_lock_survives_session_commit_but_unlocks_own_backend():
    engine = create_engine(PG_URL, pool_pre_ping=True, future=True, pool_size=4)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    other = factory()
    try:
        lock = acquire_fill_lock(session)
        assert lock is not None
        session.commit()
        stolen = other.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": FILL_LOCK_KEY}).scalar()
        assert stolen is False
        unlocked_other = other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": FILL_LOCK_KEY}).scalar()
        assert unlocked_other is False
        release_fill_lock(lock)
        taken = other.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": FILL_LOCK_KEY}).scalar()
        assert taken is True
        other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": FILL_LOCK_KEY})
        other.commit()
    finally:
        session.close()
        other.close()
        engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="no DOMAINSLOT_PG_TEST_URL")
def test_pg_yyds_session_lock_is_per_account_and_not_fill_key():
    engine = create_engine(PG_URL, pool_pre_ping=True, future=True, pool_size=4)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    other = factory()
    key_a = yyds_session_lock_key("acct-a")
    key_b = yyds_session_lock_key("acct-b")
    try:
        lock_a = acquire_yyds_session_lock(session, "acct-a")
        assert lock_a is not None
        session.commit()
        stolen = other.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key_a}).scalar()
        assert stolen is False
        fill_free = other.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": FILL_LOCK_KEY}).scalar()
        assert fill_free is True
        other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": FILL_LOCK_KEY})
        other.commit()
        lock_b = acquire_yyds_session_lock(other, "acct-b")
        assert lock_b is not None
        release_yyds_session_lock(lock_b)
        release_yyds_session_lock(lock_a)
        taken = other.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key_a}).scalar()
        assert taken is True
        other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key_a})
        other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key_b})
        other.commit()
    finally:
        session.close()
        other.close()
        engine.dispose()


def test_schema_statements_add_first_synced_at():
    from app.db.session import SCHEMA_STATEMENTS

    joined = "\n".join(SCHEMA_STATEMENTS)
    assert "ALTER TABLE aliyun_accounts ADD COLUMN IF NOT EXISTS first_synced_at" in joined
    assert "ALTER TABLE yyds_accounts ADD COLUMN IF NOT EXISTS first_synced_at" in joined


@pytest.mark.skipif(not PG_URL, reason="no DOMAINSLOT_PG_TEST_URL")
def test_pg_schema_adds_first_synced_at_to_existing_tables():
    from datetime import datetime, timezone

    from app.db.models import AliyunAccount, YydsAccount
    from app.db.session import SCHEMA_STATEMENTS

    engine = create_engine(PG_URL, pool_pre_ping=True, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    suffix = os.urandom(4).hex()
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE aliyun_accounts DROP COLUMN IF EXISTS first_synced_at"))
            conn.execute(text("ALTER TABLE yyds_accounts DROP COLUMN IF EXISTS first_synced_at"))
        with engine.begin() as conn:
            for sql in SCHEMA_STATEMENTS:
                conn.execute(text(sql))
        now = datetime.now(timezone.utc)
        aliyun = AliyunAccount(
            name=f"ak-{suffix}",
            access_key_id=f"LTAI{suffix}",
            access_key_secret_enc="enc",
            first_synced_at=now,
        )
        yyds = YydsAccount(
            name=f"y-{suffix}",
            username=f"user-{suffix}",
            password_enc="enc",
            first_synced_at=now,
        )
        session.add_all([aliyun, yyds])
        session.commit()
        session.refresh(aliyun)
        session.refresh(yyds)
        assert aliyun.first_synced_at is not None
        assert yyds.first_synced_at is not None
        session.delete(aliyun)
        session.delete(yyds)
        session.commit()
    finally:
        with engine.begin() as conn:
            for sql in SCHEMA_STATEMENTS:
                conn.execute(text(sql))
        session.close()
        engine.dispose()
