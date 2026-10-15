"""Contract tests of the Stage-1 endpoints' ``campanha_stix`` block (task 3.2).

No esquema de SEIS Estágios, o Estágio 1 passou a ser a **Seleção da base
STIX**: ``GET /api/casos/{caso}/estagio/1`` delega ao ``Estagio1Service`` e
devolve o resultado tipado ``ResultadoEstagio1`` (bundle em disco + dashboard +
campanhas + veredito), aceitando um ``versao`` opcional (padrão = versão curada).
A modelagem estrutural (DTO histórico ``Stage1*``, cuja Entrada é o objeto STIX
``campaign``) permanece acessível de forma **transitória** em
``POST /api/casos/{caso}/estagio/1/executar`` até as tarefas 3.x a renumerarem
para o Estágio 2.

Estes testes fixam o contrato HTTP:

* **GET (seis Estágios)** — a Seleção da base STIX conclui quando o bundle está
  em disco e o dashboard foi montado; versão desconhecida mantém o Estágio não
  concluído com a causa reportada, sem egresso de rede (Req. 5.7, 5.8, 5.9,
  5.10).
* **POST executar (transitório)** — a Entrada da modelagem **nunca quebra o
  Estágio** (Correctness Property 1 — HTTP 200 em todos os cenários) e **nenhum
  artefato de adversário Caldera vaza para a Entrada** (Property 3):

1. ``versao`` válida com a campanha no bundle → ``encontrada == true`` com os
   campos ricos do objeto ``campaign`` (nome, stix_id, external_id, descrição,
   aliases, datas) e a versão da base.
2. sem ``versao`` → ``encontrada == false`` e ``motivo_indisponivel ==
   "versao_ausente"`` (fallback retrocompatível, sem erro HTTP).
3. ``versao`` válida mas a campanha ausente do bundle → ``encontrada == false``
   e ``motivo_indisponivel == "campanha_nao_encontrada"``, ainda carregando o
   identificador curado (nome + external_id de ``SLUG_PARA_STIX``).

Both GET (read-only) and POST (executar) are covered where sensible.

Isolation
---------
Everything external is a throwaway:

* the database is an ephemeral temp-file SQLite wired into the app via the
  ``get_db`` dependency override (same pattern as ``test_api_endpoints.py``);
* the STIX base is read from a **pytest temp dir**: because the Stage-1
  endpoints construct ``StageService(db, case_service=...)`` directly (no stix
  injection point on the route), we monkeypatch the ``StixService`` symbol that
  ``StageService`` resolves at construction time so it points at ``tmp_path``.
  The real ``sticks/data/stix`` is never touched and there is no network egress.

The curated case fixtures already ship on disk (``shadowray`` is one of the 8
curated cases), so the slug is known to ``CaseService`` and the curated
identifier (nome + ``external_id``) resolves from ``SLUG_PARA_STIX`` on the
fallback paths.

_Requisitos: 5.2, 5.4, 4.2 — Correctness Properties 1, 3_
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.services.stage.stage_service as stage_module
import app.services.stix.estagio1_service as estagio1_module
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.services.stix.stix_service import (
    SLUG_PARA_STIX,
    VERSOES_CONHECIDAS,
    StixService,
)

# A curated slug that ships on disk and has an external_id in SLUG_PARA_STIX.
_SLUG = "shadowray"
_EXTERNAL_ID = SLUG_PARA_STIX[_SLUG]["referencias"][0]  # "C0045"
_NOME_CANONICO = SLUG_PARA_STIX[_SLUG]["nome_canonico"]  # "ShadowRay"
# A valid, known base version — resolves to enterprise-attack-{versao}.json.
_VERSAO = VERSOES_CONHECIDAS[0]

# A curated adversary file path shape that must NEVER appear in the Entrada
# (Property 3 — no Caldera artifact leaks into the Stage-1 Entrada).
_CALDERA_FRAGMENT = "data/caldera_adversaries"


# ---------------------------------------------------------------------------
# Ephemeral temp-file SQLite + app client (mirrors test_api_endpoints.py).
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema, torn down afterward."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_estagio1_campanha_")
    db_path = Path(tmp_dir) / "estagio1_campanha.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
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
    try:
        yield factory
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


def _make_client(session_factory: sessionmaker[Session]) -> TestClient:
    """Build a TestClient with the DB wired to the ephemeral SQLite factory."""
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a minimal-but-valid STIX bundle (``{"objects": [...]}``) to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "bundle", "objects": objects}), encoding="utf-8"
    )


@pytest.fixture()
def stix_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the StageService's StixService to a pytest temp data dir.

    The Stage-1 endpoints build ``StageService(db, case_service=...)`` directly,
    so there is no stix injection point on the route. ``StageService.__init__``
    falls back to ``StixService()`` resolved from the ``stage_service`` module;
    patching that symbol with a ``data_dir``-bound factory makes every bundle
    read happen inside ``tmp_path`` — the real ``sticks/data/stix`` is untouched
    and there is no network egress.
    """

    def _factory(*args: object, **kwargs: object) -> StixService:
        return StixService(data_dir=tmp_path)

    monkeypatch.setattr(stage_module, "StixService", _factory)
    return tmp_path


