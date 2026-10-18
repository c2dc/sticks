"""Property test for the automatic completion of Stage 3 — DAG part (task 4.1).

# Feature: pipeline-seis-stages, Property 9: Automatic completion of the
# curation Stages. For a curated campaign, as soon as Stage 2 (Campaign
# Selection) completes and there is a derivable curated DAG, Stage 3 (DAG
# Generation) auto-completes by loading the DAG. It respects the sequential gate
# and does not occur without the curated material (DAG absent).

Validates: Requirements 9.1, 9.3, 9.4

This is a *feature* property test (the ``pipeline-cinco-stages`` spec is a
feature, not a bugfix): the property is expected to **hold** against the current
implementation of
:meth:`~app.services.dag.dag_service.DagService.auto_complete_stage2`.

Property (design.md — Property 9, DAG part)
-------------------------------------------
Drives the gate, the idempotent auto-completion and the non-completion path of
Stage 3 over an **arbitrary** distribution of a campaign's ``StageRun`` states.
The Stage 2 (Campaign Selection) state is seeded directly as a ``StageRun`` row
to isolate the gate deterministically; the DAG derivation uses the **real**
:class:`CurationDagGenerator` over a ``{slug}_dag.json`` written to a temporary
data dir. No path touches the network nor an AI client (there is none on this
path) — everything is pure DB logic over ephemeral SQLite.

1. **Gate (Req. 9.3).** For any distribution of states in which Stage 2 is
   **not** ``completed``, ``auto_complete_stage2`` returns ``completed=False`` /
   ``blocked_by_gate=True`` and does **not** persist any completed ``stage=3``
   row — regardless of whether a curated DAG exists.
2. **Auto-completion (Req. 9.1).** When Stage 2 is ``completed`` and there is a
   derivable curated DAG, Stage 3 auto-completes by loading the DAG: the
   ``StageRun`` of ``(campaign, stage=3)`` becomes ``completed``/100; and it is
   **idempotent** — calling ``N`` times (``N`` driven by Hypothesis) produces
   exactly the same single row (same ``id``, no accumulation), and the backfill
   does not duplicate the ``Dag`` row (a single curated entity).
3. **No DAG (Req. 9.4).** With Stage 2 ``completed`` but **no** derivable curated
   DAG (data dir without ``{slug}_dag.json``), ``auto_complete_stage2`` returns
   ``no_dag=True``, does not complete, the advance to Stage 4 stays blocked, and
   the backfill is idempotent (no ``Dag`` row is persisted).

Strategy / isolation
--------------------
Each example points a :class:`CaseService` at a **temporary** data directory
with the ``<data_dir>/{api,dag}/`` layout (same as the real one), into which we
write (or not) a generated ``{slug}_dag.json``, and builds a ``DagService`` over
an **ephemeral** SQLite ``Session`` (engine on a temporary file with the full
schema via ``Base.metadata.create_all``, same as the
``test_backfill_idempotent_property.py`` / ``test_dag_choice_property.py``
fixtures). The ``DagService`` receives the real ``CurationDagGenerator`` over
the same ``CaseService``; ``_get_modeling`` materializes the campaign attributes
from the same fixture. The generated DAGs are acyclic by construction.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Dag as DagModel
from app.models.domain import StageRun
from app.models.enums import DagOrigin, StageState
from app.services.campaign_service import CURATED_CASES, CaseService
from app.services.dag.dag_service import DagService
from app.services.dag.generator import CurationDagGenerator
from app.services.stage.stage_service import (
    STAGE_DAG_GENERATION,
    STAGE_CAMPAIGN_SELECTION,
    VALID_STAGES,
    StageService,
)

# A real curated campaign slug: curation generation only produces a DAG for
# curated slugs (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guard: keep the curated-fixture slug


# ---------------------------------------------------------------------------
# Ephemeral DB fixture (same as test_dag_choice_property.py).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline5_dag_auto_prop_")
    db_path = Path(tmp_dir) / "dag_auto.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
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
    return factory, tmp_dir


def _rmtree(base: Path) -> None:
    """Remove a directory recursively (temporary-dir teardown)."""
    for root, _dirs, files in os.walk(base, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        os.rmdir(root)


# ---------------------------------------------------------------------------
# Fixture builder — writes a generated {slug}_dag.json into a temporary data dir
# with the <data_dir>/{api,dag}/ layout, same as the real one. No network/AI.
# ---------------------------------------------------------------------------


def _write_case_fixture(
    data_dir: Path,
    slug: str,
    structural_nodes: list[dict],
    *,
    com_dag: bool = True,
) -> None:
    """Write the ability fixtures (+ DAG, if ``com_dag``) for ``slug``.

    With ``com_dag=False`` it does NOT write ``{slug}_dag.json`` — modeling a
    curated campaign with no derivable DAG (``try_load_case`` fails and the
    generator returns an empty list). The empty ability is always written
    (consistent fixture environment).
    """
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

    if not com_dag:
        return

    dag_payload = {
        "campaign_name": slug,
        "metadata": {"validation": {"is_dag": True}},
        "structural_nodes": structural_nodes,
    }
    (dag_dir / f"{slug}_dag.json").write_text(
        json.dumps(dag_payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Hypothesis strategy — an acyclic DAG via child_nodes (earlier->later).
# ---------------------------------------------------------------------------


@st.composite
def _acyclic_dag(draw) -> list[dict]:
    """Draw the ``structural_nodes`` of an acyclic DAG."""
    n = draw(st.integers(min_value=1, max_value=8))
    node_ids = [f"node-{i:02d}" for i in range(n)]

    children: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.booleans()):
                children[node_ids[i]].append(node_ids[j])

    return [
        {
            "node_id": node_ids[i],
            "node_index": i,
            "technique_id": f"T{1000 + i}",
            "technique_name": f"Technique {i}",
            "tactic": f"tactic-{i}",
            "child_nodes": children[node_ids[i]],
            "parent_nodes": [],
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Seeding of Stage states and helper queries.
# ---------------------------------------------------------------------------

# Possible states of a Stage, plus ``None`` to model "no row" (which the service
# treats as ``nao_iniciado``).
_STATE_CHOICES: tuple[Optional[StageState], ...] = (
    None,
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

# Stage states that are NOT ``completed`` (to drive the closed gate).
_NON_COMPLETED_CHOICES: tuple[Optional[StageState], ...] = tuple(
    s for s in _STATE_CHOICES if s is not StageState.COMPLETED
)

# The arbitrarily-seeded Stages (all but 2 and 3, controlled by the test):
# Stage 2 (Campaign Selection) is the predecessor that opens the gate, and
# Stage 3 (DAG Generation) is the auto-completion target.
_OTHER_STAGES: tuple[int, ...] = tuple(
    e
    for e in VALID_STAGES
    if e not in (STAGE_CAMPAIGN_SELECTION, STAGE_DAG_GENERATION)
)


@st.composite
def _other_stage_states(draw) -> dict[int, Optional[StageState]]:
    """Arbitrary state distribution for the Stages other than 2 and 3."""
    return {
        stage: draw(st.sampled_from(_STATE_CHOICES)) for stage in _OTHER_STAGES
    }


def _seed_stage_runs(
    session: Session,
    campaign_id: str,
    states: dict[int, Optional[StageState]],
) -> None:
    """Persist one ``StageRun`` row per Stage whose state is not ``None``."""
    for stage, state in states.items():
        if state is None:
            continue
        session.add(
            StageRun(
                campaign_id=campaign_id,
                stage=stage,
                state=state,
                progress=100 if state is StageState.COMPLETED else 0,
            )
        )
    session.commit()


def _dag_stage_rows(session: Session, campaign_id: str) -> list[StageRun]:
    """All ``StageRun`` rows of ``(campaign_id, stage=3)``, by ``id``."""
    return list(
        session.execute(
            select(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == STAGE_DAG_GENERATION)
            .order_by(StageRun.id.asc())
        )
        .scalars()
        .all()
    )


def _count_dag_stage_completed(session: Session, campaign_id: str) -> int:
    """How many completed ``stage=3`` (DAG) rows exist for ``campaign_id``."""
    return (
        session.execute(
            select(func.count())
            .select_from(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == STAGE_DAG_GENERATION)
            .where(StageRun.state == StageState.COMPLETED)
        ).scalar_one()
    )


def _count_dags(session: Session, campaign_id: str) -> int:
    """How many persisted ``Dag`` rows exist for ``campaign_id``."""
    return (
        session.execute(
            select(func.count())
            .select_from(DagModel)
            .where(DagModel.campaign_id == campaign_id)
        ).scalar_one()
    )


def _build_dag_service(
    session: Session, data_dir: Path
) -> tuple[DagService, StageService]:
    """Build a ``DagService`` + ``StageService`` over the same Session/fixture."""
    case_service = CaseService(data_dir=data_dir)
    dag_service = DagService(
        session=session,
        case_service=case_service,
        generator=CurationDagGenerator(case_service),
    )
    stage_service = StageService(session, case_service=case_service)
    return dag_service, stage_service


# ===========================================================================
# Property 9.1 — Gate (Req. 9.3): Stage 2 not completed ⇒ does not complete.
# ===========================================================================


# Feature: pipeline-seis-stages, Property 9 (DAG part)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    stage2=st.sampled_from(_NON_COMPLETED_CHOICES),
    stage3=st.sampled_from(_STATE_CHOICES),
    outros=_other_stage_states(),
    structural_nodes=_acyclic_dag(),
)
def test_property9_gate_blocks_without_stage2_completed(
    stage2: Optional[StageState],
    stage3: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    structural_nodes: list[dict],
) -> None:
    """Stage 2 not completed ⇒ closed gate, does not complete Stage 3.

    For any distribution of states where Stage 2 (Campaign Selection) is not
    ``completed``, the sequential gate takes precedence:
    ``auto_complete_stage2`` returns ``completed=False`` /
    ``blocked_by_gate=True`` and does not persist any *new* ``stage=3`` row —
    even with a derivable curated DAG in the data dir. The gate is evaluated
    before idempotency, so the result is "blocked" even if a completed
    ``stage=3`` row already existed before.

    Validates: Requirements 9.1, 9.3, 9.4
    """
    assert stage2 is not StageState.COMPLETED

    data_dir = Path(tempfile.mkdtemp(prefix="pipeline6_dag_auto_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        # There IS a curated DAG on disk — to prove the gate takes precedence
        # even with material available.
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)
        dag_service, _stage_service = _build_dag_service(session, data_dir)

        states = dict(outros)
        states[STAGE_CAMPAIGN_SELECTION] = stage2
        states[STAGE_DAG_GENERATION] = stage3
        _seed_stage_runs(session, _CURATED_SLUG, states)

        completed_before = _count_dag_stage_completed(session, _CURATED_SLUG)

        result = dag_service.auto_complete_stage2(_CURATED_SLUG)

        # Closed gate takes precedence: does not complete and signals the block.
        assert result.completed is False
        assert result.blocked_by_gate is True
        assert result.no_dag is False
        # No completion was done by this call: the count stays the same as
        # before (0 when stage3 was not completed; 1 when it already was).
        assert (
            _count_dag_stage_completed(session, _CURATED_SLUG)
            == completed_before
        )
        assert completed_before == (
            1 if stage3 is StageState.COMPLETED else 0
        )
        # The gate closes before the backfill: no Dag is persisted.
        assert _count_dags(session, _CURATED_SLUG) == 0
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))


# ===========================================================================
# Property 9.2 — Idempotent auto-completion loading the DAG (Req. 9.1).
# ===========================================================================


# Feature: pipeline-seis-stages, Property 9 (DAG part)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    stage3_initial=st.sampled_from(_NON_COMPLETED_CHOICES),
    outros=_other_stage_states(),
    structural_nodes=_acyclic_dag(),
    n=st.integers(min_value=1, max_value=6),
)
def test_property9_auto_completes_loading_dag_and_idempotent(
    stage3_initial: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    structural_nodes: list[dict],
    n: int,
) -> None:
    """Stage 2 completed + curated DAG ⇒ auto-completes; N calls == 1.

    With the gate released (Stage 2 ``completed``) and a derivable curated DAG,
    Stage 3 auto-completes by loading the DAG: the ``(campaign, stage=3)`` row
    becomes ``completed``/100. Calling ``N`` times is idempotent — exactly one
    ``stage=3`` row with a stable ``id``, and the backfill does not duplicate the
    ``Dag`` row (a single curated entity). Completing Stage 3 unblocks Stage 4
    (Operationalization) via the sequential gate.

    Validates: Requirements 9.1, 9.3
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline6_dag_auto_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)
        dag_service, stage_service = _build_dag_service(session, data_dir)

        states = dict(outros)
        states[STAGE_CAMPAIGN_SELECTION] = StageState.COMPLETED
        states[STAGE_DAG_GENERATION] = stage3_initial
        _seed_stage_runs(session, _CURATED_SLUG, states)

        # BEFORE: Stage 4 is blocked while Stage 3 is not completed.
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is False

        # 1st call: auto-completes Stage 3 by loading the curated DAG.
        first = dag_service.auto_complete_stage2(_CURATED_SLUG)
        assert first.completed is True
        assert first.blocked_by_gate is False
        assert first.no_dag is False
        assert first.total_dags == 1
        assert first.stage_run_id is not None

        rows_1 = _dag_stage_rows(session, _CURATED_SLUG)
        assert len(rows_1) == 1
        line = rows_1[0]
        assert line.state is StageState.COMPLETED
        assert line.progress == 100
        stable_id = line.id

        # AFTER: Stage 3 completed ⇒ Stage 4 released (sequential gate).
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is True

        # Calls 2..N are idempotent no-ops: same single row, same id, and the
        # backfill still holds exactly one persisted Dag.
        for _ in range(n - 1):
            repeated = dag_service.auto_complete_stage2(_CURATED_SLUG)
            assert repeated.completed is True
            assert repeated.stage_run_id == stable_id

        rows_n = _dag_stage_rows(session, _CURATED_SLUG)
        assert len(rows_n) == 1
        assert rows_n[0].id == stable_id
        assert rows_n[0].state is StageState.COMPLETED
        assert rows_n[0].progress == 100
        assert _count_dag_stage_completed(session, _CURATED_SLUG) == 1
        # Idempotent backfill: exactly one curated Dag, no duplication.
        assert _count_dags(session, _CURATED_SLUG) == 1
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))


