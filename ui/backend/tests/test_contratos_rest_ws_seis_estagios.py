"""Integration tests of the six-stage REST contracts (1..6) and the WS channel.

Esta suíte consolida a cobertura de **contrato** do pipeline de **seis** Estágios
no nível HTTP/WS, exercitando o conjunto de endpoints que o wizard consome, com
tudo externo *mockado* (sem Caldera nem Docker reais). Ela complementa — sem
duplicar — as suítes já existentes:

* ``test_api_endpoints.py`` cobre partes da superfície REST (``GET /api/casos``,
  detalhe, reads 1/3/4/6, DAGs, sessão/preferências/auditoria, fluxo de emulação);
* ``test_estagio6_execucao_endpoints.py`` cobre em profundidade o preview e os
  gates da execução do Estágio 6 (409/503/200) + o relatório;
* ``test_ws_channels.py`` cobre em profundidade o canal ``WS /ws/estagios`` e o de
  operação.

O foco **novo** aqui é um ponto único que afirma, de ponta a ponta, os quatro
contratos pedidos pela tarefa 11.2:

1. ``GET /api/casos`` **e** ``GET /api/casos/{caso}`` retornam **6 Estágios**
   numerados 1..6 (Req. 1.1, 1.2);
2. **cada** ``GET /api/casos/{caso}/estagio/{1..6}`` responde o seu contrato —
   incluindo os reads do Estágio **2** (Seleção da campanha) e **5** (Criação do
   ambiente), que as outras suítes não exercitam diretamente (Req. 1.1, 1.2);
3. ``POST /api/casos/{caso}/estagio/6/execucao`` respeita os gates
   — 409 (contenção/isolamento), 503 (Caldera indisponível), 200 (não-confirmada)
   — (Req. 10.2, 13.6);
4. o canal ``WS /ws/estagios/{caso}`` aceita os Estágios 1..6 (Req. 1.2).

Nota de escopo (i18n): a **paridade i18n pt-BR/en** ("Estágio"/"Stage") é uma
preocupação do **frontend** — coberta pelo property test de paridade de chaves
(tarefa 14.7) sobre os bundles i18next. Não há strings i18n nos contratos REST
do backend (que respondem dados tipados, não rótulos traduzidos), então esta
suíte de backend não a reafirma.

Tudo externo é *mockado* (sem rede), reusando o molde das suítes irmãs:
- a Base_de_Dados é um SQLite temporário por módulo, ligado via ``get_db``;
- o Caldera é um :class:`CalderaClient` real sobre :class:`httpx.MockTransport`
  (saudável ou com timeout), injetado via ``get_caldera_client`` e no provisionador;
- o isolamento é um verificador *fake* no runner (``get_runner_factory``) e um
  inspector *fake* no provisionador (``get_provisionador``);
- a base STIX do Estágio 1/2 aponta para um ``data_dir`` temporário com um bundle
  curado semeado (nenhum egresso de rede).

_Requisitos: 1.1, 1.2, 10.2, 13.6_
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.db.session as db_session_module
import app.services.stage.stage_service as stage_service_module
import app.services.stix.estagio1_service as estagio1_module
from app.api.casos import get_provisionador
from app.api.emulacao import get_caldera_client, get_runner_factory
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import Case, StageRun
from app.models.enums import StageState, TranslationSource
from app.services.audit import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    ADVERSARIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.case_service import CURATED_CASES
from app.services.containment.isolation import (
    ContainerIsolationVerdict,
    ContainerNetworkInfo,
    IsolationResult,
    NetworkInfo,
)
from app.services.environment.compose_executor import ResultadoComando
from app.services.environment.provisionador import CuradoriaProvisionadorDeAmbiente
from app.services.environment.saude import ContainerStateInfo
from app.services.operation.runner import OperationRunner
from app.services.stix.stix_service import VERSAO_CURADA, StixService


# A real curated case present on disk (11 Abilities, no external destinations).
_SLUG = "shadowray"
_INTERNAL_NET = "caldera-internal-network"


# ---------------------------------------------------------------------------
# Seeded curated STIX bundle (no network egress) — one ``campaign`` object that
# resolves to the ``shadowray`` curated slug (external_id C0045 / name ShadowRay)
# so the Estágio-2 listing marks it selecionável (Req. 6.2, 6.5).
# ---------------------------------------------------------------------------


_CURATED_BUNDLE = {
    "type": "bundle",
    "id": "bundle--seis-estagios",
    "objects": [
        {
            "type": "campaign",
            "id": "campaign--shadowray",
            "name": "ShadowRay",
            "description": "Campanha curada semeada para o teste de contrato.",
            "external_references": [
                {"source_name": "mitre-attack", "external_id": "C0045"}
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Mocked Caldera transports (no real network).
# ---------------------------------------------------------------------------


def _healthy_router(request: httpx.Request) -> httpx.Response:
    """A MockTransport handler serving a healthy Caldera chain."""
    method = request.method.upper()
    path = request.url.path
    if method == "GET" and path == HEALTH_PATH:
        return httpx.Response(200, json={"status": "ok"})
    if method == "POST" and path == ABILITIES_PATH:
        return httpx.Response(200, json=json.loads(request.content.decode("utf-8")))
    if method == "POST" and path == ADVERSARIES_PATH:
        return httpx.Response(200, json=json.loads(request.content.decode("utf-8")))
    if method == "POST" and path == OPERATIONS_PATH:
        return httpx.Response(
            200, json={"id": "op-contract", "name": "run", "state": "running"}
        )
    if method == "GET" and path == f"{OPERATIONS_PATH}/op-contract":
        return httpx.Response(
            200, json={"id": "op-contract", "state": "finished", "chain": []}
        )
    raise AssertionError(f"unexpected request: {method} {path}")


def _timeout_router(request: httpx.Request) -> httpx.Response:
    """A MockTransport handler that always times out (Caldera silent, Req. 10.1)."""
    raise httpx.TimeoutException("simulated 10s timeout", request=request)


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """Fake isolation verifier: every requested container is isolated."""
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


def _not_isolated_verifier(
    containers: list[str], _inspector: object
) -> IsolationResult:
    """Fake isolation verifier: every requested container FAILS isolation (Req. 14.2)."""
    verdicts = tuple(
        ContainerIsolationVerdict(
            container=name,
            isolated=False,
            reasons=("conectado à local-network (bridge)", "dns externo 8.8.8.8"),
        )
        for name in containers
    )
    return IsolationResult(passed=False, verdicts=verdicts)


class _IsolatedInspector:
    """Permissive read-only inspector: every container is on an internal net."""

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        return ContainerNetworkInfo(
            name=container, network_names=(_INTERNAL_NET,), dns=()
        )

    def get_network_info(self, network_name: str) -> NetworkInfo:
        return NetworkInfo(name=network_name, internal=True, subnets=())


class _FakeCaldera:
    """In-memory Caldera loader for the provisioner (no network)."""

    def load_abilities(self, abilities: list[object]) -> list[object]:
        return list(abilities)

    def load_adversary(self, adversary: object) -> object:
        return adversary


class _FakeComposeExecutor:
    """``ComposeExecutor`` *fake* (no subprocess, no daemon) — ``up``/``down`` ok.

    A etapa ``subir_conteineres`` do pipeline da task 3.2 usa o ``ComposeExecutor``
    injetado; sem este *fake* o contrato do Estágio 5 construiria o
    ``DockerComposeExecutor`` real e travaria o teste.
    """

    def subir(self, compose_file: str, *, on_log: object = None) -> ResultadoComando:
        return ResultadoComando(codigo=0, linhas=[])

    def derrubar(self, compose_file: str, *, on_log: object = None) -> ResultadoComando:
        return ResultadoComando(codigo=0, linhas=[])


class _HealthyStateInspector:
    """``ContainerStateInspector`` *fake* — todo contêiner-alvo running+healthy."""

    def get_container_state(self, container: str) -> ContainerStateInfo:
        return ContainerStateInfo(name=container, running=True, health="healthy")


# ---------------------------------------------------------------------------
# Ephemeral SQLite + seeded STIX data dir fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stix_data_dir() -> Iterator[Path]:
    """A temp STIX data dir seeded with the curated bundle (no network)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_contratos_stix_"))
    base = StixService(data_dir=tmp_dir).base_path(VERSAO_CURADA)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(json.dumps(_CURATED_BUNDLE), encoding="utf-8")
    try:
        yield tmp_dir
    finally:
        for child in tmp_dir.rglob("*"):
            if child.is_file():
                child.unlink()
        for child in sorted(tmp_dir.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
        tmp_dir.rmdir()


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_contratos_")
    db_path = Path(tmp_dir) / "contratos.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine: Engine = create_db_engine(settings_obj)
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


@pytest.fixture()
def pin_stix_to_temp_dir(
    stix_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point every ``StixService`` the stage path builds at the seeded temp dir.

    The Estágio-1/2 reads build a ``StixService`` with the default data dir
    (``<repo>/sticks/data/stix``), where nothing is downloaded by default
    (Req. 5.4). Pinning the two construction sites — ``Estagio1Service``'s module
    symbol and the ``StageService``'s module symbol — to a factory bound to the
    seeded temp dir makes the bundle visible with **no network egress**.
    """
    monkeypatch.setattr(
        estagio1_module,
        "StixService",
        lambda *a, **k: StixService(data_dir=stix_data_dir),
    )
    monkeypatch.setattr(
        stage_service_module,
        "StixService",
        lambda *a, **k: StixService(data_dir=stix_data_dir),
    )


def _make_client(
    session_factory: sessionmaker[Session],
    *,
    transport: httpx.MockTransport | None = None,
    isolation_verifier=_isolated_verifier,
) -> TestClient:
    """Build a TestClient with DB, Caldera, runner and provisioner mocked."""
    transport = transport or httpx.MockTransport(_healthy_router)
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def _get_caldera_override() -> Iterator[CalderaClient]:
        client = CalderaClient(transport=transport)
        try:
            yield client
        finally:
            client.close()

    def _get_runner_factory_override():
        client = CalderaClient(transport=transport)

        def _factory(session: Session) -> OperationRunner:
            return OperationRunner(
                session=session,
                caldera=client,
                audit_logger=AuditLogger(session),
                isolation_verifier=isolation_verifier,
            )

        return _factory

    def _get_provisionador_override() -> CuradoriaProvisionadorDeAmbiente:
        return CuradoriaProvisionadorDeAmbiente(
            inspector=_IsolatedInspector(),
            caldera=_FakeCaldera(),
            # Fakes de compose/estado: ``provisionar`` sem subprocess nem daemon
            # (pipeline observável da task 3.2 sobe os contêineres e confirma a
            # saúde antes de carregar as Abilities e verificar o isolamento).
            compose_executor=_FakeComposeExecutor(),
            state_inspector=_HealthyStateInspector(),
        )

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_caldera_client] = _get_caldera_override
    app.dependency_overrides[get_runner_factory] = _get_runner_factory_override
    app.dependency_overrides[get_provisionador] = _get_provisionador_override
    return TestClient(app)


# ===========================================================================
# (1) GET /api/casos e GET /api/casos/{caso} retornam 6 Estágios (Req. 1.1, 1.2)
# ===========================================================================


def test_listar_casos_retorna_seis_estagios_por_caso(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/casos`` lista os 8 casos, cada um com 6 Estágios 1..6 (Req. 1.1, 1.2)."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert {c["id"] for c in body["casos"]} == set(CURATED_CASES)
    for caso in body["casos"]:
        assert caso["erro"] is None, caso
        assert caso["estados"] is not None
        numeros = [e["estagio"] for e in caso["estados"]["estagios"]]
        assert numeros == [1, 2, 3, 4, 5, 6]


def test_detalhar_caso_retorna_seis_estagios(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/casos/{caso}`` traz o estado dos 6 Estágios 1..6 (Req. 1.1, 1.2)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["id"] == _SLUG
    numeros = [e["estagio"] for e in body["estados"]["estagios"]]
    assert numeros == [1, 2, 3, 4, 5, 6]


# ===========================================================================
# (2) Cada GET .../estagio/{1..6} responde o seu contrato (Req. 1.1, 1.2)
# ===========================================================================


def test_estagio1_contrato_dashboard_do_bundle(
    session_factory: sessionmaker[Session],
    pin_stix_to_temp_dir: None,
) -> None:
    """Estágio 1 (Seleção da base STIX): dashboard do bundle + veredito (Req. 5.7, 5.8)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versao"] == VERSAO_CURADA
    assert body["bundle_em_disco"] is True
    assert body["dashboard"] is not None
    assert body["concluido"] is True


def test_estagio2_contrato_selecao_da_campanha(
    session_factory: sessionmaker[Session],
    pin_stix_to_temp_dir: None,
) -> None:
    """Estágio 2 (Seleção da campanha): lista do bundle + visão da seleção (Req. 6.2, 6.4).

    Read-only: lista as campanhas do bundle semeado (a curada ``ShadowRay`` é
    marcada ``selecionavel``) e expõe a visão read-only da seleção corrente —
    abrir a tela nunca conclui o Estágio.
    """
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/2")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["caso_id"] == _SLUG
    assert body["versao"] == VERSAO_CURADA
    assert isinstance(body["campanhas"], list) and body["campanhas"]
    curada = next(c for c in body["campanhas"] if c["slug_curado"] == _SLUG)
    assert curada["curada"] is True
    assert curada["selecionavel"] is True
    # Visão read-only da seleção corrente.
    assert body["selecao"]["caso_id"] == _SLUG
    assert "concluido" in body["selecao"]


def test_estagio3_contrato_geracao_do_dag(
    session_factory: sessionmaker[Session],
) -> None:
    """Estágio 3 (Geração do DAG): DagView/Mermaid do DAG curado (Req. 7.2, 7.3)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/3")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _SLUG
    assert body["curado"] is True
    assert body["pode_avancar"] is True
    assert len(body["nos"]) > 0
    assert body["mermaid"]


def test_estagio4_contrato_operacionalizacao(
    session_factory: sessionmaker[Session],
) -> None:
    """Estágio 4 (Operacionalização): Abilities ordenadas + ParametrosDeAmbiente (Req. 8.4, 8.7)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/4")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _SLUG
    assert body["abilities"] is not None
    assert body["abilities"]["has_abilities"] is True
    assert len(body["abilities"]["ordering"]) > 0
    assert body["parametros"]["caso_id"] == _SLUG
    assert body["parametros"]["plataforma"] == "conteiner"
    assert body["parametros"]["origem"] == TranslationSource.HUMAN_CURATION.value
    assert body["ambos_produzidos"] is True


def test_estagio5_contrato_criacao_do_ambiente(
    session_factory: sessionmaker[Session],
) -> None:
    """Estágio 5 (Criação do ambiente): topologia + estado de isolamento (Req. 9.6, 9.7).

    Read-only: monta a topologia do laboratório contido (hosts, redes internas,
    Mermaid) e reporta o veredito de isolamento. O estado por Estágio devolvido
    cobre os 6 Estágios 1..6.
    """
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _SLUG
    assert "isolado" in body
    assert isinstance(body["hosts"], list)
    assert isinstance(body["redes"], list)
    assert "mermaid" in body
    numeros = [e["estagio"] for e in body["estados"]["estagios"]]
    assert numeros == [1, 2, 3, 4, 5, 6]


def test_estagio6_contrato_entrada_e_saida_acumulada(
    session_factory: sessionmaker[Session],
) -> None:
    """Estágio 6 (Execução): Entrada (Adversary+Abilities) + Saída acumulada (Req. 10.x)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/6")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entrada"]["caso_id"] == _SLUG
    assert isinstance(body["saida"], list)


def test_cada_estagio_1a6_responde_200_para_caso_valido(
    session_factory: sessionmaker[Session],
    pin_stix_to_temp_dir: None,
) -> None:
    """Todo ``GET .../estagio/{1..6}`` responde 200 para um Caso curado válido (Req. 1.2)."""
    client = _make_client(session_factory)
    for estagio in (1, 2, 3, 4, 5, 6):
        resp = client.get(f"/api/casos/{_SLUG}/estagio/{estagio}")
        assert resp.status_code == 200, (estagio, resp.text)


def test_estagio_1a6_slug_desconhecido_404(
    session_factory: sessionmaker[Session],
) -> None:
    """Slug desconhecido é 404 em cada ``GET .../estagio/{1..6}`` (Req. 1.2)."""
    client = _make_client(session_factory)
    for estagio in (1, 2, 3, 4, 5, 6):
        resp = client.get(f"/api/casos/nao_existe/estagio/{estagio}")
        assert resp.status_code == 404, (estagio, resp.text)


# ===========================================================================
# (3) POST .../estagio/6/execucao respeita os gates: 409 / 503 / 200 (Req. 10.2, 13.6)
# ===========================================================================


def test_execucao_nao_confirmada_retorna_200_sem_executar(
    session_factory: sessionmaker[Session],
) -> None:
    """Gate de confirmação: ``confirmado=false`` → 200, nada iniciou (Req. 13.6)."""
    client = _make_client(session_factory)
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/6/execucao", json={"confirmado": False}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["execucao"]["iniciada"] is False
    assert body["execucao"]["resultado"] == "nao_confirmada"
    assert body["concluido"] is False


def test_execucao_container_nao_isolado_retorna_409(
    session_factory: sessionmaker[Session],
) -> None:
    """Gate de isolamento: contêiner-alvo não isolado → 409 identificando-o (Req. 10.2, 13.6)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_not_isolated_verifier,
    )
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/6/execucao", json={"confirmado": True}
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "isolamento_falhou"
    assert detail["containers"]


def test_execucao_caldera_indisponivel_retorna_503(
    session_factory: sessionmaker[Session],
) -> None:
    """Gate de disponibilidade: Caldera silencioso em 10s → 503, nada iniciou (Req. 10.2)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_timeout_router),
        isolation_verifier=_isolated_verifier,
    )
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/6/execucao", json={"confirmado": True}
    )
    assert resp.status_code == 503, resp.text
    assert "indisponível" in resp.json()["detail"].lower()


def test_execucao_confirmada_isolada_caldera_ok_retorna_200(
    session_factory: sessionmaker[Session],
) -> None:
    """Caminho feliz: confirmada + isolada + Caldera ok → 200 concluída (Req. 10.2, 13.6)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_isolated_verifier,
    )
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/6/execucao", json={"confirmado": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["execucao"]["iniciada"] is True
    assert body["execucao"]["resultado"] == "concluida"
    assert body["concluido"] is True


