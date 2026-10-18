"""Example tests of the campaign selection in Stage 2 (task 3.3).

They complement the orchestration tests in ``test_stage2_service.py`` (task 3.1)
focusing specifically on the example nodes required by task 3.3, without
duplicating them:

* **Version 18 lists the 8 selectable curated campaigns** — a bundle of the
  curated version containing exactly the 8 curated campaigns (resolved via
  ``SLUG_TO_STIX``) + non-curated campaigns yields ``selectable is True`` for
  exactly the 8 curated ones and ``False`` for the rest (Req. 6.2, 6.5);
* **Selecting extracts the attributes and concludes** — selecting each curated
  campaign (with the REAL curated artifacts present) extracts identity +
  techniques and concludes the Stage (``completed is True``), with no error
  (Req. 6.2, 6.6);
* **Nonexistent slug / extraction failure stays not completed with a cause** —
  both a campaign with a missing curated DAG and a slug outside the catalog keep
  the Stage not completed with the reported cause (Req. 6.6, 6.7).

Isolation: uses the REAL curated artifacts from the repository (``sticks/data``) for
os DAGs do Estágio 2 e um bundle STIX temporário (``data_dir`` isolated) para a
listagem/identidade — nenhuma rede, nenhum ``sticks/data/stix`` real tocado. A
persistência corre num SQLite efêmero por teste.

_Requirements: 6.2, 6.5, 6.6, 6.7_
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
from app.services.campaign_service import CURATED_CASES, CaseService
from app.services.stage.stage_service import StageService
from app.services.stix.stage2_service import (
    ListedCampaign,
    Stage2Service,
    CampaignSelectionResult,
)
from app.services.stix.stix_service import SLUG_TO_STIX, StixService

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


def _campaign_objeto(slug: str) -> dict:
    """Monta o objeto STIX ``campaign`` de uma campaign curated do catálogo.

    Usa o mapeamento ``SLUG_TO_STIX`` como fonte da verdade: quando há uma
    ``referencia`` (ex.: ``C0045``) ela é emitida em ``external_references`` (o
    canal de resolução prioritário); do contrário o ``nome_canonico`` resolve a
    campaign pelo name (campaign de ``salesforce_data_exfiltration``, sem código).
    """
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
    """Bundle da versão curated com as 8 campaigns curadas + 2 não curadas."""
    objetos = [_campaign_objeto(slug) for slug in CURATED_CASES]
    objetos.extend(
        [
            {
                "type": "campaign",
                "id": "campaign--nao-curated-1",
                "name": "Non-Curated Campaign 1",
                "external_references": [{"external_id": "C9998"}],
            },
            {
                "type": "campaign",
                "id": "campaign--nao-curated-2",
                "name": "Non-Curated Campaign 2",
                "external_references": [{"external_id": "C9999"}],
            },
        ]
    )
    return {"type": "bundle", "objects": objetos}


# ---------------------------------------------------------------------------
# Ephemeral SQLite session + temp STIX bundle dir.
# ---------------------------------------------------------------------------


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
    """A ``StixService`` apontando para um ``data_dir`` isolated com o bundle 18."""
    stix = StixService(data_dir=tmp_path)
    path_ = stix.base_path(VERSION)
    path_.parent.mkdir(parents=True, exist_ok=True)
    path_.write_text(json.dumps(_bundle_com_8_curadas()), encoding="utf-8")
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
# Versão 18 lista as 8 campaigns curadas selecionáveis — Req. 6.2, 6.5
# ---------------------------------------------------------------------------


def test_version_18_lists_the_8_curated_selectable(
    session: Session, stix_tmp: StixService
) -> None:
    """A versão 18 marca exatamente as 8 campaigns curadas como selecionáveis.

    As demais campaigns (não curadas) ficam não selecionáveis, pois a extração
    dos atributos depende dos artefatos curados em disco (Req. 6.2, 6.5).
    """
    service = _service(session, stix_tmp)

    campaigns = service.list_campaigns(VERSION)

    assert all(isinstance(c, ListedCampaign) for c in campaigns)

    selecionaveis = [c for c in campaigns if c.selectable]
    assert len(selecionaveis) == 8, "esperava exatamente as 8 campaigns curadas"
    assert all(c.curated and c.curated_slug is not None for c in selecionaveis)

    # Os slugs selecionáveis cobrem exatamente o catálogo de campaigns curadas.
    slugs_selecionaveis = {c.curated_slug for c in selecionaveis}
    assert slugs_selecionaveis == set(CURATED_CASES)

    # As não curadas permanecem indisponíveis para seleção.
    nao_curadas = [c for c in campaigns if not c.curated]
    assert len(nao_curadas) == 2
    assert all(c.selectable is False for c in nao_curadas)


# ---------------------------------------------------------------------------
# Selecting extracts the attributes and concludes — Req. 6.2, 6.6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(CURATED_CASES))
def test_select_each_curated_extracts_attributes_and_concludes(
    session: Session, stix_tmp: StixService, slug: str
) -> None:
    """Selecting any curated campaign extracts identity + techniques and concludes.

    Varre as 8 campaigns curadas: com os artefatos curados REAIS presentes, a
    seleção extrai a identidade do bundle e as técnicas do DAG curated, e conclui
    o Estágio sem erro (Req. 6.2, 6.6). campaign um artefato curated esteja ausente no
    checkout, o campaign é pulado em vez de falhar espúrio.
    """
    data_dir = _real_data_dir()
    if not _case_files_present(data_dir, slug):
        pytest.skip(f"Artefatos curados de {slug!r} ausentes no checkout.")

    service = _service(session, stix_tmp)

    result = service.select(slug, VERSION)

    assert isinstance(result, CampaignSelectionResult)
    assert result.campaign_id == slug
    assert result.completed is True
    assert result.error_message is None
    assert result.failed_step is None

    # Identidade montada a partir do objeto campaign do bundle (found).
    assert result.identity is not None
    assert result.identity.found is True

    # Técnicas associadas extraídas do DAG curated (a Saída do Estágio 2).
    assert result.techniques, "esperava técnicas extraídas do DAG curated"
    assert all(t.technique_id for t in result.techniques)


# ---------------------------------------------------------------------------
# Slug inexistente / extração failure mantém não concluído com causa — Req. 6.6, 6.7
# ---------------------------------------------------------------------------


def test_select_curated_without_dag_does_not_complete_with_cause(
    session: Session, stix_tmp: StixService
) -> None:
    """Uma campaign curated cujo DAG curated está ausente não conclui, com causa.

    Aponta o ``CaseService`` para um ``data_dir`` vazio: a extração estrutural do
    DAG failure, ``run_stage1`` reporta a failure e o Estágio 2 permanece não
    concluído, com a causa reportada e sem técnicas (Req. 6.6, 6.7). A identidade
    continua montada defensivamente.
    """
    vazio = Path(tempfile.mkdtemp(prefix="pipeline_ui_stage2_vazio_t33_"))
    try:
        stage = StageService(
            session,
            case_service=CaseService(data_dir=vazio),
            stix_service=stix_tmp,
        )
        service = Stage2Service(stage)

        result = service.select("shadowray", VERSION)

        assert result.completed is False
        assert result.error_message
        assert result.failed_step is not None
        assert result.identity is not None
        assert result.techniques == []
    finally:
        shutil.rmtree(vazio, ignore_errors=True)


def test_select_nonexistent_slug_does_not_complete_with_cause(
    session: Session, stix_tmp: StixService
) -> None:
    """Um slug fora do catálogo curated mantém o Estágio não concluído, com causa.

    Um slug inexistente não é um campaign válido: ``run_stage1`` não encontra o campaign
    e reporta a failure no result (``state = erro``), de modo que o Estágio 2
    permanece não concluído com a causa reportada e sem técnicas (Req. 6.6, 6.7).
    A identidade continua montada defensivamente (``found = False``), sem
    levantar — abrir a seleção de um slug inválido nunca derruba o Estágio.
    """
    service = _service(session, stix_tmp)

    result = service.select("slug_que_nao_existe", VERSION)

    assert result.completed is False
    assert result.error_message
    assert result.failed_step is not None
    assert result.techniques == []
    assert result.identity is not None
    assert result.identity.found is False
