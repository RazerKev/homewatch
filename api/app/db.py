"""Database engine and per-request sessions."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


@lru_cache
def get_engine() -> Engine:
    # Created lazily so tests (and the Phase 3 Lambda) control when it connects.
    return create_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,  # survive database restarts without a 500 on the next request
        pool_size=5,
        max_overflow=5,
        # Never copy row values into exception messages or SQL logs: an unexpected
        # database error must not dump someone's sensor readings into the logs.
        hide_parameters=True,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with get_sessionmaker()() as session:
        yield session
