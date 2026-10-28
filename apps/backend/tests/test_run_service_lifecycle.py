"""Unit tests for AgentRunService lifecycle, atomic persistence and failures (task 15.4).

Exercise :class:`app.services.ai.run_service.AgentRunService` and the
startup seam fallback :func:`app.services.ai.run_service.resolve_stage_agent`
end to end with **fakes only** — no SEPINF endpoint, no PostgreSQL, no real
LangGraph. The compiled graph is replaced by monkeypatching
``AgentRunService._build_graph`` to return a fake compiled graph whose
``invoke`` returns a controlled final ``AgentState`` (or raises), the
checkpointer/pgvector availability probe is patched to a no-op, and persistence
lands in an **ephemeral per-test temp-file SQLite** built straight from the ORM
(``Base.metadata.create_all``) so the AI bookkeeping rows round-trip without a
migration.

Coverage:

* **Req 4.2 / 11.4 — ``thread_id`` non-empty, unique, immutable.** ``start``
  stamps each run with a non-empty ``thread_id`` that is unique across runs and
  is never rewritten for the life of the run (it is identical before and after
  the graph invocation and terminal transition).
* **Req 3.2 / 3.4 — COMPLETED vs ABORTED marking.** A final state carrying a
  passing evaluator verdict marks the run ``COMPLETED``; a final state where the
  guard stopped the loop without a passing verdict marks it ``ABORTED``. The
  summary columns (``final_verdict_success`` / ``iterations_used``) agree.
* **Req 11.7 — rollback on partial persistence.** When persisting a completed
  iteration's stage output (or its evaluation) fails, the whole iteration write
  is rolled back — no ``agent_iterations`` / ``agent_stage_outputs`` /
  ``agent_evaluations`` rows for the iteration survive — and a
  :class:`PersistenceError` is raised.
* **Req 14.1 / 14.2 / 14.6 — FAILED on SEPINF/node errors.** A graph invocation
  that raises a SEPINF transport/timeout/model error OR a
  :class:`~app.services.ai.agents.dag_agent.DagGenerationError` flips the run to
  ``FAILED`` recording the failing-node cause, persists NO AI artifact, leaves
  ``final_verdict_success`` null, and never transitions the run out of
  ``FAILED``; a :class:`RunFailedError` naming the failing node is raised.
* **Req 12.2 / 12.3 — startup fallback diagnostic.** When a stage's AI agent
  fails to initialize, :func:`resolve_stage_agent` returns that stage's curation
  implementation (which structurally satisfies the same seam Protocol) and logs
  a diagnostic naming the affected stage.

_Requirements: 4.2, 11.4, 11.7, 14.1, 14.2, 14.6, 12.2, 12.3_
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    AgentEvaluation,
    AgentIteration,
    AgentRun,
    AgentStageOutput,
)
from app.models.enums import AgentRunStatus
from app.services.ai import run_service as run_service_module
from app.services.ai.run_service import (
    AgentRunService,
    PersistenceError,
    RunFailedError,
    resolve_stage_agent,
)

_CAMPAIGN_ID = "campaign-under-test"


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per test).
#
# Mirrors the temp-file SQLite + ``Base.metadata.create_all`` pattern used by
# the other AI tests (e.g. test_ai_op_origin_stamping_property.py): the schema
# is built straight from the ORM, so the AI bookkeeping tables (agent_runs,
# agent_iterations, agent_stage_outputs, agent_evaluations) round-trip without a
# migration. SQLite does not enforce FKs by default, so no Campaign row is
# required to persist an AgentRun under an arbitrary campaign_id.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward (no ``.db`` state leaks between tests).
    """
    tmp_dir = tempfile.mkdtemp(prefix="run_service_lifecycle_")
    db_path = Path(tmp_dir) / "run_service.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


@pytest.fixture()
def session() -> Any:
    """Yield an ephemeral SQLite session, tearing the engine/file down after."""
    sess, db_path, engine = _build_ephemeral_session()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            db_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Fakes — a scripted compiled graph with the single ``invoke`` surface the
# service uses, plus a SEPINF transport error.
# ---------------------------------------------------------------------------


class _FakeGraph:
    """Stand-in compiled graph exposing only ``invoke(state, config=...)``.

    Returns a controlled final ``AgentState`` dict (or raises a controlled
    exception) and records every ``invoke`` config so the thread_id the service
    passed to the checkpointer can be asserted.
    """

    def __init__(
        self, *, final_state: dict | None = None, raises: BaseException | None = None
    ) -> None:
        self._final_state = final_state or {}
        self._raises = raises
        self.invocations: list[dict] = []

    def invoke(self, state: Any, config: dict | None = None) -> dict:
        self.invocations.append(config or {})
        if self._raises is not None:
            raise self._raises
        return dict(self._final_state)


