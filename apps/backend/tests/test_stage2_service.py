"""Example tests of the thin Stage 2 service — Campaign selection (task 3.1).

They cover the orchestration of :class:`app.services.stix.stage2_service.Stage2Service`
que reusa o :class:`app.services.stix.stix_service.StixService` (listagem das
bundle campaigns, marking ``curated``/``curated_slug``/``selectable``) and the
:class:`app.services.stage.stage_service.StageService`
(``_build_campaign_stix`` for the identity/aliases and ``run_stage1`` for the
associated techniques, extracted from the curated DAG):

* ``list_campaigns`` marks the curated campaigns as ``selectable`` and the rest
  as non-selectable (Req. 6.2, 6.5);
* ``select`` extracts the identity + techniques and concludes the Stage when the
  extraction succeeds (Req. 6.3, 6.4, 6.6);
* ``select`` keeps the Stage not completed with the reported cause when the
  extraction fails / the slug is nonexistent (Req. 6.7).

Isolation: uses the REAL curated artifacts from the repository (``sticks/data``)
for the Stage 2 DAG and a temporary STIX bundle (isolated ``data_dir``) for the
listing/identity — no network, no real ``sticks/data/stix`` touched. Persistence
runs in an ephemeral per-test SQLite.

_Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.services.campaign_service import CaseService
from app.services.stage.stage_service import StageService
from app.services.stix.stage2_service import (
    ListedCampaign,
    Stage2Service,
    CampaignSelectionResult,
)
from app.services.stix.stix_service import StixService

VERSION = "18.0"


# ---------------------------------------------------------------------------
# Real curated data dir (read-only) — located relative to the repo root.
# ---------------------------------------------------------------------------


def _real_data_dir() -> Path:
    """tests/ -> backend/ -> apps/ -> repo root -> packages/cli/data (read-only)."""
    return Path(__file__).resolve().parents[3] / "packages" / "cli" / "data"


def _case_files_present(data_dir: Path, slug: str) -> bool:
    return (
        (data_dir / "dag" / f"{slug}_dag.json").is_file()
        and (data_dir / "api" / f"{slug}_dag-ability.json").is_file()
        and (data_dir / "api" / f"{slug}_dag-adversary.json").is_file()
    )


def _bundle_com_shadowray() -> dict:
    """Minimal STIX bundle with the curated ShadowRay campaign (C0045) + one non-curated."""
    return {
        "type": "bundle",
        "objects": [
            {
                "type": "campaign",
                "id": "campaign--shadow",
                "name": "ShadowRay",
                "external_references": [{"external_id": "C0045"}],
                "aliases": ["Shadow Ray"],
            },
            {
                "type": "campaign",
                "id": "campaign--outra",
                "name": "Non-Curated Campaign",
                "external_references": [{"external_id": "C9999"}],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Ephemeral SQLite session + temp STIX bundle dir.
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_stage2_"))
    db_path = tmp_dir / "stage2.db"
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


@pytest.fixture()
def stix_tmp(tmp_path: Path) -> StixService:
    """A ``StixService`` apontando para um ``data_dir`` isolated com o bundle 18."""
    stix = StixService(data_dir=tmp_path)
    path_ = stix.base_path(VERSION)
    path_.parent.mkdir(parents=True, exist_ok=True)
    path_.write_text(json.dumps(_bundle_com_shadowray()), encoding="utf-8")
    return stix


def _service(session: Session, stix_tmp: StixService) -> Stage2Service:
    """Builds Stage 2 reusing the real curated data + the temporary bundle."""
    stage = StageService(
        session,
        case_service=CaseService(data_dir=_real_data_dir()),
        stix_service=stix_tmp,
    )
    return Stage2Service(stage)


# ---------------------------------------------------------------------------
# list_campaigns — Req. 6.2, 6.5
# ---------------------------------------------------------------------------


def test_list_campaigns_marks_curated_selectable(
    session: Session, stix_tmp: StixService
) -> None:
    """The curated campaign is selectable; the non-curated one is not (Req. 6.2, 6.5)."""
    service = _service(session, stix_tmp)

    campaigns = service.list_campaigns(VERSION)

    assert all(isinstance(c, ListedCampaign) for c in campaigns)
    por_slug = {c.curated_slug: c for c in campaigns if c.curated}
    assert "shadowray" in por_slug
    assert por_slug["shadowray"].selectable is True

    non_curated = [c for c in campaigns if not c.curated]
    assert non_curated, "expected at least one non-curated campaign in the bundle"
    assert all(c.selectable is False for c in non_curated)


# ---------------------------------------------------------------------------
# select — success (Req. 6.3, 6.4, 6.6)
# ---------------------------------------------------------------------------


def test_select_curated_extracts_attributes_and_concludes(
    session: Session, stix_tmp: StixService
) -> None:
    """Selecting the curated campaign extracts identity + techniques and concludes.

    _Requirements: 6.3, 6.4, 6.6_
    """
    data_dir = _real_data_dir()
    if not _case_files_present(data_dir, "shadowray"):
        pytest.skip("Artefatos curados de 'shadowray' ausentes no checkout.")

    service = _service(session, stix_tmp)

    result = service.select("shadowray", VERSION)

    assert isinstance(result, CampaignSelectionResult)
    assert result.completed is True
    assert result.error_message is None
    assert result.failed_step is None

    # Identidade/aliases vieram do objeto campaign do bundle (found).
    assert result.identity is not None
    assert result.identity.found is True
    assert result.identity.external_id == "C0045"
    assert "Shadow Ray" in result.identity.aliases

    # Associated techniques extracted from the curated DAG (Stage 2 Output).
    assert result.techniques, "expected techniques extracted from the curated DAG"
    assert all(t.technique_id for t in result.techniques)


# ---------------------------------------------------------------------------
# select — extraction failure (Req. 6.7)
# ---------------------------------------------------------------------------


def test_select_nonexistent_slug_does_not_complete_with_cause(
    session: Session, stix_tmp: StixService
) -> None:
    """A campaign without a structural DAG keeps the Stage not completed with the cause.

    Usa um ``CaseService`` apontando para um ``data_dir`` empty: o DAG curated do
    campaign does not exist, so ``run_stage1`` reports a failure and Stage 2 does not
    conclude, with the reported cause (Req. 6.7).
    """
    empty = Path(tempfile.mkdtemp(prefix="pipeline_ui_stage2_empty_"))
    try:
        stage = StageService(
            session,
            case_service=CaseService(data_dir=empty),
            stix_service=stix_tmp,
        )
        service = Stage2Service(stage)

        result = service.select("shadowray", VERSION)

        assert result.completed is False
        assert result.error_message
        # A identidade continua montada (defensiva), independentemente da failure.
        assert result.identity is not None
        assert result.techniques == []
    finally:
        shutil.rmtree(empty, ignore_errors=True)
