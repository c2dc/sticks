"""Property test for Audit_Trace persistence atomicity (task 3.8).

# Feature: ai-first-campaign-flow, Property 3: Audit-trace persistence atomicity

For any iteration and any injected failure point during its persistence, the
whole iteration write must be rolled back — after the failure the database
retains **no** ``agent_iterations`` / ``agent_stage_outputs`` /
``agent_stage_traces`` / ``agent_evaluations`` rows for that iteration — and a
:class:`~app.services.ai.run_service.PersistenceError` is raised.

``AgentRunService._persist_iterations`` opens a ``begin_nested()`` SAVEPOINT and
writes the iteration row + stage outputs + 1:1 traces + evaluation inside it
(design §B). Tasks 3.4/3.6 moved the ``agent_stage_traces`` write into that same
SAVEPOINT, so a failure at ANY persistence step — the iteration row, a stage
output, the trace, or the evaluation — must roll the entire iteration back.

The test drives the real service with **fakes only**: a scripted compiled graph
whose ``invoke`` returns a controlled passing final ``AgentState`` carrying an
operationalization artifact, its matching ``AgentTrace`` and a passing
evaluation; the availability probe patched to a no-op; and persistence landing
in an ephemeral temp-file SQLite built straight from the ORM. Hypothesis selects
the injected failure point (and a varied iteration index / trace shape) so the
atomicity property is exercised across every persistence step.

Validates: Requirements 2.5
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    AgentEvaluation,
    AgentIteration,
    AgentStageOutput,
    AgentStageTrace,
)
from app.models.enums import AgentStage
from app.services.ai.run_service import AgentRunService, PersistenceError
from app.services.flow.flow_view import FlowNodeView, FlowView

_CAMPAIGN_ID = "campaign-under-test"


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
#
# Mirrors the harness in test_ai_stage_trace_persistence.py and
# test_run_service_lifecycle.py: the schema is built straight from the ORM so
# the agent_* bookkeeping tables round-trip without a migration.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    tmp_dir = tempfile.mkdtemp(prefix="ai_trace_atomicity_")
    db_path = Path(tmp_dir) / "ai_trace_atomicity.db"
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


# ---------------------------------------------------------------------------
# Fakes — scripted compiled graph + a passing op-artifact final state.
# ---------------------------------------------------------------------------


class _FakeGraph:
    def __init__(self, *, final_state: dict) -> None:
        self._final_state = final_state

    def invoke(self, state: Any, config: dict | None = None) -> dict:
        return dict(self._final_state)


def _install_fake_graph(monkeypatch: pytest.MonkeyPatch, graph: _FakeGraph) -> None:
    monkeypatch.setattr(
        AgentRunService, "_build_graph", lambda self: graph, raising=True
    )
    monkeypatch.setattr(
        AgentRunService, "_require_dependencies", lambda self: None, raising=True
    )


def _produced_op() -> Any:
    """A real ProducedOperationalization built by the deterministic agent."""
    from app.services.ai.agents.op_agent import AiOperationalizer

    flow = FlowView(
        campaign_id=_CAMPAIGN_ID,
        name=_CAMPAIGN_ID,
        curated=True,
        can_advance=True,
        nodes=[
            FlowNodeView(
                node_id="n1",
                technique_id="T1059",
                technique_name="Command Interpreter",
                tactic="execution",
            )
        ],
    )
    agent = AiOperationalizer(atomic_lookup=lambda _tid: [])
    return agent.operationalize(_CAMPAIGN_ID, flow)


def _op_trace(iteration: int) -> dict:
    """An AgentTrace for the operationalization stage of ``iteration``."""
    return {
        "stage": AgentStage.OPERATIONALIZATION.value,
        "iteration": iteration,
        "agent_input": {"flow": {"nodes": ["n1"]}, "prior_artifacts": None},
        "processing": {
            "llm_prompt": [{"role": "user", "content": "choose commands"}],
            "llm_response": "selected",
            "reasoning": "picked the only node",
            "tools_invoked": ["atomic_lookup"],
            "repair_attempts": 0,
            "repair_hints": None,
        },
    }


def _passing_final_state(iteration: int, *, with_trace: bool) -> dict:
    """A passing final state with an op artifact (+trace) and a passing evaluation.

    ``iteration`` is 0-based; ``iterations_used`` is derived by the service as
    ``iteration + 1``, so the persisted iteration row is keyed on ``iteration``.
    """
    state: dict[str, Any] = {
        "campaign_id": _CAMPAIGN_ID,
        "iteration": iteration,
        "max_iterations": max(iteration + 1, 3),
        "evaluation": {
            "verdict_success": True,
            "decision": "finish",
            "rationale": "all checks passed",
        },
        "artifacts": {"operationalization": _produced_op().model_dump(mode="json")},
        "history": [],
    }
    if with_trace:
        state["traces"] = [_op_trace(iteration)]
    return state


def _count(session: Session, model: Any) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def _all_counts(session: Session) -> tuple[int, int, int, int]:
    """(agent_iterations, agent_stage_outputs, agent_stage_traces, agent_evaluations)."""
    return (
        _count(session, AgentIteration),
        _count(session, AgentStageOutput),
        _count(session, AgentStageTrace),
        _count(session, AgentEvaluation),
    )


# ---------------------------------------------------------------------------
# Failure-injection points: each patches a distinct step of the per-iteration
# persistence to raise, so Hypothesis can vary WHERE the failure lands.
# ---------------------------------------------------------------------------


def _boom(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("simulated persistence failure")


def _inject_iteration_row_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        AgentRunService, "_persist_iteration_row", _boom, raising=True
    )


def _inject_stage_output_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.ai.agents import op_agent

    monkeypatch.setattr(op_agent, "persist_stage_output", _boom, raising=True)


def _inject_trace_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.ai.agents import _trace as trace_mod

    monkeypatch.setattr(trace_mod, "persist_stage_trace", _boom, raising=True)


def _inject_evaluation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.ai.agents import evaluator

    monkeypatch.setattr(evaluator, "persist_evaluation", _boom, raising=True)


# Each entry: (name, injector, requires_trace). The trace failure point only
# fires when a matching trace is present in the channel, so it forces with_trace.
_FAILURE_POINTS = [
    ("iteration_row", _inject_iteration_row_failure, False),
    ("stage_output", _inject_stage_output_failure, False),
    ("trace", _inject_trace_failure, True),
    ("evaluation", _inject_evaluation_failure, False),
]


# ===========================================================================
# Property 3 — audit-trace persistence atomicity
# ===========================================================================


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    iteration=st.integers(min_value=0, max_value=4),
    failure=st.sampled_from(_FAILURE_POINTS),
    trace_present=st.booleans(),
)
def test_property_persistence_failure_rolls_back_whole_iteration(
    iteration: int,
    failure: tuple[str, Any, bool],
    trace_present: bool,
) -> None:
    """# Feature: ai-first-campaign-flow, Property 3: Audit-trace persistence atomicity.

    For any iteration and any injected failure point during persistence, the
    service raises :class:`PersistenceError` and the database retains zero
    ``agent_iterations`` / ``agent_stage_outputs`` / ``agent_stage_traces`` /
    ``agent_evaluations`` rows for that iteration.

    Validates: Requirements 2.5
    """
    _name, injector, requires_trace = failure
    with_trace = trace_present or requires_trace

    sess, db_path, engine = _build_ephemeral_session()
    try:
        monkeypatch = pytest.MonkeyPatch()
        try:
            _install_fake_graph(
                monkeypatch,
                _FakeGraph(
                    final_state=_passing_final_state(iteration, with_trace=with_trace)
                ),
            )
            injector(monkeypatch)

            with pytest.raises(PersistenceError):
                AgentRunService(sess).start(_CAMPAIGN_ID)
        finally:
            monkeypatch.undo()

        # The nested transaction rolled the whole iteration back: no partial rows
        # of any kind survive (Req. 2.5).
        assert _all_counts(sess) == (0, 0, 0, 0)
    finally:
        sess.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