class _SepinfTimeoutError(RuntimeError):
    """A SEPINF transport error carrying no ``node`` attribute (unreachable/timeout)."""


def _install_fake_graph(
    monkeypatch: pytest.MonkeyPatch, graph: _FakeGraph
) -> None:
    """Patch ``_build_graph`` so the service invokes ``graph`` and probes pass.

    The dependency-availability probe is replaced by a no-op so the test never
    needs a real checkpointer/pgvector; the lifecycle under test is the graph
    invocation and persistence, not the probe (covered elsewhere).
    """
    monkeypatch.setattr(
        AgentRunService, "_build_graph", lambda self: graph, raising=True
    )
    monkeypatch.setattr(
        AgentRunService, "_require_dependencies", lambda self: None, raising=True
    )


def _passing_final_state() -> dict:
    """A final state carrying a passing verdict and one completed iteration."""
    return {
        "campaign_id": _CAMPAIGN_ID,
        "iteration": 0,
        "max_iterations": 3,
        "evaluation": {"verdict_success": True},
        "artifacts": {},
        "history": [],
    }


def _aborted_final_state(max_iterations: int = 2) -> dict:
    """A final state where the guard stopped the loop without a passing verdict."""
    return {
        "campaign_id": _CAMPAIGN_ID,
        "iteration": max_iterations - 1,
        "max_iterations": max_iterations,
        "evaluation": {"verdict_success": False},
        "artifacts": {},
        "history": [],
    }


# ===========================================================================
# Requirement 4.2 / 11.4 — thread_id is non-empty, unique and immutable
# ===========================================================================


