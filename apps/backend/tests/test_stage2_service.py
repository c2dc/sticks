"""Tests of the thin Stage 2 service - Campaign selection (DB-backed).

Stage 2 now reads identity/aliases and techniques for ANY campaign (curated or
not) from the PERSISTED STIX in the database via
StixPackageReadService.campaign_overview - never from disk and with no
curated-artifact dependency. The curated/non-curated distinction is no longer a
selection gate; it is just an attribute in the persisted STIX.

_Requirements: 4.1, 4.2, 4.5, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7
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
from app.models.domain import (
    StixCampaign,
    StixCampaignTechnique,
    StixPackage,
)
from app.services.stage.stage_service import StageService
from app.services.stix.stage2_service import (
    ListedCampaign,
    Stage2Service,
    CampaignSelectionResult,
)
from app.services.stix.stix_service import StixService

VERSION = "18.0"

_CURATED_STIX_ID = "campaign--shadow"
_CURATED_SLUG = "shadowray"
_NONCURATED_STIX_ID = "campaign--outra"


def _bundle() -> dict:
    """Minimal STIX bundle: one curated (ShadowRay/C0045) + one non-curated."""
    return {
        "type": "bundle",
        "objects": [
            {
                "type": "campaign",
                "id": _CURATED_STIX_ID,
                "name": "ShadowRay",
                "external_references": [{"external_id": "C0045"}],
                "aliases": ["Shadow Ray"],
            },
            {
                "type": "campaign",
                "id": _NONCURATED_STIX_ID,
                "name": "Non-Curated Campaign",
                "external_references": [{"external_id": "C9999"}],
            },
        ],
    }


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
    """A StixService over an isolated data_dir carrying the v18 bundle."""
    stix = StixService(data_dir=tmp_path)
    path_ = stix.base_path(VERSION)
    path_.parent.mkdir(parents=True, exist_ok=True)
    path_.write_text(json.dumps(_bundle()), encoding="utf-8")
    return stix


def _seed_package(session: Session) -> None:
    """Seed the persisted package with both campaigns and a few techniques."""
    package = StixPackage(version=VERSION, name=f"Enterprise ATT&CK v{VERSION}")
    session.add(package)
    session.flush()

    curated = StixCampaign(
        package_id=package.id,
        stix_id=_CURATED_STIX_ID,
        name="ShadowRay",
        external_id="C0045",
        description="A curated campaign.",
        aliases=["Shadow Ray"],
        curated=True,
        curated_slug=_CURATED_SLUG,
    )
    noncurated = StixCampaign(
        package_id=package.id,
        stix_id=_NONCURATED_STIX_ID,
        name="Non-Curated Campaign",
        external_id="C9999",
        description="A non-curated campaign.",
        aliases=[],
        curated=False,
        curated_slug=None,
    )
    session.add_all([curated, noncurated])
    session.flush()

    session.add_all(
        [
            StixCampaignTechnique(
                campaign_id=curated.id,
                technique_id="T1190",
                technique_name="Exploit Public-Facing Application",
                tactic="initial-access",
            ),
            StixCampaignTechnique(
                campaign_id=noncurated.id,
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
            ),
        ]
    )
    session.commit()


def _service(session: Session, stix_tmp: StixService) -> Stage2Service:
    """Stage 2 over the seeded DB session + the temporary bundle for listing."""
    return Stage2Service(StageService(session, stix_service=stix_tmp))


def test_list_campaigns_marks_every_campaign_selectable(
    session: Session, stix_tmp: StixService
) -> None:
    """The curated-campaign gate is removed: EVERY campaign is selectable."""
    service = _service(session, stix_tmp)
    campaigns = service.list_campaigns(VERSION)
    assert all(isinstance(c, ListedCampaign) for c in campaigns)
    assert campaigns, "expected campaigns listed from the bundle"
    assert all(c.selectable is True for c in campaigns)


def test_select_curated_by_slug_completes_from_db(
    session: Session, stix_tmp: StixService
) -> None:
    """Selecting a curated campaign (by slug) completes with DB identity + techniques."""
    _seed_package(session)
    service = _service(session, stix_tmp)

    result = service.select(_CURATED_SLUG, VERSION)

    assert isinstance(result, CampaignSelectionResult)
    assert result.completed is True
    assert result.error_message is None
    assert result.failed_step is None
    assert result.identity is not None and result.identity.found is True
    assert result.identity.external_id == "C0045"
    assert "Shadow Ray" in result.identity.aliases
    assert [t.technique_id for t in result.techniques] == ["T1190"]


def test_select_noncurated_by_stix_id_completes_from_db(
    session: Session, stix_tmp: StixService
) -> None:
    """Selecting a NON-curated campaign (by stix_id) ALSO completes - gate removed.

    This is the regression that used to raise "Unknown curated case slug".
    """
    _seed_package(session)
    service = _service(session, stix_tmp)

    result = service.select(_NONCURATED_STIX_ID, VERSION)

    assert result.completed is True
    assert result.error_message is None
    assert result.failed_step is None
    assert result.identity is not None and result.identity.found is True
    assert result.identity.external_id == "C9999"
    assert [t.technique_id for t in result.techniques] == ["T1059"]


def test_select_without_persisted_package_does_not_complete_with_cause(
    session: Session, stix_tmp: StixService
) -> None:
    """With no persisted package, Stage 2 does not complete and reports the cause."""
    service = _service(session, stix_tmp)  # DB not seeded

    result = service.select(_CURATED_SLUG, VERSION)

    assert result.completed is False
    assert result.error_message
    assert result.identity is not None and result.identity.found is False
    assert result.techniques == []
