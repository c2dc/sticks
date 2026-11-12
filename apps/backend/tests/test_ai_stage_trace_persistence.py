"""Tests for the 1:1 Audit_Trace persistence inside the per-iteration SAVEPOINT (task 3.6).

Exercise the extended ``persist_*`` hooks and ``AgentRunService._persist_iterations``
wiring: each stage output persisted for a completed iteration gets exactly one
matching ``agent_stage_traces`` row (1:1 via the ``stage_output_id`` FK), written
in the SAME flush inside the existing ``begin_nested()`` SAVEPOINT. Because the
trace write stays inside that savepoint, a trace-persistence failure rolls the
whole iteration back (iteration row + stage outputs + traces + evaluation),
retaining zero rows (design §B).

The tests drive the real service with **fakes only** — a scripted compiled graph
whose ``invoke`` returns a controlled final ``AgentState`` carrying both the stage
artifacts and the ``traces`` channel, the availability probe patched to a no-op,
and persistence landing in an ephemeral temp-file SQLite built straight from the
ORM. No SEPINF endpoint, no PostgreSQL, no real LangGraph.

_Requirements: 2.1, 2.5_
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
from app.services.ai.agents.op_agent import AiOperationalizer
from app.services.ai.run_service import AgentRunService, PersistenceError
from app.services.flow.flow_view import FlowNodeView, FlowView

_CAMPAIGN_ID = "campaign-under-test"


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per test).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    tmp_dir = tempfile.mkdtemp(prefix="ai_stage_trace_persist_")
    db_path = Path(tmp_dir) / "ai_stage_trace.db"
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
# Fakes — scripted compiled graph + final-state builder.
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


def _op_trace(iteration: int = 0, *, full_processing: bool = True) -> dict:
    """An AgentTrace for the operationalization stage of ``iteration``."""
    processing: dict[str, Any] = {
        "llm_prompt": [{"role": "user", "content": "choose commands"}],
        "llm_response": "selected",
    }
    if full_processing:
        processing.update(
            {
                "reasoning": "picked the only node",
                "tools_invoked": ["atomic_lookup"],
                "repair_attempts": 0,
                "repair_hints": None,
            }
        )
    return {
        "stage": AgentStage.OPERATIONALIZATION.value,
        "iteration": iteration,
        "agent_input": {"flow": {"nodes": ["n1"]}, "prior_artifacts": None},
        "processing": processing,
    }


def _final_state_with_op_and_trace(trace: dict | None) -> dict:
    """A passing final state carrying an op artifact and (optionally) its trace."""
    state: dict[str, Any] = {
        "campaign_id": _CAMPAIGN_ID,
        "iteration": 0,
        "max_iterations": 3,
        "evaluation": {"verdict_success": True},
        "artifacts": {"operationalization": _produced_op().model_dump(mode="json")},
        "history": [],
    }
    if trace is not None:
        state["traces"] = [trace]
    return state


def _count(session: Session, model: Any) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ===========================================================================
# 1:1 trace persistence inside the SAVEPOINT (Req. 2.1, 2.5)
# ===========================================================================


def test_stage_output_gets_one_matching_trace_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted op stage output gets exactly one 1:1 ``agent_stage_traces`` row."""
    _install_fake_graph(
        monkeypatch,
        _FakeGraph(final_state=_final_state_with_op_and_trace(_op_trace())),
    )

    AgentRunService(session).start(_CAMPAIGN_ID)

    outputs = session.execute(select(AgentStageOutput)).scalars().all()
    traces = session.execute(select(AgentStageTrace)).scalars().all()

    assert len(outputs) == 1
    assert len(traces) == 1
    # 1:1: the trace links the just-persisted stage output via its FK.
    assert traces[0].stage_output_id == outputs[0].id
    # The captured input + processing round-tripped into the JSON columns.
    assert traces[0].agent_input == {"flow": {"nodes": ["n1"]}, "prior_artifacts": None}
    assert traces[0].processing["llm_response"] == "selected"


