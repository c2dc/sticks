"""Testes de exemplo do serviço fino do Estágio 2 — Seleção da campaign (task 3.1).

Cobrem a orquestração de :class:`app.services.stix.estagio2_service.Stage2Service`
que reusa o :class:`app.services.stix.stix_service.StixService` (listagem das
campaigns do bundle, marcando ``curated``/``curated_slug``/``selectable``) e o
:class:`app.services.stage.stage_service.StageService`
(``_build_campaign_stix`` para a identidade/aliases e ``run_stage1`` para as
técnicas associadas, extraídas do DAG curated):

* ``list_campaigns`` marca as campaigns curadas como ``selectable`` e as
  demais como não selecionáveis (Req. 6.2, 6.5);
* ``select`` extrai a identidade + técnicas e conclui o Estágio quando a
  extração tem success (Req. 6.3, 6.4, 6.6);
* ``select`` mantém o Estágio não concluído com a causa reportada quando a
  extração failure / o slug é inexistente (Req. 6.7).

Isolamento: usa os artefatos curados REAIS do repositório (``sticks/data``) para
o DAG do Estágio 2 e um bundle STIX temporário (``data_dir`` isolated) para a
listagem/identidade — nenhuma rede, nenhum ``sticks/data/stix`` real tocado. A
persistência corre num SQLite efêmero por teste.

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
from app.services.stix.estagio2_service import (
    ListedCampaign,
    Stage2Service,
    CampaignSelectionResult,
)
from app.services.stix.stix_service import StixService

VERSAO = "18.0"


# ---------------------------------------------------------------------------
# Real curated data dir (read-only) — located relative to the repo root.
# ---------------------------------------------------------------------------


def _real_data_dir() -> Path:
    """tests/ -> backend/ -> ui/ -> repo root -> sticks/data (read-only)."""
    return Path(__file__).resolve().parents[3] / "sticks" / "data"


def _case_files_present(data_dir: Path, slug: str) -> bool:
    return (
        (data_dir / "dag" / f"{slug}_dag.json").is_file()
        and (data_dir / "api" / f"{slug}_dag-ability.json").is_file()
        and (data_dir / "api" / f"{slug}_dag-adversary.json").is_file()
    )


def _bundle_com_shadowray() -> dict:
    """Bundle STIX mínimo com a campaign curated ShadowRay (C0045) + uma não curated."""
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
                "name": "Campanha Não Curada",
                "external_references": [{"external_id": "C9999"}],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Ephemeral SQLite session + temp STIX bundle dir.
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio2_"))
    db_path = tmp_dir / "estagio2.db"
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
    caminho = stix.base_path(VERSAO)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(_bundle_com_shadowray()), encoding="utf-8")
    return stix


def _service(session: Session, stix_tmp: StixService) -> Stage2Service:
    """Monta o Estágio 2 reusando os dados curados reais + o bundle temporário."""
    stage = StageService(
        session,
        case_service=CaseService(data_dir=_real_data_dir()),
        stix_service=stix_tmp,
    )
    return Stage2Service(stage)


# ---------------------------------------------------------------------------
# list_campaigns — Req. 6.2, 6.5
# ---------------------------------------------------------------------------


def test_listar_campanhas_marca_curada_selecionavel(
    session: Session, stix_tmp: StixService
) -> None:
    """A campaign curated fica selecionável; a não curated não (Req. 6.2, 6.5)."""
    service = _service(session, stix_tmp)

    campaigns = service.list_campaigns(VERSAO)

    assert all(isinstance(c, ListedCampaign) for c in campaigns)
    por_slug = {c.curated_slug: c for c in campaigns if c.curated}
    assert "shadowray" in por_slug
    assert por_slug["shadowray"].selectable is True

    nao_curadas = [c for c in campaigns if not c.curated]
    assert nao_curadas, "esperava ao menos uma campaign não curated no bundle"
    assert all(c.selectable is False for c in nao_curadas)


# ---------------------------------------------------------------------------
# select — success (Req. 6.3, 6.4, 6.6)
# ---------------------------------------------------------------------------


def test_selecionar_curada_extrai_atributos_e_conclui(
    session: Session, stix_tmp: StixService
) -> None:
    """Selecionar a campaign curated extrai identidade + técnicas e conclui.

    _Requirements: 6.3, 6.4, 6.6_
    """
    data_dir = _real_data_dir()
    if not _case_files_present(data_dir, "shadowray"):
        pytest.skip("Artefatos curados de 'shadowray' ausentes no checkout.")

    service = _service(session, stix_tmp)

    result = service.select("shadowray", VERSAO)

    assert isinstance(result, CampaignSelectionResult)
    assert result.completed is True
    assert result.error_message is None
    assert result.failed_step is None

    # Identidade/aliases vieram do objeto campaign do bundle (found).
    assert result.identity is not None
    assert result.identity.found is True
    assert result.identity.external_id == "C0045"
    assert "Shadow Ray" in result.identity.aliases

    # Técnicas associadas extraídas do DAG curated (Saída do Estágio 2).
    assert result.techniques, "esperava técnicas extraídas do DAG curated"
    assert all(t.technique_id for t in result.techniques)


# ---------------------------------------------------------------------------
# select — failure de extração (Req. 6.7)
# ---------------------------------------------------------------------------


def test_selecionar_slug_inexistente_nao_conclui_com_causa(
    session: Session, stix_tmp: StixService
) -> None:
    """Um campaign sem DAG estrutural mantém o Estágio não concluído com a causa.

    Usa um ``CaseService`` apontando para um ``data_dir`` vazio: o DAG curated do
    campaign não existe, então ``run_stage1`` reporta failure e o Estágio 2 não
    conclui, com a causa reportada (Req. 6.7).
    """
    vazio = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio2_vazio_"))
    try:
        stage = StageService(
            session,
            case_service=CaseService(data_dir=vazio),
            stix_service=stix_tmp,
        )
        service = Stage2Service(stage)

        result = service.select("shadowray", VERSAO)

        assert result.completed is False
        assert result.error_message
        # A identidade continua montada (defensiva), independentemente da failure.
        assert result.identity is not None
        assert result.techniques == []
    finally:
        shutil.rmtree(vazio, ignore_errors=True)
