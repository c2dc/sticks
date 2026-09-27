"""SQLAlchemy engine and session wiring.

Supports both PostgreSQL (production default target) and SQLite (local dev),
selected purely by the ``database_url`` setting (env var
``PIPELINE_UI_DATABASE_URL``) — no code changes needed to switch backends.

For SQLite we apply the usual ``connect_args={"check_same_thread": False}`` so
the connection can be shared across threads (FastAPI runs handlers in a
threadpool), which is the standard SQLite-with-FastAPI handling.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings


def _engine_kwargs(database_url: str) -> dict[str, Any]:
    """Return backend-specific keyword args for ``create_engine``.

    SQLite (a file/in-memory DB) needs ``check_same_thread=False`` so the same
    connection can be used across the threadpool FastAPI uses for sync handlers.
    Other backends (PostgreSQL) use their normal defaults.
    """
    if database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {}


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Create a SQLAlchemy Engine from settings.

    The same call works for SQLite and PostgreSQL; the only difference is driven
    by the URL scheme via :func:`_engine_kwargs`.
    """
    settings = settings or get_settings()
    return create_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        **_engine_kwargs(settings.database_url),
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a process-wide cached Engine."""
    return create_db_engine()


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Return a process-wide cached session factory bound to the engine."""
    return sessionmaker(
        bind=get_engine(),
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )


# Convenience module-level handles (mirrors the common SQLAlchemy layout).
engine: Engine = get_engine()
SessionLocal: sessionmaker[Session] = get_sessionmaker()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped database session.

    Usage in a route::

        from fastapi import Depends
        from sqlalchemy.orm import Session
        from app.db import get_db

        @router.get("/casos")
        def listar_casos(db: Session = Depends(get_db)):
            ...

    The session is always closed after the request, even on error.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    """Create all tables directly from metadata (dev/test convenience).

    Prefer Alembic migrations for anything persistent. This helper is handy for
    quick local bootstrapping and for tests that spin up an ephemeral SQLite DB.
    """
    from app.db.base import Base, import_models

    import_models()
    Base.metadata.create_all(bind=get_engine())
