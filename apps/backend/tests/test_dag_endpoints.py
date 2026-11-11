"""Endpoint tests for ``GET /api/campaigns/{campaign}/dag`` (task 8.2).

Exercises the DAG route end-to-end over the real FastAPI app with a
:class:`~fastapi.testclient.TestClient`. The route is READ-ONLY and keyed by a
curated-case slug; it reads the real mounted ``data/dag/{slug}_dag.json`` through
:class:`CaseService`. No network and no AI client are involved on any path.

Covered (Req. 5.2, 5.4, 6.1, 9.1):

* curated ``shadowray`` → 200 with ``curated=true``, ``can_advance=true``, a
  non-empty Mermaid and 10 nós / 9 edges (the real DAG chain);
* an unknown slug → 404 (the ``campaigns.py`` convention, consistent with every
  other route there).

The curated-but-missing/malformed-DAG 200-with-``erro`` state is hard to trigger
through the real mounted files, so it is covered at the service level in
``test_dag_service_property.py`` (task 7.6) instead.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app

from _stix_seed import ensure_stix_campaigns


_REAL_SLUG = "shadowray"


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module (removed on exit)."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_dag_endpoints_")
    db_path = Path(tmp_dir) / "dag_endpoints.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    # The ``/dag`` endpoint guard now validates the campaign against the
    # persisted STIX campaigns (``_known_campaign_or_404`` over
    # ``stix_campaigns`` — Req. 2.2, 2.3, 4.4), so the curated slug must exist as
    # a ``stix_campaigns`` row to pass the guard.
    ensure_stix_campaigns(factory, [_REAL_SLUG])
    # One-shot curated ingest (sub-step 2.1): the ``/dag`` endpoint now resolves
    # the DAG from the ``dags`` table (sub-step 2.4 removed the request-time disk
    # read), so the curated DAG must be persisted before the endpoint reads it.
    from app.services.curated_ingest_service import CuratedIngestService

    seed_session = factory()
    try:
        CuratedIngestService(seed_session).ingest_all()
    finally:
        seed_session.close()
    try:
        yield factory
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


def _make_client(session_factory: sessionmaker[Session]) -> TestClient:
    """Build a TestClient with the DB dependency overridden onto the temp SQLite."""
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


def test_get_dag_curated_shadowray_returns_full_view(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/campaigns/shadowray/dag`` → 200 curated/can_advance + 10 nós/9 edges."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/dag")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["campaign_id"] == _REAL_SLUG
    assert body["curated"] is True
    assert body["can_advance"] is True
    assert body["error"] is None
    assert body["unavailable"] is False

    # The real ShadowRay DAG is a 10-node chain → 10 nós, 9 edges.
    assert len(body["nodes"]) == 10
    assert len(body["edges"]) == 9

    # Mermaid is non-empty and well-formed (header + one arrow per edge).
    mermaid = body["mermaid"]
    assert mermaid, "mermaid must be non-empty"
    assert mermaid.startswith("flowchart TD")
    assert mermaid.count("-->") == 9


def test_get_dag_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown case slug is a 404 on the DAG endpoint (campaigns.py convention)."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/nao_existe/dag")
    assert resp.status_code == 404, resp.text
