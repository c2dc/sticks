"""Testes de exemplo da seleção de campanha no Estágio 2 (task 3.3).

Complementam os testes de orquestração de ``test_estagio2_service.py`` (task 3.1)
focando especificamente nos exemplos exigidos pela task 3.3, sem duplicá-los:

* **Versão 18 lista as 8 campanhas curadas selecionáveis** — um bundle da versão
  curada contendo exatamente as 8 campanhas curadas (resolvidas via
  ``SLUG_PARA_STIX``) + campanhas não curadas resulta em ``selecionavel is True``
  para exatamente as 8 curadas e ``False`` para as demais (Req. 6.2, 6.5);
* **Selecionar extrai os atributos e conclui** — selecionar cada campanha curada
  (com os artefatos curados REAIS presentes) extrai identidade + técnicas e
  conclui o Estágio (``concluido is True``), sem erro (Req. 6.2, 6.6);
* **Slug inexistente / extração falha mantém não concluído com causa** — tanto um
  caso com DAG curado ausente quanto um slug fora do catálogo mantêm o Estágio
  não concluído com a causa reportada (Req. 6.6, 6.7).

Isolamento: usa os artefatos curados REAIS do repositório (``sticks/data``) para
os DAGs do Estágio 2 e um bundle STIX temporário (``data_dir`` isolado) para a
listagem/identidade — nenhuma rede, nenhum ``sticks/data/stix`` real tocado. A
persistência corre num SQLite efêmero por teste.

_Requisitos: 6.2, 6.5, 6.6, 6.7_
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
from app.services.case_service import CURATED_CASES, CaseService
from app.services.stage.stage_service import StageService
from app.services.stix.estagio2_service import (
    CampanhaListada,
    Estagio2Service,
    ResultadoSelecaoCampanha,
)
from app.services.stix.stix_service import SLUG_PARA_STIX, StixService

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


def _campaign_objeto(slug: str) -> dict:
    """Monta o objeto STIX ``campaign`` de uma campanha curada do catálogo.

    Usa o mapeamento ``SLUG_PARA_STIX`` como fonte da verdade: quando há uma
    ``referencia`` (ex.: ``C0045``) ela é emitida em ``external_references`` (o
    canal de resolução prioritário); do contrário o ``nome_canonico`` resolve a
    campanha pelo nome (caso de ``salesforce_data_exfiltration``, sem código).
    """
    mapeamento = SLUG_PARA_STIX[slug]
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
    """Bundle da versão curada com as 8 campanhas curadas + 2 não curadas."""
    objetos = [_campaign_objeto(slug) for slug in CURATED_CASES]
    objetos.extend(
        [
            {
                "type": "campaign",
                "id": "campaign--nao-curada-1",
                "name": "Campanha Não Curada 1",
                "external_references": [{"external_id": "C9998"}],
            },
            {
                "type": "campaign",
                "id": "campaign--nao-curada-2",
                "name": "Campanha Não Curada 2",
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
    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio2_t33_"))
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
    caminho.write_text(json.dumps(_bundle_com_8_curadas()), encoding="utf-8")
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
# Versão 18 lista as 8 campanhas curadas selecionáveis — Req. 6.2, 6.5
# ---------------------------------------------------------------------------


def test_versao_18_lista_as_8_curadas_selecionaveis(
    session: Session, stix_tmp: StixService
) -> None:
    """A versão 18 marca exatamente as 8 campanhas curadas como selecionáveis.

    As demais campanhas (não curadas) ficam não selecionáveis, pois a extração
    dos atributos depende dos artefatos curados em disco (Req. 6.2, 6.5).
    """
    service = _service(session, stix_tmp)

    campanhas = service.listar_campanhas(VERSAO)

    assert all(isinstance(c, CampanhaListada) for c in campanhas)

    selecionaveis = [c for c in campanhas if c.selecionavel]
    assert len(selecionaveis) == 8, "esperava exatamente as 8 campanhas curadas"
    assert all(c.curada and c.slug_curado is not None for c in selecionaveis)

    # Os slugs selecionáveis cobrem exatamente o catálogo de campanhas curadas.
    slugs_selecionaveis = {c.slug_curado for c in selecionaveis}
    assert slugs_selecionaveis == set(CURATED_CASES)

    # As não curadas permanecem indisponíveis para seleção.
    nao_curadas = [c for c in campanhas if not c.curada]
    assert len(nao_curadas) == 2
    assert all(c.selecionavel is False for c in nao_curadas)


# ---------------------------------------------------------------------------
# Selecionar extrai os atributos e conclui — Req. 6.2, 6.6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(CURATED_CASES))
def test_selecionar_cada_curada_extrai_atributos_e_conclui(
    session: Session, stix_tmp: StixService, slug: str
) -> None:
    """Selecionar qualquer campanha curada extrai identidade + técnicas e conclui.

    Varre as 8 campanhas curadas: com os artefatos curados REAIS presentes, a
    seleção extrai a identidade do bundle e as técnicas do DAG curado, e conclui
    o Estágio sem erro (Req. 6.2, 6.6). Caso um artefato curado esteja ausente no
    checkout, o caso é pulado em vez de falhar espúrio.
    """
    data_dir = _real_data_dir()
    if not _case_files_present(data_dir, slug):
        pytest.skip(f"Artefatos curados de {slug!r} ausentes no checkout.")

    service = _service(session, stix_tmp)

    resultado = service.selecionar(slug, VERSAO)

    assert isinstance(resultado, ResultadoSelecaoCampanha)
    assert resultado.caso_id == slug
    assert resultado.concluido is True
    assert resultado.mensagem_erro is None
    assert resultado.etapa_falha is None

    # Identidade montada a partir do objeto campaign do bundle (encontrada).
    assert resultado.identidade is not None
    assert resultado.identidade.encontrada is True

    # Técnicas associadas extraídas do DAG curado (a Saída do Estágio 2).
    assert resultado.tecnicas, "esperava técnicas extraídas do DAG curado"
    assert all(t.technique_id for t in resultado.tecnicas)


# ---------------------------------------------------------------------------
# Slug inexistente / extração falha mantém não concluído com causa — Req. 6.6, 6.7
# ---------------------------------------------------------------------------


def test_selecionar_curada_sem_dag_nao_conclui_com_causa(
    session: Session, stix_tmp: StixService
) -> None:
    """Uma campanha curada cujo DAG curado está ausente não conclui, com causa.

    Aponta o ``CaseService`` para um ``data_dir`` vazio: a extração estrutural do
    DAG falha, ``run_stage1`` reporta a falha e o Estágio 2 permanece não
    concluído, com a causa reportada e sem técnicas (Req. 6.6, 6.7). A identidade
    continua montada defensivamente.
    """
    vazio = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio2_vazio_t33_"))
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
        assert resultado.etapa_falha is not None
        assert resultado.identidade is not None
        assert resultado.tecnicas == []
    finally:
        shutil.rmtree(vazio, ignore_errors=True)


def test_selecionar_slug_inexistente_nao_conclui_com_causa(
    session: Session, stix_tmp: StixService
) -> None:
    """Um slug fora do catálogo curado mantém o Estágio não concluído, com causa.

    Um slug inexistente não é um caso válido: ``run_stage1`` não encontra o caso
    e reporta a falha no resultado (``estado = erro``), de modo que o Estágio 2
    permanece não concluído com a causa reportada e sem técnicas (Req. 6.6, 6.7).
    A identidade continua montada defensivamente (``encontrada = False``), sem
    levantar — abrir a seleção de um slug inválido nunca derruba o Estágio.
    """
    service = _service(session, stix_tmp)

    resultado = service.selecionar("slug_que_nao_existe", VERSAO)

    assert resultado.concluido is False
    assert resultado.mensagem_erro
    assert resultado.etapa_falha is not None
    assert resultado.tecnicas == []
    assert resultado.identidade is not None
    assert resultado.identidade.encontrada is False
