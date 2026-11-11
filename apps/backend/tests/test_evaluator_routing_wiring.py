"""Example wiring test for the evaluator route (single route, recording, reaction).

Concrete, hand-built cases (no Hypothesis) that pin the evaluator's feedback
loop wiring described by Requirement 15:

* **Single Refinement_Route (15.1, 15.2).** An insufficient / failing Stage 6
  report fed to :func:`app.services.ai.agents.evaluator.node` yields a REFINE
  decision carrying EXACTLY ONE ``refine_stage`` (an
  :class:`~app.models.enums.AgentStage` value), never a set of competing
  targets — the evaluator detects the insufficiency of that iteration and picks
  one stage to refine.
* **One route maps to one stage agent (15.3, 15.4, 15.5).**
  :func:`app.services.ai.graph.route_after_eval` normalizes that single
  ``refine_stage`` to EXACTLY ONE of the three conditional-edge keys
  ``"dag"`` / ``"operationalize"`` / ``"environment"`` that ``build_graph``
  registered onto the DAG / operationalization / environment stage nodes — so a
  dag route re-enters the DAG agent (15.3), an op route the operationalization
  agent (15.4), and an environment route the environment agent (15.5). The three
  mappings are covered with reports crafted so ``_diagnose_failing_stage`` picks
  each stage (isolation outcome -> environment; containment/confirmation block or
  failing executed nodes -> operationalization) plus a direct ``stage_3_dag``
  state for the dag mapping, which ``_diagnose_failing_stage`` does not produce
  naturally (it only ever routes to environment or operationalization), so the
  dag edge key is asserted at the ``route_after_eval`` level.
* **Each iteration records refined stage + non-empty reason (15.6).** Driving
  :class:`app.services.ai.run_service.AgentRunService` over an ephemeral SQLite
  session with a doubled compiled graph whose final state carries an evaluator
  ``rationale`` and a ``refine_stage`` writes an ``agent_iterations`` row whose
  ``refined_stage`` is populated and whose ``notes`` (the refinement reason) is
  non-empty.

Everything runs on an ephemeral temp-file SQLite built straight from the ORM and
a scripted fake graph — no SEPINF endpoint, no PostgreSQL, no LangGraph, no
network. The doubled-graph harness mirrors ``tests/test_run_service_lifecycle.py``.

_Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6_
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AgentIteration
from app.models.enums import AgentRunStatus, AgentStage, EvaluatorDecision
from app.services.ai.agents import evaluator
from app.services.ai.graph import route_after_eval
from app.services.ai.run_service import AgentRunService

_CAMPAIGN_ID = "campaign-under-test"

# The three conditional-edge keys ``build_graph`` registered onto the DAG /
# operationalization / environment stage nodes (Req. 15.3, 15.4, 15.5). "finish"
# maps to END and is NOT a refine target.
_REFINE_EDGE_KEYS = {"dag", "operationalize", "environment"}


# ---------------------------------------------------------------------------
# Crafted Stage 6 reports — each shaped so ``_diagnose_failing_stage`` routes to
# a specific stage (the evaluator reads the serialized runner ``outcome`` and the
# failing executed nodes). None of these pass verdict, so each is a failing /
# insufficient iteration eligible for refinement (Req. 15.1).
# ---------------------------------------------------------------------------


def _isolation_failure_report() -> dict:
    """A gate rejection on isolation -> the evaluator refines the ENVIRONMENT stage."""
    return {
        "outcome": "isolamento_falhou",
        "message": "contêiner não isolado",
    }


def _containment_block_report() -> dict:
    """A containment gate rejection -> the evaluator refines OPERATIONALIZATION."""
    return {
        "outcome": "contencao_recusada",
        "message": "destino externo detectado",
    }


def _failing_nodes_report() -> dict:
    """An executed run whose nodes failed -> the evaluator refines OPERATIONALIZATION."""
    return {
        "outcome": "concluida",
        "verdict_success": False,
        "total_success": 0,
        "total_failure": 2,
        "ability_results": [
            {"ability_id": "ability-1", "status": "failure"},
            {"ability_id": "ability-2", "status": "failure"},
        ],
    }


# ===========================================================================
# Case 1 — an insufficient report yields EXACTLY ONE Refinement_Route, and
# route_after_eval maps it to EXACTLY ONE stage edge key (Req. 15.1-15.5).
# ===========================================================================


def _route_state(refine_stage: str | None, evaluation: dict) -> dict:
    """A below-guard state so ``route_after_eval`` routes to the refine target.

    ``iteration + 1 < max_iterations`` keeps the max-iteration guard from turning
    the REFINE into a "finish", so the router returns the refine edge key for the
    evaluator's chosen stage (Req. 15.2-15.5).
    """
    return {
        "evaluation": evaluation,
        "refine_stage": refine_stage,
        "iteration": 0,
        "max_iterations": 3,
    }


@pytest.mark.parametrize(
    ("report", "expected_stage", "expected_edge_key"),
    [
        # Isolation gate rejection -> environment stage -> "environment" (15.5).
        (
            _isolation_failure_report(),
            AgentStage.ENVIRONMENT.value,
            "environment",
        ),
        # Containment/confirmation block -> operationalization -> "operationalize" (15.4).
        (
            _containment_block_report(),
            AgentStage.OPERATIONALIZATION.value,
            "operationalize",
        ),
        # Failing executed nodes -> operationalization -> "operationalize" (15.4).
        (
            _failing_nodes_report(),
            AgentStage.OPERATIONALIZATION.value,
            "operationalize",
        ),
    ],
)
def test_insufficient_report_yields_single_route_mapped_to_one_stage(
    report: dict, expected_stage: str, expected_edge_key: str
) -> None:
    """A failing report -> exactly one refine_stage -> exactly one stage edge key.

    The evaluator detects the iteration's insufficiency and picks a single stage
    to refine (Req. 15.1, 15.2); ``route_after_eval`` then normalizes that single
    target to exactly one of the three conditional-edge keys the stage agents are
    wired to, so the loop re-enters precisely one stage agent (Req. 15.3-15.5).
    """
    # The evaluator is given no RAG memory (store-back is skipped) and no session.
    delta = evaluator.node({"report": report, "campaign_id": _CAMPAIGN_ID})

    # Exactly one Refinement_Route: a single AgentStage value, not a collection
    # of competing targets (Req. 15.1, 15.2).
    refine_stage = delta["refine_stage"]
    assert refine_stage == expected_stage
    assert AgentStage(refine_stage) is AgentStage(expected_stage)
    assert delta["evaluation"]["decision"] == EvaluatorDecision.REFINE.value

    # route_after_eval maps that single stage to exactly one refine edge key —
    # one of {"dag","operationalize","environment"}, never "finish" below the
    # guard (Req. 15.3, 15.4, 15.5).
    edge_key = route_after_eval(_route_state(refine_stage, delta["evaluation"]))
    assert edge_key == expected_edge_key
    assert edge_key in _REFINE_EDGE_KEYS
    assert edge_key != "finish"


def test_dag_route_maps_to_dag_stage_agent() -> None:
    """A dag-targeted refinement routes to the DAG stage agent edge key (Req. 15.3).

    ``_diagnose_failing_stage`` only ever routes to environment or
    operationalization (never dag) from a report, so the dag mapping is asserted
    at the ``route_after_eval`` level by feeding a state whose ``refine_stage`` is
    the DAG stage value — exactly what the evaluator would write had it chosen the
    DAG stage. The router must normalize it to the single ``"dag"`` edge key that
    ``build_graph`` wired to the Dag_Generation_Agent.
    """
    evaluation = {
        "verdict_success": False,
        "decision": EvaluatorDecision.REFINE.value,
    }
    edge_key = route_after_eval(
        _route_state(AgentStage.DAG.value, evaluation)
    )

    assert edge_key == "dag"
    assert edge_key in _REFINE_EDGE_KEYS


# ===========================================================================
# Case 2 — each refining iteration records the refined stage + a non-empty
# reason on its agent_iterations row (Req. 15.6).
# ===========================================================================


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Mirrors the ephemeral-session harness in ``test_run_service_lifecycle.py``:
    the schema is built straight from the ORM so the AI bookkeeping tables
    round-trip without a migration, and SQLite does not enforce FKs by default so
    no Campaign row is required under the arbitrary ``campaign_id``.
    """
    tmp_dir = tempfile.mkdtemp(prefix="evaluator_routing_wiring_")
    db_path = Path(tmp_dir) / "evaluator_routing.db"
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


