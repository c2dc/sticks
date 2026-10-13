"""Startup smoke test for task 1.5.

Verifies two independent startup concerns for the Pipeline_UI backend:

1. The FastAPI app boots via the ``create_app`` factory and the ``/health``
   endpoint responds (exercised with FastAPI's ``TestClient`` / httpx).
2. The SQLAlchemy layer initializes against SQLite: an ephemeral SQLite engine
   is created from a ``Settings`` override, ``create_all`` builds the schema,
   a trivial ``SELECT 1`` runs, and a real ``get_db``-style session works.

Both DB variants (in-memory and temp file) are used and cleaned up so no state
leaks and no ``.db`` file is left behind.

_Requisitos: 10.1_
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.main import create_app


def test_app_boots_and_health_check_responds() -> None:
    """The FastAPI app comes up via the factory and /health answers OK."""
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sqlite_engine_initializes_and_select_one_works() -> None:
    """An in-memory SQLite engine initializes and a trivial SELECT 1 runs."""
    settings = Settings(database_url="sqlite:///:memory:")
    engine = create_db_engine(settings)
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        engine.dispose()


def test_sqlite_create_all_and_session_roundtrip_on_temp_file() -> None:
    """create_all() builds the schema on a temp-file SQLite DB and a
    get_db-style session opens, queries and closes cleanly.

    A temp file (not :memory:) is used so create_all runs against a real file
    engine, exercising the same path the dev SQLite default would take. The
    file is removed afterward so no .db state leaks.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_smoke_")
    db_path = Path(tmp_dir) / "smoke.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )

    try:
        # Populate metadata from the models package, then build the schema.
        import_models()
        Base.metadata.create_all(bind=engine)

        # A get_db-style scoped session works end to end.
        db = session_factory()
        try:
            assert db.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            db.close()

        # The engine actually wrote a file (SQLite file backend initialized).
        assert db_path.exists()
    finally:
        engine.dispose()
        # Clean up so no .db file leaks.
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(tmp_dir)

    assert not db_path.exists()
