"""Property test for origin stamping on Stage 4 AI artifacts (task 7.4).

# Feature: ai-curation-agents, Property 1: AI artifacts always carry
# origin = AI_GENERATED — for every artifact the Stage 4 Operationalization_Agent
# (:class:`~app.services.ai.agents.op_agent.AiOperationalizer`) produces, the
# stamped Origin equals ``"gerado_ia"`` (:attr:`TranslationSource.AI_GENERATED`)
# on BOTH the :class:`ProducedOperationalization` and the derived
# :class:`EnvironmentParameters`, and persisting that artifact requires no schema
# migration for the Origin value.

Validates: Requirements 2.4, 2.5

This is a *feature* property test (``ai-curation-agents`` is a feature spec, not
a bugfix): the property is expected to **hold** for the current definition of
:class:`~app.services.ai.agents.op_agent.AiOperationalizer` together with the
existing :class:`~app.models.enums.TranslationSource` enum (whose
``AI_GENERATED = "gerado_ia"`` member is already declared and already accepted by
the portable VARCHAR + CHECK column constraint).

What the property guarantees
----------------------------
* **Origin stamping (Req. 2.4):** for ANY Stage 3 ``DagView`` and ANY Atomic Red
  Team command pool, the ``AiOperationalizer`` stamps the exact string
  ``"gerado_ia"`` on both artifacts it emits — ``ProducedOperationalization.origin``
  and the derived ``EnvironmentParameters.origin`` — regardless of whether the
  DAG yields an operationalizable technique (so the invariant holds even on the
  suppressed-abilities path).
* **No migration for the Origin (Req. 2.5):** persisting the derived
  ``EnvironmentParameters`` with ``origin = "gerado_ia"`` is accepted by the
  schema the ORM builds from the existing models — the portable CHECK on
  ``environment_parameters.origin`` (``translation_source``, ``native_enum=False``)
  already admits the value. The schema for each example is built straight from the
  ORM (``Base.metadata.create_all``), NOT from Alembic, so a clean round-trip
  proves no new migration is required for the Origin.

How the property is driven
--------------------------
Hypothesis generates an arbitrary ``DagView`` (campaign slug + a list of nodes,
each carrying a ``technique_id``/``technique_name``/``tactic``) together with a
**fake** ``atomic_lookup`` command pool keyed by technique id. The fake lookup is
a pure in-memory callable, so the agent runs with **no disk and no network** (no
Atomic Red Team files are read, no SEPINF endpoint is contacted) and with **no
LLM** injected (the agent degrades to its deterministic selection). The agent's
``operationalize(campaign, dag)`` is invoked and the property asserts that:

1. ``produced.origin is TranslationSource.AI_GENERATED`` and its value is the
   exact string ``"gerado_ia"``;
2. ``produced.parameters.origin is TranslationSource.AI_GENERATED`` and its value
   is ``"gerado_ia"``; and
3. serializing that ``EnvironmentParameters`` and persisting it into a fresh
   temp-file SQLite database built by the ORM is accepted, and the raw value
   written to disk for ``origin`` is exactly ``"gerado_ia"`` (the portable CHECK
   accepted it with no schema change).

The assertions never trust the implementation to decide the expected outcome:
the expected string ``"gerado_ia"`` is a literal. Each example builds and
discards its own database (no ``.db`` state leaks), offline, with no
network/Docker.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Case, EnvironmentParametersRow, _utcnow
from app.models.enums import TranslationSource
from app.services.ai.agents.op_agent import AiOperationalizer
from app.services.dag.dag_service import DagNodeView, DagView

# The exact pt-BR string the Origin must carry (Req. 2.4) — a literal, so the
# assertions never trust the implementation to decide the expected value.
_GERADO_IA = "ai_generated"


# ---------------------------------------------------------------------------
# Generators: an arbitrary Stage 3 DagView + a fake Atomic Red Team command pool.
# ---------------------------------------------------------------------------

_slug_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=16
)

# ATT&CK-style technique ids (e.g. ``T1059`` / ``T1059.001``).
_technique_id_strategy = st.from_regex(r"T[0-9]{3,4}(\.[0-9]{3})?", fullmatch=True)

_node_strategy = st.builds(
    DagNodeView,
    node_id=_slug_strategy,
    technique_id=st.one_of(st.none(), _technique_id_strategy),
    technique_name=st.one_of(st.none(), st.text(max_size=24)),
    tactic=st.one_of(st.none(), st.text(max_size=16)),
)

# A single Atomic Red Team pool entry, in the shape ``tool_atomic_lookup`` yields
# and ``AiOperationalizer`` consumes (``name``/``platform``/``command`` + the
# optional ``ability_id``/``technique_name``/``tactic`` it may read).
_command_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789 -./", min_size=1, max_size=40
)
_pool_entry_strategy = st.fixed_dictionaries(
    {
        "ability_id": _slug_strategy,
        "name": st.text(max_size=20),
        "platform": st.sampled_from(["sh", "bash", "powershell", "cmd"]),
        "command": _command_strategy,
        "technique_name": st.text(max_size=20),
        "tactic": st.text(max_size=16),
    }
)


@st.composite
def _dag_and_lookup(draw: st.DrawFn) -> tuple[DagView, Callable[[str], list[dict]]]:
    """Draw an arbitrary ``DagView`` and a fake in-memory ``atomic_lookup``.

    The fake lookup maps each node's ``technique_id`` to a (possibly empty)
    command pool, so the agent runs purely in memory — no Atomic Red Team file is
    read and no network call is made. Some techniques intentionally map to an
    empty pool, exercising the suppressed-abilities path while the Origin
    invariant must still hold on the derived parameters.
    """
    campaign = draw(_slug_strategy)
    nodes = draw(st.lists(_node_strategy, max_size=5))
    dag = DagView(
        campaign_id=campaign,
        name=campaign,
        curated=True,
        can_advance=True,
        nodes=nodes,
    )

    # Build a per-technique command pool. Each present technique id gets a pool
    # drawn independently (possibly empty, exercising the no-command path).
    pools: dict[str, list[dict]] = {}
    for node in nodes:
        if node.technique_id and node.technique_id not in pools:
            pools[node.technique_id] = draw(
                st.lists(_pool_entry_strategy, max_size=3)
            )

    def fake_atomic_lookup(technique_id: str) -> list[dict]:
        # Pure, in-memory: no disk, no network (Req.: avoid disk/network).
        return list(pools.get(technique_id, []))

    return dag, fake_atomic_lookup


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
#
# Mirrors test_origin_persisted_extensible_property.py: the schema is built with
# the ORM (``Base.metadata.create_all``), NOT Alembic — so persisting
# ``origin = gerado_ia`` succeeds WITHOUT any new migration, because the portable
# CHECK (``native_enum=False``) on ``environment_parameters.origin`` already
# includes the value.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward (no ``.db`` state leaks between examples).
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_ai_op_origin_prop_")
    db_path = Path(tmp_dir) / "ai_op_origin_prop.db"
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
# Property 1 — for any DagView + any Atomic Red Team pool, the AiOperationalizer
# stamps origin=gerado_ia on BOTH artifacts, and persisting the derived
# EnvironmentParameters needs no schema migration for the Origin (Req. 2.4, 2.5).
#
# ``deadline=None``: each example builds and discards a temp-file SQLite DB,
# whose I/O may exceed Hypothesis' default deadline. The per-example DB is
# intentional (no state leak), so we disable the deadline rather than weaken the
# isolation.
# ---------------------------------------------------------------------------
# Feature: ai-curation-agents, Property 1: AI artifacts always carry
# origin = AI_GENERATED.
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(data=_dag_and_lookup())
def test_property1_ai_stage4_artifacts_carry_gerado_ia_origin(
    data: tuple[DagView, Callable[[str], list[dict]]],
) -> None:
    """AI Stage 4 artifacts carry origin=gerado_ia and persist with no migration.

    # Feature: ai-curation-agents, Property 1: AI artifacts always carry
    # origin = AI_GENERATED.
    Validates: Requirements 2.4, 2.5
    """
    dag, fake_atomic_lookup = data

    # No LLM and no RAG memory injected: the agent degrades to its deterministic
    # selection and proposes without priming, so it runs with no SEPINF endpoint.
    # The fake atomic_lookup keeps the command source purely in memory.
    agent = AiOperationalizer(atomic_lookup=fake_atomic_lookup)

    # Sanity: the agent's declared origin is AI_GENERATED (Req. 2.4).
    assert agent.origin() is TranslationSource.AI_GENERATED

    produced = agent.operationalize(dag.campaign_id, dag)

    # (1) ProducedOperationalization is stamped gerado_ia (Req. 2.4).
    assert produced.origin is TranslationSource.AI_GENERATED
    assert produced.origin.value == _GERADO_IA

    # (2) The derived EnvironmentParameters is stamped gerado_ia (Req. 2.4) —
    # this holds even when no technique was operationalizable (empty pools), i.e.
    # on the suppressed-abilities path.
    parameters = produced.parameters
    assert parameters.origin is TranslationSource.AI_GENERATED
    assert parameters.origin.value == _GERADO_IA

    # (3) Persisting the derived parameters requires no schema migration for the
    # Origin: a fresh ORM-built SQLite accepts origin=gerado_ia and round-trips
    # the exact pt-BR string to disk (Req. 2.5).
    session, db_path, engine = _build_ephemeral_session()
    try:
        # FK campaign_id -> campaigns.id must be satisfied.
        session.add(Case(id=dag.campaign_id, name=dag.campaign_id))
        session.commit()

        row = EnvironmentParametersRow(
            campaign_id=dag.campaign_id,
            platform=parameters.platform,
            content=parameters.model_dump(mode="json"),
            origin=parameters.origin,  # TranslationSource.AI_GENERATED
            created_at=_utcnow(),
        )
        session.add(row)
        session.commit()
        row_id = row.id

        # Raw value on disk is exactly "gerado_ia" — the portable CHECK accepted
        # it with no schema change (Req. 2.5).
        with engine.connect() as conn:
            stored_origin = conn.execute(
                text("SELECT origin FROM environment_parameters WHERE id = :id"),
                {"id": row_id},
            ).scalar_one()
        assert stored_origin == _GERADO_IA

        # Round-trip via the ORM confirms the persisted origin is AI_GENERATED.
        session.expunge_all()
        loaded = session.get(EnvironmentParametersRow, row_id)
        assert loaded is not None
        assert loaded.origin is TranslationSource.AI_GENERATED
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