class _FakeGraph:
    """Stand-in compiled graph exposing only ``invoke(state, config=...)``.

    Returns a controlled final ``AgentState`` dict so the run-service lifecycle
    under test is the persistence of the completed iteration, not the graph.
    """

    def __init__(self, *, final_state: dict) -> None:
        self._final_state = final_state

    def invoke(self, state: Any, config: dict | None = None) -> dict:
        return dict(self._final_state)


def _install_fake_graph(
    monkeypatch: pytest.MonkeyPatch, graph: _FakeGraph
) -> None:
    """Patch ``_build_graph`` so the service invokes ``graph`` and probes pass."""
    monkeypatch.setattr(
        AgentRunService, "_build_graph", lambda self: graph, raising=True
    )
    monkeypatch.setattr(
        AgentRunService, "_require_dependencies", lambda self: None, raising=True
    )


def _refining_final_state() -> dict:
    """A final state for a refining iteration that stopped at the guard.

    Carries a failing verdict (so the run reads as ABORTED), a single
    ``refine_stage`` (the operationalization stage value the evaluator would
    write) and an evaluator ``rationale`` — the Refinement_Route reason the
    iteration row must record (Req. 15.6). ``max_iterations=1`` makes the guard
    stop after exactly one completed pass, so one ``agent_iterations`` row at
    index 0 is persisted. No ``artifacts`` are present, so no stage-output rows
    are attempted — the test isolates the iteration-row recording.
    """
    return {
        "campaign_id": _CAMPAIGN_ID,
        "iteration": 0,
        "max_iterations": 1,
        "refine_stage": AgentStage.OPERATIONALIZATION.value,
        "evaluation": {
            "verdict_success": False,
            "decision": EvaluatorDecision.REFINE.value,
            "rationale": (
                "Decisão REFINAR. Alvo: stage_4_operationalization. Motivo: nós "
                "executados falharam em cumprir a técnica; refinar o Estágio 4."
            ),
        },
        "artifacts": {},
        "history": [],
    }