def test_absent_processing_elements_carried_as_none_sentinel(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Genuinely-absent processing elements persist as the explicit ``None`` sentinel.

    A trace whose processing omits ``reasoning``/``tools_invoked``/``repair_attempts``/
    ``repair_hints`` must store those keys present-with-``None`` rather than dropping
    them, so the read layer renders "not available" (design §A).
    """
    trace = _op_trace(full_processing=False)
    _install_fake_graph(
        monkeypatch, _FakeGraph(final_state=_final_state_with_op_and_trace(trace))
    )

    AgentRunService(session).start(_CAMPAIGN_ID)

    stored = session.execute(select(AgentStageTrace)).scalars().one()
    processing = stored.processing
    for key in ("reasoning", "tools_invoked", "repair_attempts", "repair_hints"):
        assert key in processing, f"{key} must be present (not omitted)"
        assert processing[key] is None, f"{key} must be the None sentinel"
    # The elements the agent DID capture are preserved verbatim.
    assert processing["llm_response"] == "selected"


def test_no_trace_row_when_no_matching_trace(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no matching trace in the channel, the output is persisted with no trace row."""
    _install_fake_graph(
        monkeypatch, _FakeGraph(final_state=_final_state_with_op_and_trace(None))
    )

    AgentRunService(session).start(_CAMPAIGN_ID)

    assert _count(session, AgentStageOutput) == 1
    assert _count(session, AgentStageTrace) == 0


def test_trace_write_failure_rolls_back_whole_iteration(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trace-persistence failure rolls back the whole iteration (atomicity, Req. 2.5).

    Because the trace write lands inside ``_persist_iterations``' SAVEPOINT, making
    it raise must leave zero ``agent_iterations`` / ``agent_stage_outputs`` /
    ``agent_stage_traces`` / ``agent_evaluations`` rows and surface a
    :class:`PersistenceError`.
    """
    _install_fake_graph(
        monkeypatch,
        _FakeGraph(final_state=_final_state_with_op_and_trace(_op_trace())),
    )

    from app.services.ai.agents import _trace as trace_mod

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("simulated trace persistence failure")

    monkeypatch.setattr(trace_mod, "persist_stage_trace", _boom, raising=True)

    with pytest.raises(PersistenceError):
        AgentRunService(session).start(_CAMPAIGN_ID)

    assert _count(session, AgentIteration) == 0
    assert _count(session, AgentStageOutput) == 0
    assert _count(session, AgentStageTrace) == 0
    assert _count(session, AgentEvaluation) == 0


# ===========================================================================
# Property: for any op trace, exactly one 1:1 trace row with normalized processing
# ===========================================================================

_json_scalar = st.one_of(
    st.none(), st.booleans(), st.integers(min_value=-5, max_value=5), st.text(max_size=12)
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    iteration=st.just(0),
    extra_processing=st.dictionaries(
        keys=st.text(alphabet="abcdefghij", min_size=1, max_size=6),
        values=_json_scalar,
        max_size=4,
    ),
    include_full=st.booleans(),
)
def test_property_op_trace_is_one_to_one_and_normalized(
    iteration: int, extra_processing: dict, include_full: bool
) -> None:
    """# Feature: ai-first-campaign-flow, Property: 1:1 Audit_Trace with normalized processing.

    For any captured operationalization trace, persisting a completed iteration
    writes exactly ONE ``agent_stage_traces`` row for the op stage output (1:1 via
    the FK), every known processing element is present (absent ones as the ``None``
    sentinel), and any extra captured processing keys are preserved.

    Validates: Requirements 2.1, 2.5
    """
    trace = _op_trace(iteration, full_processing=include_full)
    # Reserved processing keys are owned by the normalizer; only inject
    # non-conflicting extras so the assertion on preservation is unambiguous.
    reserved = {
        "llm_prompt",
        "llm_response",
        "reasoning",
        "tools_invoked",
        "repair_attempts",
        "repair_hints",
    }
    extras = {k: v for k, v in extra_processing.items() if k not in reserved}
    trace["processing"].update(extras)

    sess, db_path, engine = _build_ephemeral_session()
    try:
        import pytest as _pytest

        monkeypatch = _pytest.MonkeyPatch()
        try:
            _install_fake_graph(
                monkeypatch, _FakeGraph(final_state=_final_state_with_op_and_trace(trace))
            )
            AgentRunService(sess).start(_CAMPAIGN_ID)
        finally:
            monkeypatch.undo()

        outputs = sess.execute(select(AgentStageOutput)).scalars().all()
        traces = sess.execute(select(AgentStageTrace)).scalars().all()

        assert len(outputs) == 1
        assert len(traces) == 1
        assert traces[0].stage_output_id == outputs[0].id

        processing = traces[0].processing
        for key in reserved:
            assert key in processing
        for key, value in extras.items():
            assert processing[key] == value
    finally:
        sess.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
