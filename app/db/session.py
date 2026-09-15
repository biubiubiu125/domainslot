from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
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
