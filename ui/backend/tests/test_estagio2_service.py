"""Testes de exemplo do serviço fino do Estágio 2 — Seleção da campanha (task 3.1).

Cobrem a orquestração de :class:`app.services.stix.estagio2_service.Estagio2Service`
que reusa o :class:`app.services.stix.stix_service.StixService` (listagem das
campanhas do bundle, marcando ``curada``/``slug_curado``/``selecionavel``) e o
:class:`app.services.stage.stage_service.StageService`
(``_build_campanha_stix`` para a identidade/aliases e ``run_stage1`` para as
técnicas associadas, extraídas do DAG curado):

* ``listar_campanhas`` marca as campanhas curadas como ``selecionavel`` e as
  demais como não selecionáveis (Req. 6.2, 6.5);
* ``selecionar`` extrai a identidade + técnicas e conclui o Estágio quando a
  extração tem sucesso (Req. 6.3, 6.4, 6.6);
* ``selecionar`` mantém o Estágio não concluído com a causa reportada quando a
  extração falha / o slug é inexistente (Req. 6.7).

Isolamento: usa os artefatos curados REAIS do repositório (``sticks/data``) para
o DAG do Estágio 2 e um bundle STIX temporário (``data_dir`` isolado) para a
listagem/identidade — nenhuma rede, nenhum ``sticks/data/stix`` real tocado. A
persistência corre num SQLite efêmero por teste.

_Requisitos: 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_
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
from app.services.case_service import CaseService
from app.services.stage.stage_service import StageService
from app.services.stix.estagio2_service import (
    CampanhaListada,
    Estagio2Service,
    ResultadoSelecaoCampanha,
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
    """Bundle STIX mínimo com a campanha curada ShadowRay (C0045) + uma não curada."""
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
    """A ``StixService`` apontando para um ``data_dir`` isolado com o bundle 18."""
    stix = StixService(data_dir=tmp_path)
    caminho = stix.base_path(VERSAO)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(_bundle_com_shadowray()), encoding="utf-8")
    return stix


def _service(session: Session, stix_tmp: StixService) -> Estagio2Service:
    """Monta o Estágio 2 reusando os dados curados reais + o bundle temporário."""
    stage = StageService(
        session,
        case_service=CaseService(data_dir=_real_data_dir()),
        stix_service=stix_tmp,
    )
    return Estagio2Service(stage)


# ---------------------------------------------------------------------------
# listar_campanhas — Req. 6.2, 6.5
# ---------------------------------------------------------------------------


def test_listar_campanhas_marca_curada_selecionavel(
    session: Session, stix_tmp: StixService
) -> None:
    """A campanha curada fica selecionável; a não curada não (Req. 6.2, 6.5)."""
    service = _service(session, stix_tmp)

    campanhas = service.listar_campanhas(VERSAO)

    assert all(isinstance(c, CampanhaListada) for c in campanhas)
    por_slug = {c.slug_curado: c for c in campanhas if c.curada}
    assert "shadowray" in por_slug
    assert por_slug["shadowray"].selecionavel is True

    nao_curadas = [c for c in campanhas if not c.curada]
    assert nao_curadas, "esperava ao menos uma campanha não curada no bundle"
    assert all(c.selecionavel is False for c in nao_curadas)


# ---------------------------------------------------------------------------
# selecionar — sucesso (Req. 6.3, 6.4, 6.6)
# ---------------------------------------------------------------------------


def test_selecionar_curada_extrai_atributos_e_conclui(
    session: Session, stix_tmp: StixService
) -> None:
    """Selecionar a campanha curada extrai identidade + técnicas e conclui.

    _Requisitos: 6.3, 6.4, 6.6_
    """
    data_dir = _real_data_dir()
    if not _case_files_present(data_dir, "shadowray"):
        pytest.skip("Artefatos curados de 'shadowray' ausentes no checkout.")

    service = _service(session, stix_tmp)

    resultado = service.selecionar("shadowray", VERSAO)

    assert isinstance(resultado, ResultadoSelecaoCampanha)
    assert resultado.concluido is True
    assert resultado.mensagem_erro is None
    assert resultado.etapa_falha is None

    # Identidade/aliases vieram do objeto campaign do bundle (encontrada).
    assert resultado.identidade is not None
    assert resultado.identidade.encontrada is True
    assert resultado.identidade.external_id == "C0045"
    assert "Shadow Ray" in resultado.identidade.aliases

    # Técnicas associadas extraídas do DAG curado (Saída do Estágio 2).
    assert resultado.tecnicas, "esperava técnicas extraídas do DAG curado"
    assert all(t.technique_id for t in resultado.tecnicas)


# ---------------------------------------------------------------------------
# selecionar — falha de extração (Req. 6.7)
# ---------------------------------------------------------------------------


def test_selecionar_slug_inexistente_nao_conclui_com_causa(
    session: Session, stix_tmp: StixService
) -> None:
    """Um caso sem DAG estrutural mantém o Estágio não concluído com a causa.

    Usa um ``CaseService`` apontando para um ``data_dir`` vazio: o DAG curado do
    caso não existe, então ``run_stage1`` reporta falha e o Estágio 2 não
    conclui, com a causa reportada (Req. 6.7).
    """
    vazio = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio2_vazio_"))
    try:
        stage = StageService(
            session,
            case_service=CaseService(data_dir=vazio),
            stix_service=stix_tmp,
        )
        service = Estagio2Service(stage)

        resultado = service.selecionar("shadowray", VERSAO)

        assert resultado.concluido is False
        assert resultado.mensagem_erro
        # A identidade continua montada (defensiva), independentemente da falha.
        assert resultado.identidade is not None
        assert resultado.tecnicas == []
    finally:
        shutil.rmtree(vazio, ignore_errors=True)
