from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Base


@pytest.fixture
def settings() -> Settings:
    return Settings(
        SECRET_KEY="x" * 16,
        PANEL_PASSWORD="panel-pass",
        VERIFY_ATTEMPTS=1,
        VERIFY_RETRY_SECONDS=0,
    )


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