def test_iteration_records_refined_stage_and_non_empty_notes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each refining iteration writes refined_stage + a non-empty reason (Req. 15.6).

    Drives the run-service persistence over the ephemeral session with a doubled
    graph whose final state carries the evaluator's ``rationale`` and a single
    ``refine_stage``; the persisted ``agent_iterations`` row must have
    ``refined_stage`` set to the refined stage and ``notes`` (the refinement
    reason) non-empty.
    """
    _install_fake_graph(monkeypatch, _FakeGraph(final_state=_refining_final_state()))

    run = AgentRunService(session).start(_CAMPAIGN_ID, max_iterations=1)

    # The guard stopped the loop without a passing verdict -> ABORTED, one pass.
    assert run.status is AgentRunStatus.ABORTED
    assert run.iterations_used == 1

    rows = session.execute(select(AgentIteration)).scalars().all()
    assert len(rows) == 1, "exactly one agent_iterations row for the single pass"
    iteration = rows[0]

    # The refined stage is recorded (Req. 15.6): the evaluator's single
    # refine_stage mapped onto the AgentStage member.
    assert iteration.refined_stage is AgentStage.OPERATIONALIZATION

    # The refinement reason is recorded and non-empty (Req. 15.6): the evaluator
    # rationale carried on the iteration's notes.
    assert iteration.notes is not None
    assert iteration.notes.strip() != ""
    assert "REFINAR" in iteration.notes
