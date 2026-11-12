"""API integration tests for the AI-run endpoints (task 16.2).

Exercises the two endpoints registered from ``app/api/ai.py`` end-to-end over
the real FastAPI app with a :class:`~fastapi.testclient.TestClient`, following
the existing API-test conventions (``test_flow_endpoints.py``): a fresh temp-file
SQLite engine + ORM schema per module and the ``get_db`` dependency overridden
onto that ephemeral session. No real SEPINF / Postgres / pgvector is involved —
the ``AgentRunService`` graph build and the dependency-availability probe are
monkeypatched so the start path runs against a controlled fake graph.

Covered (Req. 13.1, 13.2):

* **POST start creates a row and invokes the graph (Req. 13.1).** A successful
  ``POST /api/campaigns/{campaign}/ai/run`` returns 201 with the run fields, an
  ``agent_runs`` row exists after the call, and the fake graph's ``invoke`` was
  called under the row's own ``thread_id``.
* **POST failure leaves no partial row and returns an error (Req. 13.2).** When
  the service raises (dependency unavailable / run failed), the handler rolls the
  session back: the response is a 503 error and ZERO ``agent_runs`` rows are
  persisted.
* **GET read exposes iterations and the evaluator verdict (Req. 13.2).** A seeded
  run with two iterations — one carrying an ``agent_evaluations`` verdict —
  returns the run summary plus its iterations (oldest first) each with its
  evaluator verdict; an unknown id / campaign mismatch is a 404.

_Requirements: 13.1, 13.2_
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import (
    AgentEvaluation,
    AgentIteration,
    AgentRun,
)
from app.models.enums import (
    AgentRunStatus,
    AgentStage,
    EvaluatorDecision,
)
from app.services.ai.run_service import (
    AgentRunService,
    DependencyUnavailableError,
    RunFailedError,
)

from _stix_seed import ensure_stix_campaigns

_CAMPAIGN_ID = "campaign-under-test"


def _utcnow() -> dt.datetime:
    """Naive-UTC timestamp matching the AI bookkeeping convention."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Ephemeral session factory (fresh temp-file SQLite + ORM schema per module).
#
# Mirrors test_flow_endpoints.py: the schema is built straight from the ORM so
# the AI bookkeeping tables round-trip without a migration. SQLite does not
# enforce FKs by default, so no Campaign row is required to persist an AgentRun
# under an arbitrary campaign_id.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module (removed on exit)."""
    tmp_dir = tempfile.mkdtemp(prefix="ai_run_endpoints_")
    db_path = Path(tmp_dir) / "ai_run_endpoints.db"
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
    # Seed the persisted STIX campaign the start endpoint's DB-backed guard now
    # validates against (``_known_campaign_or_404`` over ``stix_campaigns`` —
    # Req. 2.2, 2.3, 4.4). ``campaign-under-test`` is a NON-curated campaign
    # (``curated_slug`` left null, so the Campaign_Key resolves to its stix_id),
    # proving the guard accepts any persisted campaign — curated or not — not
    # only the 8 curated slugs.
    ensure_stix_campaigns(factory, [_CAMPAIGN_ID])
    try:
        yield factory
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


@pytest.fixture()
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A per-test session for directly seeding / asserting bookkeeping rows."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


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


# ---------------------------------------------------------------------------
# Fake compiled graph — exposes only ``invoke(state, config=...)``.
# ---------------------------------------------------------------------------


class _FakeGraph:
    """Stand-in compiled graph recording invocations and returning a final state."""

    def __init__(self, final_state: dict[str, Any]) -> None:
        self._final_state = final_state
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, state: Any, config: dict[str, Any] | None = None) -> dict:
        self.invocations.append(config or {})
        return dict(self._final_state)


def _passing_final_state() -> dict[str, Any]:
    """A final state carrying a passing verdict and one completed iteration."""
    return {
        "campaign_id": _CAMPAIGN_ID,
        "iteration": 0,
        "max_iterations": 3,
        "evaluation": {"verdict_success": True},
        "artifacts": {},
        "history": [],
    }


def _patch_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    graph: _FakeGraph | None = None,
    start_raises: BaseException | None = None,
) -> None:
    """Patch the service boundary so no real SEPINF/Postgres/pgvector is needed.

    Either plug a fake compiled graph in (with the dependency probe stubbed to a
    no-op) so the real ``start`` runs its create-row + invoke + persist path, or
    make ``start`` itself raise a controlled error to drive the failure branch.
    """
    monkeypatch.setattr(
        AgentRunService, "_require_dependencies", lambda self: None, raising=True
    )
    if start_raises is not None:
        def _boom(self: AgentRunService, *a: Any, **k: Any) -> Any:
            raise start_raises

        monkeypatch.setattr(AgentRunService, "start", _boom, raising=True)
        return

    monkeypatch.setattr(
        AgentRunService, "_build_graph", lambda self: graph, raising=True
    )


# ===========================================================================
# Req. 13.1 — POST start creates a row and invokes the graph
# ===========================================================================