# ===========================================================================
# Property 9.3 — No derivable DAG ⇒ does not complete, advance blocked (Req. 9.4).
# ===========================================================================


# Feature: pipeline-seis-stages, Property 9 (DAG part)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    stage3_initial=st.sampled_from(_NON_COMPLETED_CHOICES),
    outros=_other_stage_states(),
    n=st.integers(min_value=1, max_value=6),
)
def test_property9_no_dag_does_not_complete_and_advance_blocked(
    stage3_initial: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    n: int,
) -> None:
    """Stage 2 completed but NO derivable DAG ⇒ ``no_dag``, does not complete.

    With the gate released (Stage 2 ``completed``) but no derivable curated DAG
    (the data dir has no ``{slug}_dag.json``), ``auto_complete_stage2`` returns
    ``no_dag=True``, does not complete the ``stage=3`` row, and the advance to
    Stage 4 stays blocked. The backfill is idempotent: no ``Dag`` row is
    persisted, even after ``N`` calls.

    Validates: Requirements 9.3, 9.4
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline6_dag_auto_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        # Curated campaign with NO derivable DAG: does not write ``{slug}_dag.json``.
        _write_case_fixture(data_dir, _CURATED_SLUG, [], com_dag=False)
        dag_service, stage_service = _build_dag_service(session, data_dir)

        states = dict(outros)
        states[STAGE_CAMPAIGN_SELECTION] = StageState.COMPLETED
        states[STAGE_DAG_GENERATION] = stage3_initial
        _seed_stage_runs(session, _CURATED_SLUG, states)

        for _ in range(n):
            result = dag_service.auto_complete_stage2(_CURATED_SLUG)
            # No derivable DAG: does not complete, signals ``no_dag``, gate open.
            assert result.completed is False
            assert result.no_dag is True
            assert result.blocked_by_gate is False
            assert result.total_dags == 0
            assert result.stage_run_id is None

        # Nothing completed: no completed ``stage=3`` row and Stage 4 stays
        # blocked by the sequential gate (Req. 9.4).
        assert _count_dag_stage_completed(session, _CURATED_SLUG) == 0
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is False
        # Idempotent backfill: no invalid Dag is persisted.
        assert _count_dags(session, _CURATED_SLUG) == 0
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))
