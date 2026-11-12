"""Integration tests of the REST surface with mocked services (task 11.4).

Covers the REST surface that remains after the curated pipeline removal, end-to-end
at the HTTP layer with the external world mocked:

* the campaigns surface - ``GET /api/campaigns`` (the 8 curated cases),
  ``GET /api/campaigns/{campaign}`` and ``GET /api/campaigns/{campaign}/operation``;
* the session / preferences / audit surface - ``GET /api/session-state``,
  ``PUT /api/preferences`` and ``GET /api/audit/{operation}``;
* the removed ``/stage/N`` surface answers 404.

The database is an ephemeral temp-file SQLite created per test module and wired
into the app via the ``get_db`` dependency override.

_Requirements: 1.3, 6.8_
"""

from __future__ import annotations

import datetime as dt
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
from app.models.domain import AuditLogEntry, Operation
from app.models.enums import OperationState
from tests._curated_db import CURATED_NAMES as CURATED_CASES, seed_case_rows

from _stix_seed import ensure_stix_campaigns
def _seed_curated_stix_campaigns(session_factory: sessionmaker[Session]) -> None:
    """Seed the persisted STIX campaigns the DB-backed guard validates against.

    Ensures every curated slug has a matching ``stix_campaigns`` row so the
    Campaign_Key resolves to the slug and the guard (``_known_campaign_or_404``)
    accepts it, keeping the curated flow green without regression (Req. 9.3).
    Idempotent and self-healing (re-run on every client build): one test in this
    module re-persists an empty bundle for the curated version, wiping its
    ``stix_campaigns`` children, so the re-seed restores them regardless of test
    ordering. The additional non-curated keys exercised across the suite
    (``costaricto``) are seeded too.
    """
    ensure_stix_campaigns(session_factory, CURATED_CASES.keys())


# ---------------------------------------------------------------------------
# Mocked Caldera transports (no real network) — same contract as the sibling
# suite's healthy chain, kept local so this file is self-contained.
# ---------------------------------------------------------------------------





@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module.

    Kept module-scoped so the whole REST surface exercises the same ephemeral
    DB; the audit-trail test seeds its own rows via the same factory. The temp
    file is removed on teardown (Windows-friendly cleanup).
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_api_endpoints_")
    db_path = Path(tmp_dir) / "api_endpoints.db"
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
    # Seed the persisted STIX campaigns the DB-backed guard now validates against
    # (``_known_campaign_or_404`` over ``stix_campaigns`` — Req. 2.2, 2.3, 4.4).
    # The campaigns surface is the single source of truth, so the curated slugs
    # exercised across this suite must exist as ``stix_campaigns`` rows (with
    # ``curated_slug`` set so the Campaign_Key resolves to the slug) for the guard
    # to accept them exactly as the curated flow did before the migration.
    _seed_curated_stix_campaigns(factory)
    # The campaigns/flow endpoints resolve the attack flow + Abilities/Adversary
    # from the database, so the 8 curated campaigns are persisted as ORM rows.
    seed_session = factory()
    try:
        for slug in CURATED_CASES:
            seed_case_rows(
                seed_session,
                slug,
                abilities=[
                    {
                        "ability_id": f"{slug}-ab-1",
                        "name": "T1190 - first",
                        "technique_id": "T1190",
                        "tactic": "initial-access",
                        "executors": [
                            {"name": "sh", "platform": "linux", "command": "echo 1"}
                        ],
                    },
                    {
                        "ability_id": f"{slug}-ab-2",
                        "name": "T1059 - second",
                        "technique_id": "T1059",
                        "tactic": "execution",
                        "executors": [
                            {"name": "sh", "platform": "linux", "command": "echo 2"}
                        ],
                    },
                ],
                adversary={
                    "id": f"{slug}-adv",
                    "name": f"{slug} adversary",
                    "atomic_ordering": [f"{slug}-ab-1", f"{slug}-ab-2"],
                },
                flow={
                    "campaign_name": CURATED_CASES[slug],
                    "structural_nodes": [
                        {"node_id": "n0", "technique_id": "T1190", "child_nodes": ["n1"]},
                        {"node_id": "n1", "technique_id": "T1059", "parent_nodes": ["n0"]},
                    ],
                    "edges": [{"source": "n0", "target": "n1"}],
                },
            )
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
    """Build a TestClient with the DB dependency overridden by the ephemeral DB."""
    # Re-heal the persisted curated campaigns the DB-backed guard validates
    # against (the module-scoped DB is shared across tests).
    _seed_curated_stix_campaigns(session_factory)
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


# A curated slug seeded in the database and used across the surface tests.
_REAL_SLUG = "shadowray"


# ===========================================================================
# GET /api/campaigns - the 8 curated cases
# ===========================================================================