# ===========================================================================
# (4) WS /ws/estagios/{caso} aceita os Estágios 1..6 (Req. 1.2)
# ===========================================================================


@pytest.fixture()
def ws_client(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """A TestClient whose WS channels resolve the ephemeral DB session factory."""
    monkeypatch.setattr(db_session_module, "SessionLocal", session_factory)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("estagio", [1, 2, 3, 4, 5, 6])
def test_ws_estagios_aceita_1_a_6(
    ws_client: TestClient,
    session_factory: sessionmaker[Session],
    estagio: int,
) -> None:
    """O canal ``WS /ws/estagios`` aceita cada Estágio 1..6 e emite o frame tipado (Req. 1.2)."""
    caso = f"ws-caso-{estagio}"
    session = session_factory()
    try:
        session.add(Case(id=caso, nome=caso))
        session.add(
            StageRun(
                caso_id=caso,
                estagio=estagio,
                estado=StageState.COMPLETED,
                progresso=100,
            )
        )
        session.commit()
    finally:
        session.close()

    with ws_client.websocket_connect(
        f"/ws/estagios/{caso}?intervalo=0&estagio={estagio}"
    ) as ws:
        frame = ws.receive_json()

    assert frame.get("valido") is not False
    assert frame["estagio"] == estagio
    assert frame["estado"] == StageState.COMPLETED.value


@pytest.mark.parametrize("estagio", [0, 7])
def test_ws_estagios_rejeita_fora_de_1_a_6(
    ws_client: TestClient,
    session_factory: sessionmaker[Session],
    estagio: int,
) -> None:
    """Estágios fora de 1..6 (0 e 7) rendem um frame ``invalido`` gracioso (Req. 1.2)."""
    caso = f"ws-caso-oob-{estagio}"
    session = session_factory()
    try:
        session.add(Case(id=caso, nome=caso))
        session.commit()
    finally:
        session.close()

    with ws_client.websocket_connect(
        f"/ws/estagios/{caso}?intervalo=0&estagio={estagio}"
    ) as ws:
        frame = ws.receive_json()

    assert frame["valido"] is False
    assert frame["estagio"] == estagio