def test_thread_id_is_non_empty(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start`` stamps every run with a non-empty ``thread_id`` (Req. 11.4)."""
    _install_fake_graph(monkeypatch, _FakeGraph(final_state=_passing_final_state()))

    run = AgentRunService(session).start(_CAMPAIGN_ID)

    assert isinstance(run.thread_id, str)
    assert run.thread_id != ""


def test_thread_id_is_unique_across_runs(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separate runs receive distinct ``thread_id`` values (Req. 4.2, 11.4)."""
    _install_fake_graph(monkeypatch, _FakeGraph(final_state=_passing_final_state()))
    svc = AgentRunService(session)

    thread_ids = {svc.start(_CAMPAIGN_ID).thread_id for _ in range(25)}

    assert len(thread_ids) == 25, "each run must mint a unique thread_id"


def test_thread_id_is_immutable_for_life_of_run(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``thread_id`` passed to the graph equals the final persisted one.

    The service minted the id at creation; it is the SAME value the graph was
    invoked under (so the checkpoint thread matches the run) and the SAME value
    the row carries after the terminal transition — never rewritten (Req. 4.2).
    """
    graph = _FakeGraph(final_state=_passing_final_state())
    _install_fake_graph(monkeypatch, graph)

    run = AgentRunService(session).start(_CAMPAIGN_ID)

    invoked_thread = graph.invocations[0]["configurable"]["thread_id"]
    assert invoked_thread == run.thread_id

    # Reload from the DB to confirm the persisted value is the minted one.
    session.expire_all()
    reloaded = session.get(AgentRun, run.id)
    assert reloaded is not None
    assert reloaded.thread_id == run.thread_id


# ===========================================================================
# Requirement 3.2 / 3.4 — COMPLETED on a passing verdict, ABORTED on the guard
# ===========================================================================


def test_start_marks_completed_on_passing_verdict(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing final verdict marks the run COMPLETED (Req. 3.2)."""
    _install_fake_graph(monkeypatch, _FakeGraph(final_state=_passing_final_state()))

    run = AgentRunService(session).start(_CAMPAIGN_ID)

    assert run.status is AgentRunStatus.COMPLETED
    assert run.final_verdict_success is True
    assert run.iterations_used == 1


def test_start_marks_aborted_when_guard_stops_without_verdict(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop stopped at the guard without a passing verdict is ABORTED (Req. 3.4)."""
    _install_fake_graph(
        monkeypatch, _FakeGraph(final_state=_aborted_final_state(max_iterations=2))
    )

    run = AgentRunService(session).start(_CAMPAIGN_ID, max_iterations=2)

    assert run.status is AgentRunStatus.ABORTED
    assert run.final_verdict_success is False
    # The guard bounds iterations_used to max_iterations (Req. 3.5).
    assert run.iterations_used == 2


def test_start_aborts_when_no_evaluation_present(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final state with no evaluation at all reads as ABORTED, not COMPLETED."""
    final_state = _passing_final_state()
    final_state.pop("evaluation")
    _install_fake_graph(monkeypatch, _FakeGraph(final_state=final_state))

    run = AgentRunService(session).start(_CAMPAIGN_ID)

    assert run.status is AgentRunStatus.ABORTED
    assert run.final_verdict_success is False


# ===========================================================================
# Requirement 11.7 — rollback on partial per-iteration persistence
# ===========================================================================


def _op_artifact_final_state() -> dict:
    """A passing final state carrying an operationalization artifact to persist.

    The presence of ``artifacts.operationalization`` drives
    ``_persist_stage_outputs`` to call ``op_agent.persist_stage_output`` for the
    completed iteration; the rollback tests make that call raise.
    """
    state = _passing_final_state()
    state["artifacts"] = {
        "operationalization": {"origin": "gerado_ia", "nodes": []}
    }
    return state


def _count_rows(session: Session) -> tuple[int, int, int]:
    """Return (agent_iterations, agent_stage_outputs, agent_evaluations) counts."""
    iterations = session.execute(
        select(func.count()).select_from(AgentIteration)
    ).scalar_one()
    outputs = session.execute(
        select(func.count()).select_from(AgentStageOutput)
    ).scalar_one()
    evaluations = session.execute(
        select(func.count()).select_from(AgentEvaluation)
    ).scalar_one()
    return iterations, outputs, evaluations


def test_partial_persistence_rolls_back_and_raises(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing stage-output persist rolls back the whole iteration (Req. 11.7).

    ``_persist_stage_outputs`` flushes the ``agent_iterations`` row first, then
    persists the stage output; patching the operationalization persist to raise
    forces the nested transaction to roll back so NO iteration / stage-output /
    evaluation rows survive, and a PersistenceError is raised.
    """
    _install_fake_graph(
        monkeypatch, _FakeGraph(final_state=_op_artifact_final_state())
    )

    from app.services.ai.agents import op_agent

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("simulated stage-output persistence failure")

    monkeypatch.setattr(op_agent, "persist_stage_output", _boom, raising=True)

    with pytest.raises(PersistenceError):
        AgentRunService(session).start(_CAMPAIGN_ID)

    # The whole iteration write was rolled back: no partial rows survive.
    assert _count_rows(session) == (0, 0, 0)


def test_partial_persistence_leaves_no_evaluation_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing evaluation persist also rolls back the iteration + its outputs.

    The iteration row and the stage-output row are written first; making the
    evaluation persist raise must roll BOTH of them back too (atomic iteration),
    leaving zero rows across all three tables (Req. 11.7).
    """
    _install_fake_graph(
        monkeypatch, _FakeGraph(final_state=_op_artifact_final_state())
    )

    from app.services.ai.agents import evaluator, op_agent

    # Let the stage output "persist" without error so the evaluation is the sole
    # failure point, proving the already-written rows are rolled back with it.
    monkeypatch.setattr(
        op_agent, "persist_stage_output", lambda *a, **k: None, raising=True
    )

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("simulated evaluation persistence failure")

    monkeypatch.setattr(evaluator, "persist_evaluation", _boom, raising=True)

    with pytest.raises(PersistenceError):
        AgentRunService(session).start(_CAMPAIGN_ID)

    assert _count_rows(session) == (0, 0, 0)


# ===========================================================================
# Requirement 14.1 / 14.2 / 14.6 — FAILED on SEPINF / node errors
# ===========================================================================


def test_sepinf_error_marks_run_failed_and_persists_no_artifact(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SEPINF transport/timeout error flips the run to FAILED (Req. 14.1, 14.2).

    No AI artifact is persisted (no iteration/stage-output/evaluation rows),
    ``final_verdict_success`` is left null, and a RunFailedError is raised.
    """
    graph = _FakeGraph(raises=_SepinfTimeoutError("SEPINF unreachable (timeout)"))
    _install_fake_graph(monkeypatch, graph)

    svc = AgentRunService(session)
    with pytest.raises(RunFailedError) as excinfo:
        svc.start(_CAMPAIGN_ID)

    # A transport error carries no node attribute -> reported as "unknown".
    assert excinfo.value.node == "unknown"

    # The run row exists and is FAILED with a null verdict; no artifact persisted.
    run = session.execute(select(AgentRun)).scalar_one()
    assert run.status is AgentRunStatus.FAILED
    assert run.final_verdict_success is None
    assert _count_rows(session) == (0, 0, 0)


def test_dag_generation_error_marks_run_failed_naming_node(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DagGenerationError names the failing node ``"dag"`` on FAILED (Req. 14.4)."""
    from app.services.ai.agents.dag_agent import DagGenerationError

    graph = _FakeGraph(
        raises=DagGenerationError("unparseable after one repair", node="dag")
    )
    _install_fake_graph(monkeypatch, graph)

    with pytest.raises(RunFailedError) as excinfo:
        AgentRunService(session).start(_CAMPAIGN_ID)

    assert excinfo.value.node == "dag"
    run = session.execute(select(AgentRun)).scalar_one()
    assert run.status is AgentRunStatus.FAILED
    assert run.final_verdict_success is None


def test_failed_run_does_not_transition_out_of_failed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once FAILED the run never reads as COMPLETED/ABORTED (Req. 14.6).

    After a failing ``start`` the persisted status must remain FAILED even when
    reloaded from the database — the service made no later transition.
    """
    graph = _FakeGraph(raises=_SepinfTimeoutError("model error"))
    _install_fake_graph(monkeypatch, graph)

    with pytest.raises(RunFailedError):
        AgentRunService(session).start(_CAMPAIGN_ID)

    session.expire_all()
    run = session.execute(select(AgentRun)).scalar_one()
    assert run.status is AgentRunStatus.FAILED
    assert run.status is not AgentRunStatus.COMPLETED
    assert run.status is not AgentRunStatus.ABORTED


# ===========================================================================
# Requirement 12.2 / 12.3 — startup seam fallback diagnostic
# ===========================================================================


def _patch_ai_initializer(
    monkeypatch: pytest.MonkeyPatch, stage: str, init: Any
) -> None:
    """Swap the AI initializer for ``stage`` in the table ``resolve_stage_agent`` reads.

    ``resolve_stage_agent`` resolves the (AI, curation) initializer pair from the
    module-level ``_STAGE_INITIALIZERS`` dict, which captured the real function
    objects at import time — so patching the module attribute alone is not seen.
    This patches the dict entry itself, keeping the curation fallback intact.
    """
    _ai, curation = run_service_module._STAGE_INITIALIZERS[stage]
    monkeypatch.setitem(
        run_service_module._STAGE_INITIALIZERS, stage, (init, curation)
    )


def test_resolve_stage_agent_falls_back_to_curation_on_init_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing AI init falls back to curation behind the same seam (Req. 12.3).

    The DAG stage's AI initializer is patched to raise; ``resolve_stage_agent``
    must return the curation implementation (which satisfies the ``DagGenerator``
    seam Protocol structurally) and log a diagnostic naming the affected stage.
    """
    from app.services.dag.generator import CurationDagGenerator, DagGenerator

    def _boom() -> Any:
        raise RuntimeError("AI DAG generator failed to initialize")

    _patch_ai_initializer(monkeypatch, "dag", _boom)

    with caplog.at_level(logging.ERROR, logger=run_service_module.__name__):
        agent = resolve_stage_agent("dag", prefer_ai=True)

    assert isinstance(agent, CurationDagGenerator)
    # Structural seam satisfaction: the fallback plugs into the same seam.
    assert isinstance(agent, DagGenerator)
    # A diagnostic naming the affected stage was logged (Req. 12.3).
    assert any(
        record.levelno >= logging.ERROR and "dag" in record.getMessage().lower()
        for record in caplog.records
    ), "expected an ERROR diagnostic naming the affected stage"


def test_resolve_stage_agent_operationalize_fallback_satisfies_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 4 AI init failure falls back to the curation operationalizer (Req. 12.3)."""
    from app.services.environment.operationalizer import (
        CurationOperationalizer,
        OperationalizerByOrigin,
    )

    def _boom() -> Any:
        raise RuntimeError("op init failed")

    _patch_ai_initializer(monkeypatch, "operationalize", _boom)

    agent = resolve_stage_agent("operationalize", prefer_ai=True)

    assert isinstance(agent, CurationOperationalizer)
    assert isinstance(agent, OperationalizerByOrigin)


def test_resolve_stage_agent_environment_fallback_satisfies_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 5 AI init failure falls back to the curation provisioner (Req. 12.3)."""
    from app.services.environment.provisioner import (
        CurationEnvironmentProvisioner,
        EnvironmentProvisioner,
    )

    def _boom() -> Any:
        raise RuntimeError("env init failed")

    _patch_ai_initializer(monkeypatch, "environment", _boom)

    agent = resolve_stage_agent("environment", prefer_ai=True)

    assert isinstance(agent, CurationEnvironmentProvisioner)
    assert isinstance(agent, EnvironmentProvisioner)


def test_resolve_stage_agent_not_deployed_returns_curation_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage whose AI agent is not deployed runs on curation (Req. 12.2).

    With ``prefer_ai=False`` the curation implementation is returned directly and
    the AI initializer is never consulted (no raise, no fallback path).
    """
    from app.services.dag.generator import CurationDagGenerator

    def _must_not_be_called() -> Any:  # pragma: no cover - must never run
        raise AssertionError("AI initializer must not be consulted when not deployed")

    _patch_ai_initializer(monkeypatch, "dag", _must_not_be_called)

    agent = resolve_stage_agent("dag", prefer_ai=False)
    assert isinstance(agent, CurationDagGenerator)
