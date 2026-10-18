"""Property tests for DAG choice and the Stage 2 sequential gate (task 5.5).

# Feature: pipeline-cinco-stages, Property 9: Choosing a DAG completes
# Stage 2 (DAG Generation) and unblocks Stage 3.
# Feature: pipeline-cinco-stages, Property 10: Without a DAG, advancing beyond
# Stage 2 stays blocked.

Validates: Requirements 4.7, 6.2, 6.3, 6.5

These are *feature* property tests (the ``pipeline-cinco-stages`` spec is a
feature, not a bugfix): the properties are expected to **hold** against the
current implementation of
:meth:`~app.services.dag.dag_service.DagService.choose_dag` and the sequential
gate of :class:`~app.services.stage.stage_service.StageService`.

In the six-Stage pipeline, DAG Generation is **Stage 3** (its Input is the Stage
2 campaign attributes — Campaign Selection): ``choose_dag`` writes the
``chosen_dag_id`` on the ``stage=3`` ``StageRun`` and completes it, unblocking
Stage 4 (Operationalization). ``DagChoiceResult.stage`` is
:data:`ESTAGIO_GERACAO_DAG` (``== 3``).

Properties (design.md — Properties 9 and 10)
--------------------------------------------
(a) *A valid choice completes Stage 3 and is idempotent* (Property 9):
    after ``sync_curated_dags(slug)``, choosing a valid, persisted ``dag_id`` of
    the campaign makes ``choose_dag(slug, dag_id)`` return ``success=True``; the
    ``stage=3`` ``StageRun`` becomes ``completed`` with
    ``chosen_dag_id == dag_id``; repeating the same choice ``N`` times is
    idempotent — same ``stage_run_id``, exactly one ``stage=3`` row, same
    ``chosen_dag_id`` (Req. 4.7, 6.3).

(b) *A non-existent/foreign dag_id is refused without touching Stage 3*:
    a ``dag_id`` that does not exist, or that belongs to **another** campaign, is
    refused with ``failure == DAG_NOT_FOUND`` and the ``stage=3`` ``StageRun``
    stays unchanged (or absent) (Req. 6.3 — error).

(c) *A campaign with no persisted DAG is refused with NO_DAG* (Req. 6.2 gate):
    with no persisted ``Dag`` for the campaign, ``choose_dag`` refuses with
    ``failure == NO_DAG`` and nothing is completed.

(d) *Stage 4 sequential gate* (Property 10):
    while Stage 3 (DAG Generation) is not ``completed``, Stage 4 stays blocked
    (``is_stage_startable(campaign, 4)`` is ``False`` and the view reports
    ``blocked=True``); after a successful choice (Stage 3 ``completed``), Stage 4
    becomes startable (Req. 6.2, 6.3).

Strategy / isolation
--------------------
Each example points a :class:`CaseService` at a **temporary** data directory
with the ``<data_dir>/{api,dag}/`` layout (same as the real one), into which we
write a generated ``{slug}_dag.json``, and builds a ``DagService`` + a
``StageService`` over the **same** **ephemeral** SQLite ``Session`` (engine on a
temporary file with the full schema via ``Base.metadata.create_all``, same as
the ``test_backfill_idempotente_property.py`` and
``test_stage_blocking_property.py`` fixtures). The generated DAGs are acyclic by
construction. Nothing here touches the network nor an AI client (there is none
on this path).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

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
from app.services.dag.dag_service import (
    ESTAGIO_GERACAO_DAG,
    DagService,
    DagChoiceFailure,
)
from app.services.dag.gerador import CurationDagGenerator
from app.services.stage.stage_service import StageService

# A real curated campaign slug: curation generation only produces a DAG for
# curated slugs (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guard: keep the curated-fixture slug

# A second curated slug, for the "dag_id from another campaign" scenario in (b).
_OTHER_SLUG = "apt41_dust"
assert _OTHER_SLUG in CURATED_CASES
assert _OTHER_SLUG != _CURATED_SLUG


# ---------------------------------------------------------------------------
# Ephemeral DB fixture (same as test_backfill_idempotente_property.py).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_escolha_dag_prop_")
    db_path = Path(tmp_dir) / "escolha_dag.db"
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
) -> None:
    """Write the ability + DAG fixtures for ``slug`` under ``data_dir``."""
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

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


def _estagio_dag_rows(session: Session, campaign: str) -> list[StageRun]:
    """All ``stage=3`` (DAG) ``StageRun`` rows of the campaign, by ``id``."""
    return (
        session.execute(
            select(StageRun)
            .where(StageRun.campaign_id == campaign)
            .where(StageRun.stage == ESTAGIO_GERACAO_DAG)
            .order_by(StageRun.id.asc())
        )
        .scalars()
        .all()
    )


def _count_estagio_dag(session: Session, campaign: str) -> int:
    """Count of ``stage=3`` (DAG) ``StageRun`` rows of the campaign."""
    return (
        session.execute(
            select(func.count())
            .select_from(StageRun)
            .where(StageRun.campaign_id == campaign)
            .where(StageRun.stage == ESTAGIO_GERACAO_DAG)
        )
        .scalar_one()
    )


# ===========================================================================
# Property 9 — a valid choice completes Stage 2, is idempotent, and unblocks
# Stage 3 (gate — Property 10, "after" part).
# ===========================================================================


# Feature: pipeline-cinco-stages, Property 9: Choosing a DAG completes Stage 2
# and unblocks Stage 3.
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(structural_nodes=_acyclic_dag(), repeticoes=st.integers(min_value=1, max_value=5))
def test_property9_escolha_valida_conclui_estagio2_e_e_idempotente(
    structural_nodes: list[dict], repeticoes: int
) -> None:
    """Choosing a valid ``dag_id`` completes Stage 2; repeating is idempotent.

    After the backfill, choosing a persisted DAG of the campaign returns
    ``success=True``; the ``stage=3`` ``StageRun`` becomes ``completed``
    (progress 100) with ``chosen_dag_id == dag_id``; repeating the same choice
    ``N`` times keeps the same ``stage_run_id``, exactly one ``stage=3`` row and
    the same ``chosen_dag_id`` (Req. 4.7, 6.3). Before the choice Stage 4 is
    blocked; afterwards, it becomes startable (gate — Req. 6.2, 6.3).

    Validates: Requirements 4.7, 6.2, 6.3
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_escolha_dag_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)
        case_service = CaseService(data_dir=data_dir)
        dag_service = DagService(
            session=session,
            case_service=case_service,
            gerador=CurationDagGenerator(case_service),
        )
        stage_service = StageService(session)

        # Backfill: persists exactly one curadoria_humana Dag for the campaign.
        # It is the *choice* that completes Stage 3 (DAG Generation) and unblocks
        # Stage 4 (Operationalization) via the sequential gate.
        resumos = dag_service.sync_curated_dags(_CURATED_SLUG)
        assert len(resumos) == 1
        dag_id = resumos[0].id
        assert resumos[0].origin == DagOrigin.HUMAN_CURATION

        # (d) — BEFORE the choice: Stage 4 is blocked (sequential gate), because
        # Stage 3 (DAG Generation) is not completed yet (Req. 6.2).
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is False
        view4_antes = stage_service.get_stage_state(_CURATED_SLUG, 4)
        assert view4_antes.blocked is True
        assert view4_antes.startable is False

        # 1st choice: success, completes Stage 3 with chosen_dag_id = dag_id.
        result = dag_service.choose_dag(_CURATED_SLUG, dag_id)
        assert result.success is True
        assert result.failure is None
        assert result.stage == ESTAGIO_GERACAO_DAG
        assert result.stage_run_id is not None
        primeiro_stage_run_id = result.stage_run_id

        lines = _estagio_dag_rows(session, _CURATED_SLUG)
        assert len(lines) == 1
        line = lines[0]
        assert line.id == primeiro_stage_run_id
        assert line.state == StageState.COMPLETED
        assert line.progress == 100
        assert line.chosen_dag_id == dag_id

        # (d) — AFTER the choice: Stage 3 is completed ⇒ Stage 4 released.
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is True
        view4_depois = stage_service.get_stage_state(_CURATED_SLUG, 4)
        assert view4_depois.blocked is False
        assert view4_depois.startable is True

        # Idempotency: repeating the SAME choice N times does not change the
        # state — same stage_run_id, exactly one stage=3 row, same chosen_dag_id.
        for _ in range(repeticoes):
            repetido = dag_service.choose_dag(_CURATED_SLUG, dag_id)
            assert repetido.success is True
            assert repetido.stage_run_id == primeiro_stage_run_id

        assert _count_estagio_dag(session, _CURATED_SLUG) == 1
        linha_final = _estagio_dag_rows(session, _CURATED_SLUG)[0]
        assert linha_final.id == primeiro_stage_run_id
        assert linha_final.chosen_dag_id == dag_id
        assert linha_final.state == StageState.COMPLETED
        assert linha_final.progress == 100
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))


