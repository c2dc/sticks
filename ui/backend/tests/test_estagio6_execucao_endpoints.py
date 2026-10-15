"""Endpoint tests for the Estágio 6 (Execução + relatório) REST surface (task 8.5).

Cobre os três endpoints novos do esquema de seis Estágios adicionados em
``app/api/casos.py``:

* ``POST /api/casos/{caso}/estagio/6/execucao/preview`` — a lista completa de
  comandos concretos com o container-alvo de cada e a sinalização de destino
  externo, **sem executar** (Req. 13.6, 13.7).
* ``POST /api/casos/{caso}/estagio/6/execucao`` — confirmação + execução com os
  quatro gates do ``OperationRunner``, mapeando o resultado tipado para HTTP:
    - ``confirmado=false`` → **200** nada iniciou (``nao_confirmada``, Req. 13.2);
    - contêiner-alvo não isolado → **409** identificando o contêiner (Req. 14.2);
    - Caldera silencioso em 10s → **503** (Req. 10.1);
    - caminho feliz (interno + isolado + Caldera saudável) → **200** com execução
      concluída, relatório e desprovisionamento (Req. 10.7, 10.8, 12.1).
* ``GET /api/casos/{caso}/relatorio`` — o relatório consolidado + métricas de
  suficiência procedural derivadas dos resultados por Ability persistidos + o DAG
  curado (Req. 15.7).

Tudo externo é *mockado* (sem Caldera nem Docker reais), reusando o mesmo molde de
``test_emulacao_endpoints.py``:
- a Base_de_Dados é um SQLite temporário por módulo, ligado via ``get_db``;
- o Caldera é um :class:`CalderaClient` real sobre :class:`httpx.MockTransport`
  (saudável ou com timeout), injetado via ``get_caldera_client`` e no provisionador;
- o isolamento é um verificador *fake* injetado no runner (via ``get_runner_factory``)
  e um inspector *fake* no provisionador (via ``get_provisionador``).

Usa o Caso curado real ``shadowray`` (11 Abilities, **sem** destino externo), de
modo que o gate de contenção passe e os gates de isolamento/disponibilidade possam
ser exercitados isoladamente.

_Requisitos: 13.6, 13.7, 13.2, 14.2, 10.1, 10.7, 10.8, 12.1, 15.7_
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
from sqlalchemy.orm import Session, sessionmaker

from app.api.casos import get_provisionador
from app.api.emulacao import get_caldera_client, get_runner_factory
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.services.audit import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    ADVERSARIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
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


_INTERNAL_NET = "caldera-internal-network"


class _IsolatedInspector:
    """Permissive read-only inspector: every container is on an internal net.

    Used only to make the provisioner's own ``inspect_isolation`` pass during
    ``provisionar`` (no Docker daemon). The *runner*'s isolation gate is driven
    separately by the injected ``isolation_verifier``.
    """

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        return ContainerNetworkInfo(
            name=container, network_names=(_INTERNAL_NET,), dns=()
        )

    def get_network_info(self, network_name: str) -> NetworkInfo:
        return NetworkInfo(name=network_name, internal=True, subnets=())


_SLUG = "shadowray"  # real curated case (no external destinations)


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
            200, json={"id": "op-test", "name": "run", "state": "running"}
        )
    if method == "GET" and path == f"{OPERATIONS_PATH}/op-test":
        return httpx.Response(
            200,
            json={
                "id": "op-test",
                "state": "finished",
                "chain": [],
            },
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
    """Fake isolation verifier: every requested container FAILS isolation."""
    verdicts = tuple(
        ContainerIsolationVerdict(
            container=name,
            isolated=False,
            reasons=("conectado à local-network (bridge)", "dns externo 8.8.8.8"),
        )
        for name in containers
    )
    return IsolationResult(passed=False, verdicts=verdicts)


class _FakeCaldera:
    """In-memory Caldera loader for the provisioner (no network)."""

    def load_abilities(self, abilities: list[object]) -> list[object]:
        return list(abilities)

    def load_adversary(self, adversary: object) -> object:
        return adversary


class _FakeComposeExecutor:
    """``ComposeExecutor`` *fake* (no subprocess, no daemon) — ``up``/``down`` ok.

    A etapa ``subir_conteineres`` do pipeline da task 3.2 usa o ``ComposeExecutor``
    injetado; sem este *fake* a conclusão do Estágio 6 (que provisiona antes de
    executar) construiria o ``DockerComposeExecutor`` real e travaria o teste.
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
# Ephemeral module-scoped SQLite + app fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_estagio6_")
    db_path = Path(tmp_dir) / "estagio6.db"
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
        # Fake inspector (never consulted — the fake verifier short-circuits
        # isolation) + fake Caldera loader + fakes de compose/estado, so
        # provisionar() touches no subprocess nor daemon (pipeline da task 3.2).
        return CuradoriaProvisionadorDeAmbiente(
            inspector=_IsolatedInspector(),
            caldera=_FakeCaldera(),
            compose_executor=_FakeComposeExecutor(),
            state_inspector=_HealthyStateInspector(),
        )

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_caldera_client] = _get_caldera_override
    app.dependency_overrides[get_runner_factory] = _get_runner_factory_override
    app.dependency_overrides[get_provisionador] = _get_provisionador_override
    return TestClient(app)


