"""Example tests of campaign selection in Stage 2 - DB-backed (task 3.3).

Complements test_stage2_service.py. Stage 2 now reads identity + techniques for
ANY campaign from the PERSISTED STIX in the database (no disk, no curated gate):

* Version 18 lists EVERY campaign as selectable (Req. 4.1, 4.2, 4.5);
* Selecting any campaign (curated slug OR non-curated stix_id) completes with
  identity + techniques read from the DB (Req. 6.2, 6.6);
* A campaign absent from the persisted package stays not completed with a cause
  (Req. 6.6, 6.7).

_Requirements: 6.2, 6.5, 6.6, 6.7
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
from app.services.campaign_service import CURATED_CASES
from app.services.stage.stage_service import StageService
from app.services.stix.stage2_service import (
    ListedCampaign,
    Stage2Service,
    CampaignSelectionResult,
)
from app.services.stix.stix_service import SLUG_TO_STIX, StixService

VERSION = "18.0"

_NONCURATED = [
    ("campaign--nao-curada-1", "Non-Curated Campaign 1", "C9998"),
    ("campaign--nao-curada-2", "Non-Curated Campaign 2", "C9999"),
]


def _campaign_objeto(slug: str) -> dict:
    """STIX campaign object of a curated campaign from the catalog."""
    mapeamento = SLUG_TO_STIX[slug]
    referencias = mapeamento["referencias"]
    objeto: dict = {
        "type": "campaign",
        "id": f"campaign--{slug}",
        "name": mapeamento["nome_canonico"],
    }
    if referencias:
        objeto["external_references"] = [{"external_id": referencias[0]}]
    return objeto


def _bundle_com_8_curadas() -> dict:
    """Curated-version bundle with the 8 curated campaigns + 2 non-curated."""
    objetos = [_campaign_objeto(slug) for slug in CURATED_CASES]
    for stix_id, name, ext in _NONCURATED:
        objetos.append(
            {
                "type": "campaign",
                "id": stix_id,
                "name": name,
                "external_references": [{"external_id": ext}],
            }
        )
    return {"type": "bundle", "objects": objetos}


@pytest.fixture()
def session() -> Iterator[Session]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_stage2_t33_"))
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
    path_.write_text(json.dumps(_bundle_com_8_curadas()), encoding="utf-8")
    return stix


def _seed_from_bundle(session: Session) -> None:
    """Persist the 8 curated + 2 non-curated campaigns with one technique each."""
    package = StixPackage(version=VERSION, name=f"Enterprise ATT&CK v{VERSION}")
    session.add(package)
    session.flush()

    for slug in CURATED_CASES:
        mapping = SLUG_TO_STIX[slug]
        referencias = mapping["referencias"]
        campaign = StixCampaign(
            package_id=package.id,
            stix_id=f"campaign--{slug}",
            name=mapping["nome_canonico"],
            external_id=referencias[0] if referencias else None,
            description=f"Curated campaign {slug}.",
            aliases=[],
            curated=True,
            curated_slug=slug,
        )
        session.add(campaign)
        session.flush()
        session.add(
            StixCampaignTechnique(
                campaign_id=campaign.id,
                technique_id="T1190",
                technique_name="Exploit Public-Facing Application",
                tactic="initial-access",
            )
        )

    for stix_id, name, ext in _NONCURATED:
        campaign = StixCampaign(
            package_id=package.id,
            stix_id=stix_id,
            name=name,
            external_id=ext,
            description="Non-curated campaign.",
            aliases=[],
            curated=False,
            curated_slug=None,
        )
        session.add(campaign)
        session.flush()
        session.add(
            StixCampaignTechnique(
                campaign_id=campaign.id,
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
            )
        )
    session.commit()


def _service(session: Session, stix_tmp: StixService) -> Stage2Service:
    """Stage 2 over the seeded DB session + the temporary bundle for listing."""
    return Stage2Service(StageService(session, stix_service=stix_tmp))


def test_version_18_lists_every_campaign_selectable(
    session: Session, stix_tmp: StixService
) -> None:
    """Version 18 marks EVERY campaign as selectable (gate removed)."""
    service = _service(session, stix_tmp)
    campaigns = service.list_campaigns(VERSION)
    assert all(isinstance(c, ListedCampaign) for c in campaigns)
    assert campaigns, "expected campaigns in the v18 bundle"
    assert all(c.selectable is True for c in campaigns)

    curadas = [c for c in campaigns if c.curated]
    assert len(curadas) == 8
    assert {c.curated_slug for c in curadas} == set(CURATED_CASES)
    nao_curadas = [c for c in campaigns if not c.curated]
    assert len(nao_curadas) == 2
    assert all(c.selectable is True for c in nao_curadas)


@pytest.mark.parametrize("slug", sorted(CURATED_CASES))
def test_select_each_curated_completes_from_db(
    session: Session, stix_tmp: StixService, slug: str
) -> None:
    """Selecting any curated campaign (by slug) completes with DB identity + techniques."""
    _seed_from_bundle(session)
    service = _service(session, stix_tmp)

    result = service.select(slug, VERSION)

    assert isinstance(result, CampaignSelectionResult)
    assert result.campaign_id == slug
    assert result.completed is True
    assert result.error_message is None
    assert result.failed_step is None
    assert result.identity is not None and result.identity.found is True
    assert result.techniques, "expected techniques from the DB"
    assert all(t.technique_id for t in result.techniques)


def test_select_noncurated_by_stix_id_completes_from_db(
    session: Session, stix_tmp: StixService
) -> None:
    """A non-curated campaign (by stix_id) ALSO completes - gate removed."""
    _seed_from_bundle(session)
    service = _service(session, stix_tmp)

    stix_id = _NONCURATED[0][0]
    result = service.select(stix_id, VERSION)

    assert result.completed is True
    assert result.error_message is None
    assert [t.technique_id for t in result.techniques] == ["T1059"]
    assert result.identity is not None and result.identity.found is True


def test_select_without_persisted_package_does_not_complete_with_cause(
    session: Session, stix_tmp: StixService
) -> None:
    """With no persisted package, Stage 2 does not complete and reports the cause."""
    service = _service(session, stix_tmp)  # DB not seeded
    result = service.select("shadowray", VERSION)
    assert result.completed is False
    assert result.error_message
    assert result.failed_step is not None
    assert result.identity is not None and result.identity.found is False
    assert result.techniques == []


def test_select_campaign_absent_from_package_does_not_complete(
    session: Session, stix_tmp: StixService
) -> None:
    """A campaign key absent from the persisted package stays not completed, with cause."""
    _seed_from_bundle(session)
    service = _service(session, stix_tmp)

    result = service.select("campaign--does-not-exist", VERSION)

    assert result.completed is False
    assert result.error_message
    assert result.techniques == []
    assert result.identity is not None and result.identity.found is False
