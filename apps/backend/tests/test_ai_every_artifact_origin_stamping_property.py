"""Property test for every-artifact origin stamping on a Run (task 3.5).

Feature: ai-first-campaign-flow, Property 5: Every AI-produced artifact carries
the AI-generated origin — for ANY Run, every persisted ``agent_stage_outputs``
row carries ``origin == "ai_generated"``.

Validates: Requirements 3.2

This is a *feature* property test (``ai-first-campaign-flow`` is a feature spec,
not a bugfix): the property is expected to **hold** for the current pipeline. It
drives the real
:class:`~app.services.ai.run_service.AgentRunService` persistence path end to end
with **fakes only** — no SEPINF endpoint, no PostgreSQL, no real LangGraph. The
compiled graph is replaced by a fake whose ``invoke`` returns a controlled final
``AgentState`` carrying a Hypothesis-varied set of stage artifacts (DAG,
operationalization, environment) across a Hypothesis-varied iteration count, the
checkpointer / pgvector availability probe is patched to a no-op, and persistence
lands in an **ephemeral per-test temp-file SQLite** built straight from the ORM
(``Base.metadata.create_all``) so the AI bookkeeping rows round-trip without a
migration. This mirrors the conventions of
``test_run_service_lifecycle.py`` and ``test_ai_op_origin_stamping_property.py``.

What the property guarantees
----------------------------
For ANY Run — ANY combination of present stage artifacts (DAG and/or
operationalization and/or environment, in any mix including all three) and ANY
iteration count within the guard — once the Run has persisted its completed
iteration, EVERY row in ``agent_stage_outputs`` the Run wrote carries the raw
origin value exactly ``"ai_generated"`` (:attr:`TranslationSource.AI_GENERATED`).
The assertion reads the raw ``origin`` column straight off the database (not the
ORM's re-hydrated enum), so it proves what was actually persisted, and the
expected string ``"ai_generated"`` is a literal — the assertion never trusts the
implementation to decide the expected value.

How the property is driven
---------------------------
Hypothesis draws:

* which of the three stage artifacts are present (at least one, so the Run always
  writes at least one ``agent_stage_outputs`` row to assert on);
* the ``max_iterations`` guard and whether the final verdict passes (so the Run
  terminates COMPLETED or ABORTED) — the persisted iteration is the last one
  regardless, and the origin invariant must hold on it.

The DAG and operationalization artifacts are real artifacts: the
operationalization is produced by the real deterministic
:class:`~app.services.ai.agents.op_agent.AiOperationalizer` (no LLM, no network,
a fake in-memory Atomic Red Team pool), and the DAG is a real
:class:`~app.services.dag.generator.ProducedDag` stamped with the AI DAG origin.
The environment artifact is an opaque JSON payload (the env persistence hook
treats ``artifacts.environment`` as an opaque JSON dict). Every example builds and
discards its own temp-file SQLite database — offline, with no network/Docker.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.enums import DagOrigin
from app.services.ai.agents.op_agent import AiOperationalizer
from app.services.ai.run_service import AgentRunService
from app.services.dag.dag_service import DagNodeView, DagView
from app.services.dag.generator import ProducedDag

# The exact string the Origin must carry (Req. 3.2) — a literal, so the
# assertion never trusts the implementation to decide the expected value.
_AI_GENERATED = "ai_generated"

_CAMPAIGN_ID = "campaign-under-test"


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
#
# Mirrors test_run_service_lifecycle.py / test_ai_op_origin_stamping_property.py:
# the schema is built straight from the ORM (``Base.metadata.create_all``), so
# the AI bookkeeping tables round-trip without a migration. SQLite does not
# enforce FKs by default, so no Campaign row is required to persist a Run.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward (no ``.db`` state leaks between examples).
    """
    tmp_dir = tempfile.mkdtemp(prefix="ai_every_artifact_origin_prop_")
    db_path = Path(tmp_dir) / "every_artifact_origin.db"
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
# Fake compiled graph — the single ``invoke`` surface the service uses.
# ---------------------------------------------------------------------------


class _FakeGraph:
    """Stand-in compiled graph exposing only ``invoke(state, config=...)``.

    Returns a controlled final ``AgentState`` dict so the Run persists the
    iteration whose artifacts Hypothesis drew, without a real LangGraph, SEPINF
    endpoint or checkpointer.
    """

    def __init__(self, *, final_state: dict) -> None:
        self._final_state = final_state

    def invoke(self, _state: Any, config: dict | None = None) -> dict:
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


# ---------------------------------------------------------------------------
# Generators — a real DAG artifact, a real operationalization artifact and an
# opaque environment artifact, in the serialized shape the final state carries.
# ---------------------------------------------------------------------------

_slug_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=16
)
_technique_id_strategy = st.from_regex(r"T[0-9]{3,4}(\.[0-9]{3})?", fullmatch=True)


def _produced_dag_payload(campaign: str) -> dict:
    """A real AI-origin ``ProducedDag`` serialized for ``artifacts.dag``.

    ``run_service`` reconstructs this via ``ProducedDag.model_validate`` and
    persists it via ``dag_agent.persist_dag_stage_output``, which stamps the
    ``agent_stage_outputs`` row ``origin=ai_generated``.
    """
    produced = ProducedDag(
        origin=DagOrigin.AI_GENERATED,
        identifier=campaign,
        label=campaign,
        content={"structural_nodes": [], "arestas": []},
    )
    return produced.model_dump()