# ---------------------------------------------------------------------------
# preview — real curated case, no execution (Req. 13.6, 13.7)
# ---------------------------------------------------------------------------


def test_preview_returns_commands_and_containers(
    session_factory: sessionmaker[Session],
) -> None:
    """Preview lists concrete commands + target of each, no execution (Req. 13.6)."""
    client = _make_client(session_factory)
    resp = client.post(f"/api/casos/{_SLUG}/estagio/6/execucao/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["caso_id"] == _SLUG
    # shadowray has no external destinations.
    assert body["tem_externo"] is False
    assert isinstance(body["abilities"], list) and body["abilities"]
    total_commands = 0
    for ability in body["abilities"]:
        for command in ability["comandos"]:
            total_commands += 1
            assert isinstance(command["comando"], str) and command["comando"].strip()
            assert command["alvos"], "each command must present a target"
    assert total_commands > 0


def test_preview_unknown_case_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug on the preview endpoint is a 404."""
    client = _make_client(session_factory)
    resp = client.post("/api/casos/does-not-exist/estagio/6/execucao/preview")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# execução — outcome mapping (Req. 13.2, 14.2, 10.1, 10.7/10.8/12.1)
# ---------------------------------------------------------------------------


def test_execucao_not_confirmed_runs_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    """confirmado=false => nothing started; Stage 6 not concluded (Req. 13.2)."""
    client = _make_client(session_factory)
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/6/execucao", json={"confirmado": False}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["execucao"]["iniciada"] is False
    assert body["execucao"]["resultado"] == "nao_confirmada"
    assert body["execucao"]["estado"] == "nao_iniciada"
    assert body["concluido"] is False
    # The environment is torn down even when nothing ran (Req. 10.8, 12.1).
    assert body["desprovisionamento"]["desprovisionado"] is True


def test_execucao_non_isolated_container_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """A non-isolated target container -> 409 identifying the container (Req. 14.2)."""
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
    assert detail["containers"], "the offending container must be identified"


def test_execucao_caldera_timeout_returns_503(
    session_factory: sessionmaker[Session],
) -> None:
    """Caldera silent within 10s -> 503; nothing started (Req. 10.1)."""
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


def test_execucao_happy_path_concludes_with_report_and_teardown(
    session_factory: sessionmaker[Session],
) -> None:
    """Internal + isolated + healthy Caldera -> 200, concluded, report + teardown.
    _Requisitos: 10.7, 10.8, 12.1_"""
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
    assert body["caso_id"] == _SLUG
    assert body["execucao"]["iniciada"] is True
    assert body["execucao"]["resultado"] == "concluida"
    # A report was consolidated (Req. 10.7) and the environment deprovisioned.
    assert body["relatorio"]["caso_id"] == _SLUG
    assert "metricas" in body["relatorio"]
    assert body["desprovisionamento"]["desprovisionado"] is True
    assert body["relatorio"]["ambiente_desprovisionado"] is True
    # Execution concluded, env torn down, report persisted -> Stage 6 concluded.
    assert body["concluido"] is True


# ---------------------------------------------------------------------------
# relatório — métricas derivadas dos resultados persistidos + DAG (Req. 15.7)
# ---------------------------------------------------------------------------


def test_relatorio_shape_for_case_without_execution(
    session_factory: sessionmaker[Session],
) -> None:
    """A case with no execution yet returns a reportable, zeroed report (Req. 15.7)."""
    client = _make_client(session_factory)
    # Use a different real curated slug that this module never executes, so the
    # persisted results are empty regardless of other tests in the module.
    resp = client.get("/api/casos/c0010/relatorio")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == "c0010"
    metricas = body["metricas"]
    # The DAG defines the technique universe (denominator); none succeeded.
    assert metricas["total_tecnicas_dag"] >= 0
    assert metricas["tecnicas_sucesso"] == 0
    assert metricas["taxa_sucesso"] == 0.0
    assert metricas["veredito_sucesso"] is False
    assert body["ambiente_desprovisionado"] is False


def test_relatorio_unknown_case_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug on the relatorio endpoint is a 404."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos/does-not-exist/relatorio")
    assert resp.status_code == 404, resp.text
