"""Property test for the Evaluator's decision consistency with metrics (task 13.3).

# Feature: ai-curation-agents, Property 7: Evaluator decision consistency with
# metrics. For any Stage 6 report, the Evaluator decides ``FINISH`` whenever
# ``verdict_success`` is true and ``REFINE`` only when it is false (the
# max-iteration guard that can turn a would-be REFINE into a FINISH/ABORTED is
# enforced downstream in ``route_after_eval`` — task 14.3 — NOT in the evaluator
# node itself); and the persisted evaluation's metrics equal the report metrics
# from which they were derived (read unchanged).

Validates: Requirements 10.2, 10.3, 10.5

This is a *feature* spec (``ai-curation-agents``), so the property is expected to
HOLD on :func:`app.services.ai.agents.evaluator.node` /
:func:`app.services.ai.agents.evaluator.persist_evaluation` as written.

Strategy / isolation
--------------------
Each Hypothesis example generates a random **Stage 6 report** with an explicit
``metrics`` block (``success_rate`` ∈ [0, 1], ``procedural_coverage`` ∈ [0, 1],
``verdict_success`` true/false, and a ``failed_nodes`` list of node ids). The
report carries ``outcome == "concluida"`` so it is an *executed* iteration (the
gate-rejection / missing-report branches of the node are exercised by the
sibling ``test_gate_rejection_surfaced_to_agent.py``; here we focus on the
decision-vs-metrics property over executed reports).

Two things are checked per example:

1. **Decision consistency (evaluator level, Req. 10.2/10.3):** the node's
   ``decision`` is ``FINISH`` **iff** ``verdict_success`` is true and ``REFINE``
   **iff** it is false, and ``refine_stage`` is ``None`` exactly when the
   decision is FINISH (a refine target is diagnosed only on REFINE). The guard
   is downstream, so the evaluator always decides REFINE on a failing verdict.
2. **Persisted metrics equal the report metrics (Req. 10.5):** the evaluation
   the node produced is persisted through :func:`persist_evaluation` against a
   fresh **ephemeral** SQLite database built straight from the ORM
   (``Base.metadata.create_all``), and the stored ``agent_evaluations`` row is
   read back and compared field-by-field to the **report's** metric block —
   ``success_rate``, ``procedural_coverage``, ``verdict_success`` and
   ``failed_nodes`` are read UNCHANGED (the evaluator never recomputes/rescales
   the Stage 6 numbers).

The evaluator runs with **no LLM and no RAG memory** injected (``memory=None``
and no ``state["_session"]``), so the store-back is skipped and the node touches
no socket/subprocess/disk. The persistence step uses a per-example temp-file
SQLite database that is disposed and deleted after use, so no ``.db`` state leaks
between examples. The FK chain ``agent_runs → agent_iterations →
agent_evaluations`` (and the ``campaigns`` row the run references) is created so
the non-null FKs are satisfiable on SQLite.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AgentIteration, AgentRun, Case, _utcnow
from app.models.enums import AgentRunStatus, EvaluatorDecision
from app.services.ai.agents.evaluator import node, persist_evaluation

# The exact English decision strings (Req. 10.2/10.3) — literals, so the
# assertions never trust the implementation to decide the expected value.
_FINISH = "finish"
_REFINE = "refine"


# ---------------------------------------------------------------------------
# Generators: a random executed Stage 6 report with an explicit metrics block.
# ---------------------------------------------------------------------------

# Node identifiers used both as attack flow node ids and as the failed-node list entries.
_node_id_strategy = st.from_regex(r"T[0-9]{3,4}(\.[0-9]{3})?", fullmatch=True)

# A fraction in [0, 1] — the shape of both success_rate and procedural_coverage.
_fraction_strategy = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)


@st.composite
def _stage6_report(draw: st.DrawFn) -> dict[str, Any]:
    """Build a random executed Stage 6 report carrying an explicit metrics block.

    The report mirrors the idealized shape the evaluator reads from
    ``state["report"]``: a ``metrics`` sub-dict with ``success_rate``,
    ``procedural_coverage``, ``verdict_success`` and ``failed_nodes``. ``outcome``
    is ``"concluida"`` so this is an *executed* iteration (not a gate rejection),
    which is the case this property governs.
    """
    verdict_success = draw(st.booleans())
    success_rate = draw(_fraction_strategy)
    procedural_coverage = draw(_fraction_strategy)
    failed_nodes = draw(
        st.lists(_node_id_strategy, min_size=0, max_size=6, unique=True)
    )
    return {
        "outcome": "concluida",
        "metrics": {
            "success_rate": success_rate,
            "procedural_coverage": procedural_coverage,
            "verdict_success": verdict_success,
            "failed_nodes": failed_nodes,
        },
    }


# ---------------------------------------------------------------------------
# Ephemeral DB fixture — a fresh temp-file SQLite engine with the full ORM
# schema (Base.metadata.create_all), mirroring test_ai_op_origin_stamping_property.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward (no ``.db`` state leaks between examples).
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_evaluator_prop_")
    db_path = Path(tmp_dir) / "evaluator_prop.db"
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


def _seed_iteration(session: Session) -> int:
    """Create the FK chain campaigns → agent_runs → agent_iterations.

    Returns the ``agent_iterations.id`` an ``AgentEvaluation`` can hang off of so
    its non-null ``iteration_id`` FK is satisfiable on SQLite.
    """
    campaign = Case(
        id="prop-campaign",
        name="Property Campaign",
    )
    session.add(campaign)
    session.flush()

    run = AgentRun(
        campaign_id=campaign.id,
        status=AgentRunStatus.RUNNING,
        max_iterations=3,
        iterations_used=0,
        thread_id="prop-thread",
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    session.add(run)
    session.flush()

    iteration = AgentIteration(
        run_id=run.id,
        index=0,
        created_at=_utcnow(),
    )
    session.add(iteration)
    session.flush()
    return iteration.id


# ---------------------------------------------------------------------------
# Property 7 — decision consistency with metrics over executed Stage 6 reports.
#
# ``deadline=None``: each example builds and discards a temp-file SQLite DB,
# whose I/O may exceed Hypothesis' default deadline. The per-example DB is
# intentional (no state leak), so we disable the deadline rather than weaken the
# isolation.
# ---------------------------------------------------------------------------


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(report=_stage6_report())
def test_property7_evaluator_decision_consistency_with_metrics(
    report: dict[str, Any],
) -> None:
    """FINISH iff verdict_success; persisted metrics equal the report metrics.

    # Feature: ai-curation-agents, Property 7: Evaluator decision consistency
    # with metrics.
    Validates: Requirements 10.2, 10.3, 10.5
    """
    metrics = report["metrics"]
    verdict_success = metrics["verdict_success"]

    # The evaluator decides with NO LLM and NO RAG memory: memory=None and no
    # state["_session"], so the store-back is skipped (the decision never depends
    # on the RAG store being reachable — Req. 5.7) and the node is pure/offline.
    state: dict[str, Any] = {
        "campaign_id": "prop-campaign",
        "report": report,
        "retrieved_experience": [],
    }
    delta = node(state, memory=None)

    evaluation = delta["evaluation"]
    decision = evaluation["decision"]

    # (1) Decision consistency at the evaluator level (Req. 10.2, 10.3): FINISH
    # iff verdict_success is true, REFINE iff it is false. The max-iteration guard
    # is enforced downstream in route_after_eval (task 14.3), so on a failing
    # verdict the evaluator ALWAYS decides REFINE here — never FINISH/ABORTED.
    if verdict_success:
        assert decision == _FINISH, (
            f"verdict_success=True must decide FINISH, got {decision!r}"
        )
        # A passing verdict ends the run → no refine target is diagnosed.
        assert delta["refine_stage"] is None, (
            "FINISH must carry no refine_stage"
        )
    else:
        assert decision == _REFINE, (
            f"verdict_success=False must decide REFINE, got {decision!r}"
        )
        # A failing verdict routes back to a stage → a refine target is diagnosed.
        assert delta["refine_stage"] is not None, (
            "REFINE must diagnose a refine_stage"
        )

    # The decision is exactly one of the two enum values — never anything else.
    assert decision in {_FINISH, _REFINE}
    assert EvaluatorDecision(decision).value == decision

    # The evaluation carries the metrics read UNCHANGED from the report — the
    # node never recomputes or rescales the Stage 6 numbers (Req. 10.1, 10.5).
    assert evaluation["success_rate"] == metrics["success_rate"]
    assert evaluation["procedural_coverage"] == metrics["procedural_coverage"]
    assert evaluation["verdict_success"] == verdict_success
    assert evaluation["failed_nodes"] == [str(n) for n in metrics["failed_nodes"]]

    # (2) Persisted metrics equal the report metrics (Req. 10.5): persist the
    # evaluation through the Run_Service hook against a fresh ORM-built SQLite and
    # read the stored agent_evaluations row back, comparing every metric column to
    # the REPORT's metric block (not to the in-memory evaluation).
    session, db_path, engine = _build_ephemeral_session()
    try:
        iteration_id = _seed_iteration(session)
        row = persist_evaluation(session, iteration_id, evaluation)
        session.commit()

        stored_id = row.id
        session.expire_all()
        stored = session.get(type(row), stored_id)
        assert stored is not None

        # The stored metric columns equal the REPORT metrics (read unchanged).
        assert stored.success_rate == metrics["success_rate"]
        assert stored.procedural_coverage == metrics["procedural_coverage"]
        assert stored.verdict_success == verdict_success
        assert list(stored.failed_nodes or []) == [
            str(n) for n in metrics["failed_nodes"]
        ]
        # The persisted decision matches the node's decision (round-trips the
        # enum through the portable VARCHAR+CHECK column).
        assert stored.decision.value == decision
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