# ===========================================================================
# Property (b) — a non-existent/foreign dag_id is refused (DAG_NOT_FOUND)
# without altering the Stage 2 StageRun.
# ===========================================================================


# Feature: pipeline-cinco-stages, Property 9: Choosing a DAG completes Stage 2
# and unblocks Stage 3 (negative side — foreign/non-existent dag).
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    structural_nodes=_acyclic_dag(),
    other_nodes=_acyclic_dag(),
    desloca=st.integers(min_value=1, max_value=1000),
)
def test_propertyb_dag_inexistente_ou_alheio_e_recusado(
    structural_nodes: list[dict], other_nodes: list[dict], desloca: int
) -> None:
    """A non-existent/foreign ``dag_id`` ⇒ ``DAG_NOT_FOUND``; Stage 2 intact.

    With a persisted DAG for the target campaign (to pass the ``NO_DAG`` gate)
    and a DAG from **another** campaign, choosing the other campaign's ``dag_id``
    — or a non-existent ``dag_id`` — is refused with
    ``failure == DAG_NOT_FOUND`` and the target campaign's ``stage=3``
    ``StageRun`` stays unchanged (not completed by any valid choice).

    Validates: Requirements 6.3
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_escolha_dag_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)
        _write_case_fixture(data_dir, _OTHER_SLUG, other_nodes)
        case_service = CaseService(data_dir=data_dir)
        dag_service = DagService(
            session=session,
            case_service=case_service,
            gerador=CurationDagGenerator(case_service),
        )

        # Persist a DAG for each campaign. The target campaign HAS a DAG (passes
        # the NO_DAG gate), but the chosen dag_id will be the OTHER campaign's.
        resumos_alvo = dag_service.sync_curated_dags(_CURATED_SLUG)
        resumos_outro = dag_service.sync_curated_dags(_OTHER_SLUG)
        assert len(resumos_alvo) == 1
        assert len(resumos_outro) == 1
        dag_id_alheio = resumos_outro[0].id

        # Non-existent dag_id: an id above the maximum persisted one.
        max_id = session.execute(select(func.max(DagModel.id))).scalar_one()
        dag_id_inexistente = max_id + desloca

        for dag_id in (dag_id_alheio, dag_id_inexistente):
            result = dag_service.choose_dag(_CURATED_SLUG, dag_id)
            assert result.success is False
            assert result.failure == DagChoiceFailure.DAG_NOT_FOUND
            assert result.stage_run_id is None

        # The target campaign's Stage 3 (DAG Generation) StageRun was NOT
        # created/altered: no valid choice occurred, so there is no completed
        # stage=3 row. (The backfill materializes the campaign attributes, but
        # that does not create a stage=3 row nor complete it.)
        linhas_alvo = _estagio_dag_rows(session, _CURATED_SLUG)
        assert linhas_alvo == []
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))


# ===========================================================================
# Property (c) — a campaign with no persisted DAG is refused with NO_DAG and the
# advance beyond Stage 3 stays blocked (Property 10 — Req. 6.2).
# ===========================================================================


# Feature: pipeline-cinco-stages, Property 10: Without a DAG, the advance beyond
# Stage 3 stays blocked.
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(dag_id=st.integers(min_value=1, max_value=10_000))
def test_property10_sem_dag_recusa_e_estagio4_bloqueado(dag_id: int) -> None:
    """No persisted DAG ⇒ ``NO_DAG`` and Stage 4 stays blocked.

    With a campaign that has no persisted ``Dag``, ``choose_dag`` refuses with
    ``failure == NO_DAG`` for any ``dag_id`` and nothing is completed; without
    Stage 3 (DAG Generation) completed, the sequential gate keeps Stage 4
    (Operationalization) blocked (Req. 6.2).

    Validates: Requirements 6.2
    """
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        dag_service = DagService(session=session)
        stage_service = StageService(session)

        # No backfill: the campaign has no persisted DAG (gate Req. 6.2).
        result = dag_service.choose_dag(_CURATED_SLUG, dag_id)
        assert result.success is False
        assert result.failure == DagChoiceFailure.NO_DAG
        assert result.stage_run_id is None

        # Nothing was completed: there is no stage=3 row, and Stage 4 stays
        # blocked by the sequential gate (Stage 3 did not complete).
        assert _estagio_dag_rows(session, _CURATED_SLUG) == []
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is False
        view4 = stage_service.get_stage_state(_CURATED_SLUG, 4)
        assert view4.blocked is True
        assert view4.startable is False
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(Path(db_tmp_dir))