def test_post_start_creates_row_and_invokes_graph(
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful start returns the run fields, persists a row, invokes the graph."""
    graph = _FakeGraph(_passing_final_state())
    _patch_service(monkeypatch, graph=graph)

    client = _make_client(session_factory)
    resp = client.post(f"/api/campaigns/{_CAMPAIGN_ID}/ai/run", json={})
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["campaign_id"] == _CAMPAIGN_ID
    assert body["status"] == AgentRunStatus.COMPLETED.value
    assert body["thread_id"], "a non-empty thread_id must be returned"
    assert body["iterations_used"] == 1
    assert body["final_verdict_success"] is True

    # The row was committed by the handler (visible to a fresh session).
    run = db_session.get(AgentRun, body["id"])
    assert run is not None
    assert run.campaign_id == _CAMPAIGN_ID
    assert run.status is AgentRunStatus.COMPLETED

    # The graph was invoked under the row's own checkpoint thread (Req. 13.1).
    assert len(graph.invocations) == 1
    assert graph.invocations[0]["configurable"]["thread_id"] == body["thread_id"]


def test_post_start_honours_max_iterations_body(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional ``max_iterations`` body is carried onto the created run."""
    graph = _FakeGraph(_passing_final_state())
    _patch_service(monkeypatch, graph=graph)

    client = _make_client(session_factory)
    resp = client.post(
        f"/api/campaigns/{_CAMPAIGN_ID}/ai/run", json={"max_iterations": 5}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["max_iterations"] == 5


# ===========================================================================
# Req. 13.2 — POST failure leaves no partial row and returns an error
# ===========================================================================


def test_post_start_dependency_unavailable_persists_no_row(
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dependency-unavailable failure returns 503 and persists NO row (Req. 13.2)."""
    before = db_session.execute(
        select(func.count()).select_from(AgentRun)
    ).scalar_one()

    _patch_service(
        monkeypatch,
        start_raises=DependencyUnavailableError("checkpointer"),
    )

    client = _make_client(session_factory)
    resp = client.post(f"/api/campaigns/{_CAMPAIGN_ID}/ai/run", json={})

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "dependency_unavailable"
    assert detail["dependency"] == "checkpointer"

    # No partial agent_runs row survived the rolled-back failed attempt.
    db_session.expire_all()
    after = db_session.execute(
        select(func.count()).select_from(AgentRun)
    ).scalar_one()
    assert after == before, "a failed start must leave no partial agent_runs row"


def test_post_start_run_failed_persists_no_row(
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run-failed error returns 503 naming the node and persists NO row (Req. 13.2)."""
    before = db_session.execute(
        select(func.count()).select_from(AgentRun)
    ).scalar_one()

    _patch_service(monkeypatch, start_raises=RunFailedError("flow"))

    client = _make_client(session_factory)
    resp = client.post(f"/api/campaigns/{_CAMPAIGN_ID}/ai/run", json={})

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "run_failed"
    assert detail["node"] == "flow"

    db_session.expire_all()
    after = db_session.execute(
        select(func.count()).select_from(AgentRun)
    ).scalar_one()
    assert after == before, "a failed start must leave no partial agent_runs row"


# ===========================================================================
# Req. 13.2 — GET read exposes iterations and the evaluator verdict
# ===========================================================================


def _seed_run_with_iterations(session: Session) -> AgentRun:
    """Seed a completed run with two iterations; the second carries a verdict."""
    now = _utcnow()
    run = AgentRun(
        campaign_id=_CAMPAIGN_ID,
        status=AgentRunStatus.COMPLETED,
        max_iterations=3,
        iterations_used=2,
        thread_id="thread-seeded-read",
        final_verdict_success=True,
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    session.flush()

    # Iteration 0 — first pass, no refined stage, no evaluation.
    it0 = AgentIteration(
        run_id=run.id,
        index=0,
        refined_stage=None,
        notes="first pass",
        created_at=now,
    )
    # Iteration 1 — refined into Stage 4, carries the evaluator verdict.
    it1 = AgentIteration(
        run_id=run.id,
        index=1,
        refined_stage=AgentStage.OPERATIONALIZATION,
        notes="refined pass",
        created_at=now + dt.timedelta(seconds=1),
    )
    session.add_all([it0, it1])
    session.flush()

    evaluation = AgentEvaluation(
        iteration_id=it1.id,
        report_id=None,
        success_rate=0.9,
        procedural_coverage=0.75,
        verdict_success=True,
        failed_nodes=None,
        decision=EvaluatorDecision.FINISH,
        rationale="metrics met the passing threshold",
        created_at=now + dt.timedelta(seconds=2),
    )
    session.add(evaluation)
    session.flush()
    session.commit()
    return run


def test_get_run_exposes_iterations_and_verdict(
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    """GET returns the run + its iterations (oldest first) with the verdict (Req. 13.2)."""
    run = _seed_run_with_iterations(db_session)

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_CAMPAIGN_ID}/ai/runs/{run.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Run summary.
    assert body["run"]["id"] == run.id
    assert body["run"]["campaign_id"] == _CAMPAIGN_ID
    assert body["run"]["final_verdict_success"] is True

    # Iterations oldest first.
    iterations = body["iterations"]
    assert len(iterations) == 2
    assert [it["index"] for it in iterations] == [0, 1]

    # First pass has no evaluation; the refined pass carries the verdict.
    assert iterations[0]["evaluation"] is None
    assert iterations[1]["refined_stage"] == (
        AgentStage.OPERATIONALIZATION.value
    )

    verdict = iterations[1]["evaluation"]
    assert verdict is not None
    assert verdict["verdict_success"] is True
    assert verdict["decision"] == EvaluatorDecision.FINISH.value
    assert verdict["success_rate"] == 0.9
    assert verdict["procedural_coverage"] == 0.75
    assert verdict["rationale"] == "metrics met the passing threshold"


def test_get_run_unknown_id_is_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown run id is a 404 (Req. 13.2)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_CAMPAIGN_ID}/ai/runs/999999")
    assert resp.status_code == 404, resp.text


def test_get_run_campaign_mismatch_is_404(
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    """A run id that belongs to a different campaign is a 404 (Req. 13.2)."""
    run = _seed_run_with_iterations(db_session)

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/outra-campanha/ai/runs/{run.id}")
    assert resp.status_code == 404, resp.text
