"""Example tests for the extended AI-run read endpoint and ``_fail_run`` (task 3.10).

Two concerns, exercised over the ephemeral-SQLite + fake-graph harness the
sibling AI tests use (``test_ai_run_endpoints.py`` / ``test_run_service_lifecycle.py``):

1. **Extended read endpoint (Req. 1.6, 2.4).** ``GET
   /api/campaigns/{campaign}/ai/runs/{id}`` returns, per iteration, the
   ``agent_stage_outputs`` rows ordered by stage — each stamped
   ``origin=ai_generated`` with its ``payload`` and its 1:1 Audit_Trace
   (``agent_input`` + ``processing``). Any ``None``/absent Agent_Processing
   element resolves to the explicit ``"not_available"`` sentinel, and
   ``run.error`` is null on a successful run.

2. **``_fail_run`` failure path (Req. 2.6, 4.4).** A graph invocation that raises
   a node error carrying a partial trace flips the run to ``FAILED``, writes the
   failing stage + absent-input cause into ``agent_runs.error``, and persists the
   partial Audit_Trace (a minimal anchor ``agent_stage_outputs`` row + its 1:1
   ``agent_stage_traces`` row for the failing stage) inside a SAVEPOINT. The read
   endpoint then surfaces ``error`` and the iteration's ``failing_agent``.

No real SEPINF / Postgres / pgvector is involved: the service graph build and
the dependency-availability probe are monkeypatched, and persistence lands in a
fresh temp-file SQLite built straight from the ORM.

_Requirements: 1.6, 2.4, 2.6_
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

from app.api.ai import NOT_AVAILABLE
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import (
    AgentIteration,
    AgentRun,
    AgentStageOutput,
    AgentStageTrace,
)
from app.models.enums import AgentRunStatus, AgentStage, TranslationSource
from app.services.ai.run_service import AgentRunService, RunFailedError

_CAMPAIGN_ID = "campaign-under-test"


def _utcnow() -> dt.datetime:
    """Naive-UTC timestamp matching the AI bookkeeping convention."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Ephemeral session factory (fresh temp-file SQLite + ORM schema per module).
# Mirrors test_ai_run_endpoints.py so the AI bookkeeping tables round-trip
# without a migration and no Campaign row is required under SQLite.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module (removed on exit)."""
    tmp_dir = tempfile.mkdtemp(prefix="ai_run_trace_read_")
    db_path = Path(tmp_dir) / "ai_run_trace_read.db"
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


# ===========================================================================
# 1. Extended read endpoint — stage outputs + trace + sentinel + null error
#    (Req. 1.6, 2.4)
# ===========================================================================


