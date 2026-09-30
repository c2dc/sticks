"""Integration test for the real Stage 1 (Modelagem Estrutural) — task 8.6.

This exercises :meth:`StageService.run_stage1` end-to-end against the **real
curated data files** shipped in the repository under ``sticks/data`` — the same
files the production pipeline reads:

* ``data/api/{slug}_dag-ability.json``  — the curated Abilities,
* ``data/api/{slug}_dag-adversary.json`` — the Adversary grouping,
* ``data/dag/{slug}_dag.json``          — the structural DAG the curated
  pipeline emitted (campaign metadata, ``structural_nodes`` with technique
  identity, parent/child relationships, ``provides`` tags and
  ``attacker_commands``), from which Stage 1 derives its structural elements.

It is **pure local integration**: it reads real JSON from disk and persists the
extraction into an ephemeral SQLite database created per test (mirroring the
temp-file SQLite + ``Base.create_all`` pattern used across the suite, e.g.
``test_aggregate_progress_property.py``). It performs **no network access** and
does **NOT** start emulation, touch Caldera, or use the attack Docker
environment — Stage 1 is only the structural modeling (STIX/DAG parsing). There
is therefore intentionally no ``lab`` marker on this module.

What it validates (Req. 2.2 automated structural modeling; Req. 2.3 the
extracted elements — techniques, relationships, indicators, infrastructure,
malware and campaign metadata):

* Running the real structural modeling for 1–2 curated cases (``shadowray`` and
  ``costaricto``) yields a **non-empty** extraction: at least techniques,
  relationships and campaign metadata, with indicators/infrastructure/malware
  present too for these curated cases.
* The extraction is **persisted** and associated with the case: the ``Case`` row
  exists, the curated ``Ability`` rows (and the ``Adversary``) are written, and
  the Stage-1 ``StageRun`` is marked ``concluido`` (Req. 2.4) with no error.

If a case's real input artifacts are absent from the checkout the test skips
that case with an explanatory reason rather than failing — but the curated
cases ship complete, so the real non-empty extraction is what normally runs.

_Requisitos: 2.2, 2.3_
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    Ability as AbilityModel,
    Adversary as AdversaryModel,
    Case as CaseModel,
    StageRun,
)
from app.models.enums import StageState
from app.services.case_service import CaseService
from app.services.stage.stage_service import StageService

# The curated cases exercised here. Both ship a complete ability/adversary/DAG
# set under ``sticks/data``; the real structural extraction is non-empty for
# both.
REAL_CASES: tuple[str, ...] = ("shadowray", "costaricto")


# ---------------------------------------------------------------------------
# Real data dir (read-only) — located relative to the repo root, exactly like
# ``test_case_service.py`` / ``test_subnet_validator.py`` do.
# ---------------------------------------------------------------------------


def _real_data_dir() -> Path:
    """Locate the real ``sticks/data`` dir relative to the repo root.

    tests/ -> backend/ -> pipeline_ui/ -> repo root -> sticks/data (read-only).
    """
    here = Path(__file__).resolve()
    return here.parents[3] / "sticks" / "data"


def _case_files_present(data_dir: Path, slug: str) -> bool:
    """Whether every real input artifact Stage 1 needs for ``slug`` exists.

    Stage 1 requires the structural DAG (``data/dag/{slug}_dag.json``) plus the
    curated ability/adversary pair (``data/api/{slug}_dag-*.json``).
    """
    return (
        (data_dir / "dag" / f"{slug}_dag.json").is_file()
        and (data_dir / "api" / f"{slug}_dag-ability.json").is_file()
        and (data_dir / "api" / f"{slug}_dag-adversary.json").is_file()
    )


# ---------------------------------------------------------------------------
# Ephemeral SQLite session (fresh temp-file DB + schema per test).
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    """Yield a fresh temp-file SQLite session with the full schema created.

    Uses the temp-file SQLite + ``Base.metadata.create_all`` pattern shared by
    the rest of the suite so the real Stage-1 persistence runs against a real
    (but throwaway) database. Torn down entirely afterward.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_stage1_int_"))
    db_path = tmp_dir / "stage1_integration.db"
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
    db_session = session_factory()
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The integration test — real Stage 1 over the local data files.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", REAL_CASES)
def test_real_stage1_extracts_and_persists_non_empty(
    slug: str, session: Session
) -> None:
    """Real Stage 1 for a curated case extracts a non-empty structural model
    from the local ``data/`` files, persists it, and completes the stage.

    _Requisitos: 2.2, 2.3_
    """
    data_dir = _real_data_dir()
    if not _case_files_present(data_dir, slug):
        pytest.skip(
            f"Artefatos STIX/DAG de entrada ausentes para o caso {slug!r} em "
            f"{data_dir} — modelagem estrutural real não pode ser executada."
        )

    # Point the service at the REAL local data dir (read-only). No Caldera, no
    # Docker, no network — this is only the structural modeling (Stage 1).
    case_service = CaseService(data_dir=data_dir)
    service = StageService(session, case_service=case_service)

    result = service.run_stage1(slug)

    # --- Stage completed (Req. 2.2, 2.4) ----------------------------------
    assert result.estado is StageState.COMPLETED, result.mensagem_erro
    assert result.ok is True
    assert result.is_error is False
    assert result.mensagem_erro is None
    assert result.etapa_falha is None
    assert result.stage_run_id is not None

    # --- Entrada references the real source dataset (Req. 2.1) ------------
    assert result.entrada.caso_id == slug
    assert result.entrada.dag_file is not None
    assert result.entrada.campaign_name

    # --- Saída is a NON-EMPTY structural extraction (Req. 2.3) ------------
    saida = result.saida
    assert saida is not None
    assert saida.is_empty is False

    # Techniques + relationships form the structural graph.
    assert len(saida.techniques) > 0
    assert len(saida.relationships) > 0
    # Every technique carries an ATT&CK id (the core Stage-1 output).
    assert all(t.technique_id for t in saida.techniques)
    # Relationship endpoints reference real technique nodes.
    tech_ids = {t.technique_id for t in saida.techniques}
    assert tech_ids  # non-empty

    # Indicators mined from the curated attacker commands (destinations).
    assert len(saida.indicators) > 0
    assert all(i.value for i in saida.indicators)

    # Infrastructure and malware elements were inferred for these cases.
    assert len(saida.infrastructure) > 0
    assert len(saida.malware) > 0

    # Campaign metadata is populated.
    assert saida.metadata
    assert saida.metadata.get("campaign_name")
    assert saida.metadata.get("total_nodes", 0) > 0

    # --- The extraction was PERSISTED and associated with the case -------
    db_case = session.get(CaseModel, slug)
    assert db_case is not None
    assert db_case.nome

    # Curated Abilities persisted and linked to the case (Req. 2.4 — the
    # extracted/curated elements are persisted associated with the case).
    ability_count = session.execute(
        select(func.count())
        .select_from(AbilityModel)
        .where(AbilityModel.caso_id == slug)
    ).scalar_one()
    assert ability_count > 0

    # Adversary persisted and linked to the case.
    adversary = session.execute(
        select(AdversaryModel).where(AdversaryModel.caso_id == slug)
    ).scalars().first()
    assert adversary is not None
    assert adversary.atomic_ordering

    # The Stage-1 StageRun exists, is marked concluido and error-free (Req. 2.4).
    stage_run = session.execute(
        select(StageRun)
        .where(StageRun.caso_id == slug)
        .where(StageRun.estagio == 1)
    ).scalars().first()
    assert stage_run is not None
    assert stage_run.estado is StageState.COMPLETED
    assert stage_run.progresso == 100
    assert stage_run.mensagem_erro is None
    assert stage_run.etapa_falha is None


def test_real_stage1_at_least_one_curated_case_available() -> None:
    """Guard: at least one curated case must ship its real input artifacts, so
    the real (non-skipped) structural extraction above actually runs.

    _Requisitos: 2.2_
    """
    data_dir = _real_data_dir()
    available = [slug for slug in REAL_CASES if _case_files_present(data_dir, slug)]
    assert available, (
        "Nenhum caso curado possui os artefatos STIX/DAG reais em "
        f"{data_dir}; a integração local do Estágio 1 não pôde ser validada."
    )