@st.composite
def _operationalization_payload(draw: st.DrawFn, campaign: str) -> dict:
    """A real ``ProducedOperationalization`` serialized for the artifact slot.

    Produced by the real deterministic ``AiOperationalizer`` (no LLM, no network:
    a fake in-memory Atomic Red Team pool) from a drawn ``DagView``. The run
    service reconstructs it via ``ProducedOperationalization.model_validate`` and
    persists it via ``op_agent.persist_stage_output`` (which also persists the
    derived ``EnvironmentParameters`` and stamps the stage-output row
    ``origin=ai_generated``).
    """
    nodes = draw(
        st.lists(
            st.builds(
                DagNodeView,
                node_id=_slug_strategy,
                technique_id=st.one_of(st.none(), _technique_id_strategy),
                technique_name=st.one_of(st.none(), st.text(max_size=16)),
                tactic=st.one_of(st.none(), st.text(max_size=12)),
            ),
            max_size=4,
        )
    )
    dag = DagView(
        campaign_id=campaign,
        name=campaign,
        curated=True,
        can_advance=True,
        nodes=nodes,
    )
    # No LLM / no RAG memory injected and a fake empty atomic pool: the agent
    # degrades to its deterministic selection and runs purely in memory.
    agent = AiOperationalizer(atomic_lookup=lambda _tid: [])
    produced = agent.operationalize(campaign, dag)
    return produced.model_dump(mode="json")


def _environment_payload(campaign: str) -> dict:
    """An opaque JSON environment artifact for ``artifacts.environment``.

    The env persistence hook (``env_agent.persist_stage_output``) treats
    ``artifacts.environment`` as an opaque JSON dict payload and stamps the
    ``agent_stage_outputs`` row ``origin=ai_generated`` itself, so any JSON dict
    exercises the stamping path.
    """
    return {
        "campaign_id": campaign,
        "platform": "container",
        "target_containers": [],
        "origin": "ai_generated",
    }


@st.composite
def _run_final_state(draw: st.DrawFn) -> tuple[dict, int, bool]:
    """Draw a final ``AgentState`` with a varied set of present stage artifacts.

    Returns ``(final_state, max_iterations, verdict_success)``. At least one of
    the three stage artifacts is present so the Run always writes at least one
    ``agent_stage_outputs`` row to assert on.
    """
    include_dag = draw(st.booleans())
    include_op = draw(st.booleans())
    include_env = draw(st.booleans())
    # Guarantee at least one artifact is present.
    if not (include_dag or include_op or include_env):
        include_dag = True

    artifacts: dict[str, Any] = {}
    if include_dag:
        artifacts["dag"] = _produced_dag_payload(_CAMPAIGN_ID)
    if include_op:
        artifacts["operationalization"] = draw(
            _operationalization_payload(_CAMPAIGN_ID)
        )
    if include_env:
        # The env persistence hook requires the operationalization-derived
        # params linkage only when present; the hook itself reads the opaque
        # payload, so an environment artifact may stand alone.
        artifacts["environment"] = _environment_payload(_CAMPAIGN_ID)

    max_iterations = draw(st.integers(min_value=1, max_value=5))
    iteration = draw(st.integers(min_value=0, max_value=max_iterations - 1))
    verdict_success = draw(st.booleans())

    final_state = {
        "campaign_id": _CAMPAIGN_ID,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "evaluation": {"verdict_success": verdict_success},
        "artifacts": artifacts,
        "history": [],
    }
    return final_state, max_iterations, verdict_success


# ---------------------------------------------------------------------------
# Property 5 — for ANY Run, every persisted agent_stage_outputs row has
# origin == "ai_generated" (Req. 3.2).
#
# ``deadline=None``: each example builds and discards a temp-file SQLite DB and
# runs the real deterministic operationalizer, whose work may exceed Hypothesis'
# default deadline. The per-example DB is intentional (no state leak), so the
# deadline is disabled rather than weakening the isolation.
# ---------------------------------------------------------------------------
# Feature: ai-first-campaign-flow, Property 5: Every AI-produced artifact carries
# the AI-generated origin.
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(data=_run_final_state())
def test_property5_every_persisted_stage_output_is_ai_generated(
    data: tuple[dict, int, bool],
) -> None:
    """Every persisted ``agent_stage_outputs`` row carries origin=ai_generated.

    Feature: ai-first-campaign-flow, Property 5: Every AI-produced artifact
    carries the AI-generated origin.
    Validates: Requirements 3.2
    """
    final_state, max_iterations, _verdict_success = data

    session, db_path, engine = _build_ephemeral_session()
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_fake_graph(monkeypatch, _FakeGraph(final_state=final_state))

        run = AgentRunService(session).start(
            _CAMPAIGN_ID, max_iterations=max_iterations
        )

        # The Run persisted its completed iteration; read the raw origin value of
        # every agent_stage_outputs row straight off the database (not the ORM's
        # re-hydrated enum) so the assertion proves what was actually persisted.
        # The read goes through the service's own Session so it sees the rows the
        # Run flushed within its still-open unit of work (``start`` flushes but
        # does not commit — the transaction boundary belongs to the caller).
        origins = (
            session.execute(text("SELECT origin FROM agent_stage_outputs"))
            .scalars()
            .all()
        )

        # The Run always carries at least one artifact, so it always wrote at
        # least one stage-output row to assert on.
        assert origins, (
            "expected the Run to persist at least one agent_stage_outputs row "
            f"(run id={run.id})"
        )
        # EVERY persisted row carries the AI-generated origin (Req. 3.2).
        assert all(origin == _AI_GENERATED for origin in origins), (
            "every persisted agent_stage_outputs row must carry "
            f"origin == {_AI_GENERATED!r}; got {sorted(set(origins))!r}"
        )
    finally:
        monkeypatch.undo()
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