def test_list_campaigns_lists_eight_cases(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/campaigns`` lists the 8 curated cases by their slugs."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    slugs = {c["id"] for c in body["campaigns"]}
    assert slugs == set(CURATED_CASES), slugs
    assert len(body["campaigns"]) == 8


def test_removed_stage_routes_return_404(
    session_factory: sessionmaker[Session],
) -> None:
    """The legacy ``/stage/N`` surface no longer exists (Req. 1.3)."""
    client = _make_client(session_factory)
    for stage in range(1, 7):
        resp = client.get(f"/api/campaigns/{_REAL_SLUG}/stage/{stage}")
        assert resp.status_code == 404, (stage, resp.text)


# ===========================================================================
# GET /api/campaigns/{campaign} — case detail
# ===========================================================================


def test_detail_campaign_returns_abilities_adversary_and_origin(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/campaigns/{campaign}`` returns the curated Abilities, Adversary,
    ordering and the explicit translation source (Req. 3.2, 3.3, 3.5, 11.1)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["id"] == _REAL_SLUG
    assert body["translation_source"] == "human_curation"
    assert isinstance(body["abilities"], list) and body["abilities"]
    # Every ability carries its stable id.
    for ability in body["abilities"]:
        assert ability["ability_id"]
    # The ordering mirrors the adversary's atomic_ordering when present.
    if body["adversary"] is not None:
        assert body["atomic_ordering"] == body["adversary"]["atomic_ordering"]


def test_detail_campaign_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown case slug is a 404 (not one of the 8 curated cases)."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/nao_existe")
    assert resp.status_code == 404, resp.text




# ===========================================================================
# GET /api/campaigns/{campaign}/operation
# ===========================================================================


def test_get_operation_empty_before_any_emulation(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../operation`` returns an empty operations list before any run."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/costaricto/operation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == "costaricto"
    assert body["operations"] == []


def test_get_operation_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug is a 404 on the operation endpoint."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/nao_existe/operation")
    assert resp.status_code == 404, resp.text


# ===========================================================================
# GET /api/state-sessao
# ===========================================================================


def test_get_session_state_default_when_none_persisted(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/state-sessao`` returns the default all-not-started state when
    no session state is persisted (Req. 9.5)."""
    client = _make_client(session_factory)
    resp = client.get("/api/session-state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exists"] is False
    assert body["restore_failed"] is False
    assert body["current_campaign"] is None
    assert isinstance(body["completed_stages_by_campaign"], dict)


# ===========================================================================
# PUT /api/preferences
# ===========================================================================


def test_put_preferences_persists_theme_and_language(
    session_factory: sessionmaker[Session],
) -> None:
    """``PUT /api/preferences`` persists the theme + language and echoes the
    stored values back (Req. 7.4, 8.4)."""
    client = _make_client(session_factory)
    resp = client.put(
        "/api/preferences", json={"theme": "dark", "language": "en"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"theme": "dark", "language": "en"}

    # A partial update keeps the omitted field at its previously persisted value.
    resp2 = client.put("/api/preferences", json={"theme": "light"})
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["theme"] == "light"
    assert body2["language"] == "en"


def test_put_preferences_rejects_invalid_theme(
    session_factory: sessionmaker[Session],
) -> None:
    """An out-of-enum theme value is a 422 (schema validation)."""
    client = _make_client(session_factory)
    resp = client.put("/api/preferences", json={"theme": "neon"})
    assert resp.status_code == 422, resp.text


# ===========================================================================
# GET /api/audit/{operation}
# ===========================================================================


def test_get_audit_returns_persisted_trail_oldest_first(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/audit/{operation}`` returns the persisted audit trail of an
    Operation — one row per command, oldest first (Req. 6.8)."""
    # Seed an Operation with two audit rows directly via the shared factory.
    db = session_factory()
    try:
        op = Operation(
            campaign_id="c0010",
            state=OperationState.FINISHED,
            total_success=1,
            total_failure=1,
        )
        db.add(op)
        db.flush()
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        db.add_all(
            [
                AuditLogEntry(
                    operation_id=op.id,
                    ability_id="a-1",
                    command="curl http://192.168.20.30/a",
                    target_container="target-a",
                    result="success",
                    recorded_at=now,
                ),
                AuditLogEntry(
                    operation_id=op.id,
                    ability_id="a-2",
                    command="curl http://192.168.20.31/b",
                    target_container="target-b",
                    result="failure",
                    recorded_at=now + dt.timedelta(seconds=1),
                ),
            ]
        )
        db.commit()
        op_id = op.id
    finally:
        db.close()

    client = _make_client(session_factory)
    resp = client.get(f"/api/audit/{op_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation_id"] == op_id
    assert len(body["records"]) == 2
    # Oldest first: ids strictly ascending, one row per executed command.
    ids = [r["id"] for r in body["records"]]
    assert ids == sorted(ids)
    assert body["records"][0]["command"] == "curl http://192.168.20.30/a"
    assert body["records"][0]["target_container"] == "target-a"


def test_get_audit_unknown_operation_returns_empty_trail(
    session_factory: sessionmaker[Session],
) -> None:
    """An operation with no audit rows returns an empty (but 200) trail."""
    client = _make_client(session_factory)
    resp = client.get("/api/audit/999999")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation_id"] == 999999
    assert body["records"] == []