def _seed_completed_run_with_trace(session: Session) -> AgentRun:
    """Seed a COMPLETED run, one iteration, two stage outputs each with a trace.

    * The attack flow stage output carries a FULLY populated processing trace.
    * The operationalization stage output carries a trace with SOME processing
      elements left ``None`` (and one missing entirely) so the read layer must
      resolve them to the ``"not_available"`` sentinel.
    """
    now = _utcnow()
    run = AgentRun(
        campaign_id=_CAMPAIGN_ID,
        status=AgentRunStatus.COMPLETED,
        max_iterations=3,
        iterations_used=1,
        thread_id="thread-trace-read",
        final_verdict_success=True,
        error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    session.flush()

    iteration = AgentIteration(
        run_id=run.id,
        index=0,
        refined_stage=None,
        notes="first pass",
        created_at=now,
    )
    session.add(iteration)
    session.flush()

    # Attack flow stage output — fully populated processing.
    flow_output = AgentStageOutput(
        iteration_id=iteration.id,
        stage=AgentStage.FLOW,
        origin=TranslationSource.AI_GENERATED,
        payload={"nodes": ["T1059"], "edges": []},
        created_at=now,
    )
    session.add(flow_output)
    session.flush()
    session.add(
        AgentStageTrace(
            stage_output_id=flow_output.id,
            agent_input={"attack_context": "T1059", "prior_artifacts": {}},
            processing={
                "llm_prompt": "generate a flow for T1059",
                "llm_response": "{...}",
                "reasoning": "chose a linear chain",
                "tools_invoked": ["attack_lookup"],
                "repair_attempts": 0,
                "repair_hints": [],
            },
            created_at=now,
        )
    )

    # Operationalization stage output — some processing elements absent, so the
    # read layer must surface the "not_available" sentinel for them. Note
    # ``repair_hints`` is omitted entirely (missing key), the others are None.
    op_output = AgentStageOutput(
        iteration_id=iteration.id,
        stage=AgentStage.OPERATIONALIZATION,
        origin=TranslationSource.AI_GENERATED,
        payload={"commands": ["whoami"]},
        created_at=now + dt.timedelta(seconds=1),
    )
    session.add(op_output)
    session.flush()
    session.add(
        AgentStageTrace(
            stage_output_id=op_output.id,
            agent_input={"prior_artifacts": {"flow": "..."}},
            processing={
                "llm_prompt": "operationalize the flow",
                "llm_response": None,
                "reasoning": None,
                "tools_invoked": None,
                "repair_attempts": None,
                # repair_hints omitted entirely.
            },
            created_at=now + dt.timedelta(seconds=1),
        )
    )

    session.flush()
    session.commit()
    return run


def test_get_run_returns_stage_outputs_trace_and_sentinel(
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    """GET returns stage outputs ordered by stage, each with origin/payload/trace.

    Null/absent Agent_Processing elements resolve to the ``"not_available"``
    sentinel and the run ``error`` is null on a successful run (Req. 1.6, 2.4).
    """
    run = _seed_completed_run_with_trace(db_session)

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_CAMPAIGN_ID}/ai/runs/{run.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # A successful run carries no error and no failing agent.
    assert body["run"]["error"] is None

    iterations = body["iterations"]
    assert len(iterations) == 1
    iteration = iterations[0]
    assert iteration["failing_agent"] is None

    stage_outputs = iteration["stage_outputs"]
    assert len(stage_outputs) == 2

    # Ordered by stage: "flow" sorts before "operationalization".
    assert [so["stage"] for so in stage_outputs] == [
        AgentStage.FLOW.value,
        AgentStage.OPERATIONALIZATION.value,
    ]

    # Every stage output is AI-generated with its payload and a trace.
    for so in stage_outputs:
        assert so["origin"] == TranslationSource.AI_GENERATED.value
        assert isinstance(so["payload"], dict)
        assert "agent_input" in so["trace"]
        assert "processing" in so["trace"]

    # The attack flow output trace carries fully populated processing (no sentinel).
    flow_proc = stage_outputs[0]["trace"]["processing"]
    assert flow_proc["llm_prompt"] == "generate a flow for T1059"
    assert flow_proc["llm_response"] == "{...}"
    assert flow_proc["reasoning"] == "chose a linear chain"
    assert flow_proc["tools_invoked"] == ["attack_lookup"]
    assert flow_proc["repair_attempts"] == 0
    assert flow_proc["repair_hints"] == []
    assert NOT_AVAILABLE not in flow_proc.values()

    # The op output trace has absent/None elements resolved to "not_available".
    op_proc = stage_outputs[1]["trace"]["processing"]
    assert op_proc["llm_prompt"] == "operationalize the flow"  # present, kept.
    assert op_proc["llm_response"] == NOT_AVAILABLE  # None -> sentinel.
    assert op_proc["reasoning"] == NOT_AVAILABLE
    assert op_proc["tools_invoked"] == NOT_AVAILABLE
    assert op_proc["repair_attempts"] == NOT_AVAILABLE
    assert op_proc["repair_hints"] == NOT_AVAILABLE  # missing key -> sentinel.
    # The agent_input the agent received is surfaced as-is.
    assert stage_outputs[1]["trace"]["agent_input"] == {"prior_artifacts": {"flow": "..."}}


# ===========================================================================
# 2. ``_fail_run`` — failing stage + absent-input cause in agent_runs.error,
#    partial trace persisted, and the read endpoint surfaces error +
#    failing_agent (Req. 2.6, 4.4)
# ===========================================================================


class _NodeFailureWithTrace(RuntimeError):
    """A node error naming the failing stage and carrying its partial trace.

    Mirrors the design's "Failure capture": the failing agent attaches the
    partial ``AgentTrace`` it accumulated in its sink (up to the failure) and the
    short node key + absent-input ``cause`` onto the raised exception, so
    ``_fail_run`` can record the cause and persist the partial trace.
    """

    def __init__(self, node: str, cause: str, trace: dict) -> None:
        super().__init__(f"{node} failed: {cause}")
        self.node = node
        self.cause = cause
        self.partial_trace = trace


class _FailingGraph:
    """A fake compiled graph whose ``invoke`` raises the node failure."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.invocations: list[dict] = []

    def invoke(self, state: Any, config: dict | None = None) -> dict:
        self.invocations.append(config or {})
        raise self._exc


def _install_failing_graph(monkeypatch: pytest.MonkeyPatch, graph: _FailingGraph) -> None:
    """Patch ``_build_graph``/``_require_dependencies`` so the failure path runs."""
    monkeypatch.setattr(AgentRunService, "_build_graph", lambda self: graph, raising=True)
    monkeypatch.setattr(AgentRunService, "_require_dependencies", lambda self: None, raising=True)


def test_fail_run_records_error_and_persists_partial_trace(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node failure writes the cause into agent_runs.error + persists the trace.

    The failing stage ``operationalize`` could not be filled because its required
    Agent_Input was absent; ``_fail_run`` flips the run to FAILED, records the
    failing stage + absent-input cause in ``agent_runs.error``, and persists the
    partial Audit_Trace (a minimal anchor stage output + its 1:1 trace row) for
    the failing stage (Req. 2.6, 4.4).
    """
    partial_trace = {
        "stage": AgentStage.OPERATIONALIZATION.value,
        "iteration": 0,
        "agent_input": {"prior_artifacts": {}},
        "processing": {
            "llm_prompt": "operationalize the flow",
            "llm_response": None,
            "reasoning": "aborted: required flow artifact absent",
        },
    }
    exc = _NodeFailureWithTrace(
        node="operationalize",
        cause="required Agent_Input (flow artifact) absent",
        trace=partial_trace,
    )
    _install_failing_graph(monkeypatch, _FailingGraph(exc))

    session = session_factory()
    try:
        with pytest.raises(RunFailedError) as excinfo:
            AgentRunService(session).start(_CAMPAIGN_ID)
        assert excinfo.value.node == "operationalize"
        session.commit()
        # The module-scoped factory is shared, so scope to the newest run.
        run_id = session.execute(select(AgentRun.id).order_by(AgentRun.id.desc())).scalars().first()
    finally:
        session.close()

    # A fresh session confirms what was durably persisted.
    verify = session_factory()
    try:
        run = verify.get(AgentRun, run_id)
        assert run is not None
        # FAILED with a null verdict and the structured cause recorded.
        assert run.status is AgentRunStatus.FAILED
        assert run.final_verdict_success is None
        assert run.error is not None
        assert "operationalize" in run.error
        assert "absent" in run.error.lower()

        # The partial trace was persisted: a MINIMAL anchor stage output (empty
        # payload sentinel) for the failing stage, plus its 1:1 trace row. Scope
        # to this run's iteration(s) since the factory is shared module-wide.
        anchor = verify.execute(
            select(AgentStageOutput)
            .join(AgentIteration, AgentStageOutput.iteration_id == AgentIteration.id)
            .where(
                AgentIteration.run_id == run_id,
                AgentStageOutput.stage == AgentStage.OPERATIONALIZATION,
            )
        ).scalar_one()
        assert anchor.origin is TranslationSource.AI_GENERATED
        assert anchor.payload == {}  # no valid artifact produced.

        trace_count = verify.execute(
            select(func.count())
            .select_from(AgentStageTrace)
            .where(AgentStageTrace.stage_output_id == anchor.id)
        ).scalar_one()
        assert trace_count == 1
    finally:
        verify.close()


def test_get_run_surfaces_error_and_failing_agent_on_failed_run(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read endpoint surfaces run.error + the iteration's failing_agent (Req. 2.6)."""
    partial_trace = {
        "stage": AgentStage.FLOW.value,
        "iteration": 0,
        "agent_input": {"attack_context": "T1059"},
        "processing": {"llm_prompt": "generate a flow", "llm_response": None},
    }
    exc = _NodeFailureWithTrace(
        node="flow",
        cause="LLM output unparseable after one repair",
        trace=partial_trace,
    )
    _install_failing_graph(monkeypatch, _FailingGraph(exc))

    session = session_factory()
    try:
        with pytest.raises(RunFailedError):
            AgentRunService(session).start(_CAMPAIGN_ID)
        session.commit()
        # The module-scoped factory is shared, so scope to the newest run.
        run_id = session.execute(select(AgentRun.id).order_by(AgentRun.id.desc())).scalars().first()
    finally:
        session.close()

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_CAMPAIGN_ID}/ai/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The run summary surfaces the structured failure cause.
    assert body["run"]["status"] == AgentRunStatus.FAILED.value
    assert body["run"]["error"] is not None
    assert "flow" in body["run"]["error"]

    # The failing stage is reported on the iteration's failing_agent via its
    # minimal (empty-payload) anchor stage output.
    iterations = body["iterations"]
    assert len(iterations) == 1
    iteration = iterations[0]
    assert iteration["failing_agent"] == AgentStage.FLOW.value

    anchor = iteration["stage_outputs"][0]
    assert anchor["stage"] == AgentStage.FLOW.value
    assert anchor["payload"] == {}
    # The partial trace surfaces: captured elements kept, absent -> sentinel.
    processing = anchor["trace"]["processing"]
    assert processing["llm_prompt"] == "generate a flow"
    assert processing["llm_response"] == NOT_AVAILABLE
    assert processing["reasoning"] == NOT_AVAILABLE