@pytest.fixture()
def estagio1_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the six-stage ``GET .../estagio/1`` handler to a temp data dir.

    No esquema de seis Estágios, ``GET /api/casos/{caso}/estagio/1`` delega a
    :class:`~app.services.stix.estagio1_service.Estagio1Service`, que (sem
    injeção na rota) constrói um ``StixService()`` resolvido no módulo
    ``estagio1_service``. Patching that symbol with a ``data_dir``-bound factory
    makes the bundle read + dashboard happen inside ``tmp_path`` — the real
    ``sticks/data/stix`` is untouched and there is no network egress (nenhum
    bundle em disco salvo os que o teste escreve).
    """

    def _factory(*args: object, **kwargs: object) -> StixService:
        return StixService(data_dir=tmp_path)

    monkeypatch.setattr(estagio1_module, "StixService", _factory)
    return tmp_path


# A rich ``campaign`` object resolving to the ``shadowray`` slug (via C0045).
def _campanha_encontrada() -> dict:
    return {
        "type": "campaign",
        "id": "campaign--c0045-0000-0000-0000-000000000000",
        "name": _NOME_CANONICO,
        "description": "ataques contra clusters Ray expostos.",
        "aliases": [_NOME_CANONICO, "Ray cluster abuse"],
        "first_seen": "2023-09-05T00:00:00.000Z",
        "last_seen": "2024-03-01T00:00:00.000Z",
        "external_references": [
            {"source_name": "mitre-attack", "external_id": _EXTERNAL_ID}
        ],
    }


def _assert_sem_artefato_caldera(entrada: dict) -> None:
    """Property 3 — no Caldera adversary path leaks into the STIX campaign block.

    The Stage-1 Entrada's STIX identity lives in ``campanha_stix`` — that block
    must never carry a Caldera adversary path (``data/caldera_adversaries/*.yml``).
    The legacy ``dag_file``/``stix_reference`` fields are deliberately preserved
    in the DTO for contract compatibility (per the design) and are dropped on the
    frontend side (tasks 5.1/7.1); they are therefore out of scope for this
    API-level guarantee.
    """
    bloco = entrada["campanha_stix"]
    serializado = json.dumps(bloco)
    assert _CALDERA_FRAGMENT not in serializado, bloco


# ===========================================================================
# 1) versao válida COM a campanha no bundle → encontrada=true + campos ricos
# ===========================================================================


def test_get_estagio1_versao_valida_dashboard_concluido(
    session_factory: sessionmaker[Session], estagio1_data_dir: Path
) -> None:
    """``GET .../estagio/1?versao=`` com o bundle em disco → HTTP 200 e o
    resultado do Estágio 1 (Seleção da base STIX) ``concluido=True`` com o
    dashboard do bundle e as campanhas (seis Estágios, Req. 5.7, 5.8, 5.10)."""
    campanha = _campanha_encontrada()
    _write_bundle(
        StixService(data_dir=estagio1_data_dir).base_path(_VERSAO), [campanha]
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/1", params={"versao": _VERSAO})

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Veredito + Saída do Estágio 1 (bundle em disco + dashboard + campanhas).
    assert body["versao"] == _VERSAO
    assert body["concluido"] is True
    assert body["bundle_em_disco"] is True
    assert body["mensagem_erro"] is None
    assert body["dashboard"] is not None
    assert body["dashboard"]["versao"] == _VERSAO
    # The written campaign is listed among the bundle's campanhas (Entrada do E2).
    nomes = {c["nome"] for c in body["campanhas"]}
    assert _NOME_CANONICO in nomes


def test_post_estagio1_executar_versao_valida_campanha_encontrada(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../estagio/1/executar?versao=`` com a campanha no bundle → HTTP
    200 e ``campanha_stix.encontrada=true``; a execução estrutural segue (a
    Entrada é informativa, Property 1)."""
    campanha = _campanha_encontrada()
    _write_bundle(StixService(data_dir=stix_data_dir).base_path(_VERSAO), [campanha])

    client = _make_client(session_factory)
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/1/executar", params={"versao": _VERSAO}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _SLUG
    bloco = body["entrada"]["campanha_stix"]
    assert bloco["encontrada"] is True
    assert bloco["stix_id"] == campanha["id"]
    assert bloco["external_id"] == _EXTERNAL_ID
    assert bloco["versao_base"] == _VERSAO
    _assert_sem_artefato_caldera(body["entrada"])


# ===========================================================================
# 2) sem versao → encontrada=false / motivo_indisponivel="versao_ausente"
# ===========================================================================


def test_get_estagio1_sem_versao_usa_versao_curada(
    session_factory: sessionmaker[Session], estagio1_data_dir: Path
) -> None:
    """``GET .../estagio/1`` sem ``versao`` → HTTP 200 e o Estágio 1 parte da
    versão curada (``VERSAO_CURADA``) como padrão do fluxo (seis Estágios, Req.
    5.5, 5.6). Com o bundle curado em disco, conclui sem egresso de rede."""
    from app.services.stix.stix_service import VERSAO_CURADA

    # Bundle curado em disco → nenhum download (sem rede); conclui o Estágio 1.
    _write_bundle(
        StixService(data_dir=estagio1_data_dir).base_path(VERSAO_CURADA),
        [_campanha_encontrada()],
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/1")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versao"] == VERSAO_CURADA
    assert body["curada"] is True
    assert body["concluido"] is True
    assert body["bundle_em_disco"] is True
    assert body["dashboard"] is not None


def test_post_estagio1_executar_sem_versao_fallback_versao_ausente(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../estagio/1/executar`` sem ``versao`` → HTTP 200 e o fallback
    ``versao_ausente`` (a execução nunca quebra por falta de versão)."""
    client = _make_client(session_factory)
    resp = client.post(f"/api/casos/{_SLUG}/estagio/1/executar")

    assert resp.status_code == 200, resp.text
    bloco = resp.json()["entrada"]["campanha_stix"]
    assert bloco["encontrada"] is False
    assert bloco["motivo_indisponivel"] == "versao_ausente"
    assert bloco["versao_base"] is None


# ===========================================================================
# 3) versao válida SEM a campanha no bundle → campanha_nao_encontrada
# ===========================================================================


def test_get_estagio1_versao_desconhecida_nao_conclui_com_causa(
    session_factory: sessionmaker[Session], estagio1_data_dir: Path
) -> None:
    """``GET .../estagio/1?versao=`` com versão desconhecida → HTTP 200 e o
    Estágio 1 **não concluído** com a causa reportada, sem egresso de rede
    (``versao`` é validada contra ``VERSOES_CONHECIDAS`` antes de tocar rede —
    seis Estágios, Req. 5.9). O serviço nunca levanta: devolve o resultado
    tipado reportável."""
    versao_desconhecida = "999.0"

    client = _make_client(session_factory)
    resp = client.get(
        f"/api/casos/{_SLUG}/estagio/1", params={"versao": versao_desconhecida}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versao"] == versao_desconhecida
    assert body["concluido"] is False
    assert body["bundle_em_disco"] is False
    assert body["dashboard"] is None
    assert body["campanhas"] == []
    assert body["mensagem_erro"]  # a causa reportada ao Pesquisador


def test_post_estagio1_executar_versao_valida_campanha_ausente(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../estagio/1/executar?versao=`` com a base legível mas SEM a
    campanha → HTTP 200 e ``campanha_nao_encontrada`` (execução não quebra)."""
    _write_bundle(StixService(data_dir=stix_data_dir).base_path(_VERSAO), [])

    client = _make_client(session_factory)
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/1/executar", params={"versao": _VERSAO}
    )

    assert resp.status_code == 200, resp.text
    bloco = resp.json()["entrada"]["campanha_stix"]
    assert bloco["encontrada"] is False
    assert bloco["motivo_indisponivel"] == "campanha_nao_encontrada"
    assert bloco["versao_base"] == _VERSAO
    assert bloco["nome"] == _NOME_CANONICO
    assert bloco["external_id"] == _EXTERNAL_ID


# ===========================================================================
# Property 1 — no HTTP error in ANY of the three scenarios (status 200 always).
# ===========================================================================


def test_estagio1_nunca_quebra_http_em_nenhum_cenario(
    session_factory: sessionmaker[Session],
    stix_data_dir: Path,
    estagio1_data_dir: Path,
) -> None:
    """A rota do Estágio 1 nunca quebra o HTTP (status 200 sempre).

    Cobre o GET da Seleção da base STIX (seis Estágios) e o POST
    ``.../estagio/1/executar`` (modelagem transitória) nos cenários relevantes;
    todos devolvem 200, de modo que a tela sempre recebe um resultado tipado
    reportável (Req. 5.9) e a Entrada da modelagem nunca quebra o Estágio."""
    from app.services.stix.stix_service import VERSAO_CURADA

    base_get = StixService(data_dir=estagio1_data_dir).base_path(_VERSAO)
    base_curada = StixService(data_dir=estagio1_data_dir).base_path(VERSAO_CURADA)
    base_post = StixService(data_dir=stix_data_dir).base_path(_VERSAO)
    client = _make_client(session_factory)

    # Cenário A: GET com versão válida e bundle em disco → 200.
    _write_bundle(base_get, [_campanha_encontrada()])
    assert (
        client.get(
            f"/api/casos/{_SLUG}/estagio/1", params={"versao": _VERSAO}
        ).status_code
        == 200
    )

    # Cenário B: GET sem versão → usa a versão curada; bundle curado em disco → 200.
    _write_bundle(base_curada, [_campanha_encontrada()])
    assert client.get(f"/api/casos/{_SLUG}/estagio/1").status_code == 200

    # Cenário C: GET com versão desconhecida → 200 (não concluído, sem rede).
    assert (
        client.get(
            f"/api/casos/{_SLUG}/estagio/1", params={"versao": "999.0"}
        ).status_code
        == 200
    )

    # POST executar (modelagem transitória): válida+campanha, sem versão, sem campanha.
    _write_bundle(base_post, [_campanha_encontrada()])
    assert (
        client.post(
            f"/api/casos/{_SLUG}/estagio/1/executar", params={"versao": _VERSAO}
        ).status_code
        == 200
    )
    assert client.post(f"/api/casos/{_SLUG}/estagio/1/executar").status_code == 200
    _write_bundle(base_post, [])
    assert (
        client.post(
            f"/api/casos/{_SLUG}/estagio/1/executar", params={"versao": _VERSAO}
        ).status_code
        == 200
    )
