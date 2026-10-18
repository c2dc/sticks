"""Endpoint tests for the Estágio 5 (Criação do environment) provision surface (task 7.3).

Cobre o endpoint novo do esquema de seis Estágios adicionado em
``app/api/campaigns.py``:

* ``POST /api/campaigns/{campaign}/stage/5/provision`` — provisiona/verifica o
  environment curated reusando :class:`CurationEnvironmentProvisioner`: resolve os
  contêineres-target dos :class:`EnvironmentParameters` (Saída do Estágio 4),
  **carrega as Abilities ordenadas** no Caldera na ordem do DAG (Req. 9.5) e
  **verifica o isolation** read-only ANTES de complete (Req. 9.6). A conclusão
  é condicional (Req. 9.7, 9.9):
    - environment isolated + Abilities carregadas → Estágio 5 **concluído**
      (``completed=true``), com o :class:`ProvisionedEnvironment` exposto (Req. 9.8);
    - contêiner-target **não** isolated → Estágio 5 **não concluído**
      (``completed=false``), com o(s) contêiner(es) e a razão reportados em
      ``motivo``, e o resumo de isolation no corpo (Req. 9.7).

Tudo external é *mockado* (sem Caldera nem Docker reais), reusando o mesmo molde
de ``test_stage6_execution_endpoints.py``:
- a Base_de_Dados é um SQLite temporário por módulo, ligado via ``get_db``;
- o isolation é um inspector/verificador *fake* no provisioner (via
  ``get_provisioner``);
- o Caldera é um loader *fake* in-memory no provisioner.

Usa o campaign curated real ``shadowray`` (11 Abilities), cujo Estágio 4
(Operacionalização) deriva os :class:`EnvironmentParameters` de forma pura (sem
network/AI), so the provisioning is exercisable end to end.

_Requirements: 9.5, 9.6, 9.7, 9.8, 9.9_
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.campaigns import get_provisioner
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.services.containment.isolation import (
    ContainerIsolationVerdict,
    ContainerNetworkInfo,
    IsolationResult,
    NetworkInfo,
)
from app.services.environment.compose_executor import CommandResult
from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.environment.health import ContainerStateInfo


_INTERNAL_NET = "caldera-internal-network"
_SLUG = "shadowray"  # real curated case


# ---------------------------------------------------------------------------
# Fake isolation inspectors (no Docker daemon) + fake Caldera loader.
# ---------------------------------------------------------------------------


class _IsolatedInspector:
    """Read-only inspector: every container is attached to an internal network."""

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        return ContainerNetworkInfo(
            name=container, network_names=(_INTERNAL_NET,), dns=()
        )

    def get_network_info(self, network_name: str) -> NetworkInfo:
        return NetworkInfo(name=network_name, internal=True, subnets=())


class _NotIsolatedInspector:
    """Read-only inspector: every container is on an external bridge w/ external DNS."""

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        return ContainerNetworkInfo(
            name=container, network_names=("bridge",), dns=("8.8.8.8",)
        )

    def get_network_info(self, network_name: str) -> NetworkInfo:
        # A non-internal bridge network.
        return NetworkInfo(name=network_name, internal=False, subnets=())


class _FakeCaldera:
    """In-memory Caldera loader for the provisioner (no network)."""

    def load_abilities(self, abilities: list[object]) -> list[object]:
        return list(abilities)

    def load_adversary(self, adversary: object) -> object:
        return adversary


class _FakeCalderaThatFails:
    """Caldera loader that raises — the Abilities load fails (fail-closed)."""

    def load_abilities(self, abilities: list[object]) -> list[object]:
        raise RuntimeError("Caldera indisponível")

    def load_adversary(self, adversary: object) -> object:  # pragma: no cover
        raise RuntimeError("Caldera indisponível")


class _FakeComposeExecutor:
    """``ComposeExecutor`` *fake* (no subprocess, no daemon) — ``up``/``down`` ok.

    A step ``subir_conteineres`` do pipeline (task 3.2) usa o ``ComposeExecutor``
    injetado; sem este *fake* o endpoint construiria o ``DockerComposeExecutor``
    real e invocaria ``docker compose up`` (travaria o teste). ``code=0`` deixa
    o pipeline avançar.
    """

    def up(self, compose_file: str, *, on_log: object = None) -> CommandResult:
        return CommandResult(code=0, lines=[])

    def down(self, compose_file: str, *, on_log: object = None) -> CommandResult:
        return CommandResult(code=0, lines=[])


class _HealthyStateInspector:
    """``ContainerStateInspector`` *fake* — todo contêiner-target running+healthy.

    A step ``aguardar_saude`` (task 3.2) usa o inspector de state injetado; este
    *fake* confirma a saúde sem daemon Docker para o pipeline alcançar a carga de
    Abilities e a verificação de isolation.
    """

    def get_container_state(self, container: str) -> ContainerStateInfo:
        return ContainerStateInfo(name=container, running=True, health="healthy")


# ---------------------------------------------------------------------------
# Ephemeral module-scoped SQLite + app fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_stage5_")
    db_path = Path(tmp_dir) / "stage5.db"
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
    inspector: object,
    caldera: object,
) -> TestClient:
    """Build a TestClient with DB + provisioner (isolation + Caldera) mocked."""
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def _get_provisioner_override() -> CurationEnvironmentProvisioner:
        return CurationEnvironmentProvisioner(
            inspector=inspector,  # type: ignore[arg-type]
            caldera=caldera,  # type: ignore[arg-type]
            # Fakes de compose + state mantêm ``provision`` sem subprocess nem
            # daemon Docker (o pipeline da task 3.2 sobe os contêineres e confirma
            # a saúde antes de carregar as Abilities e verificar o isolation).
            compose_executor=_FakeComposeExecutor(),  # type: ignore[arg-type]
            state_inspector=_HealthyStateInspector(),  # type: ignore[arg-type]
        )

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_provisioner] = _get_provisioner_override
    return TestClient(app)


# ---------------------------------------------------------------------------
# provision — isolated + Abilities loaded => Stage 5 concluded (Req. 9.8, 9.9)
# ---------------------------------------------------------------------------


def test_provision_isolated_and_loaded_concludes_stage5(
    session_factory: sessionmaker[Session],
) -> None:
    """Isolated env + Abilities loaded => 200, concluded, ProvisionedEnvironment exposed.
    _Requirements: 9.5, 9.6, 9.8, 9.9_"""
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.post(f"/api/campaigns/{_SLUG}/stage/5/provision")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    environment = body["environment"]
    assert environment["campaign_id"] == _SLUG
    assert environment["platform"] == "conteiner"
    assert environment["target_containers"], "target containers must be resolved"
    assert environment["loaded_abilities"] is True
    assert environment["isolation"]["approved"] is True
    assert environment["compose_file"]
    assert environment["origin"] == "curadoria_humana"

    # Isolated + loaded => Stage 5 concluded, no blocking reason.
    assert body["completed"] is True
    assert body["reason"] is None

    # The per-stage state reflects the completion of Stage 5.
    stage5 = next(e for e in body["states"]["stages"] if e["stage"] == 5)
    assert stage5["state"] == "concluido"


# ---------------------------------------------------------------------------
# provision — not isolated => Stage 5 NOT concluded, container reported (Req. 9.7)
# ---------------------------------------------------------------------------


def test_provision_not_isolated_does_not_conclude_and_reports_container(
    session_factory: sessionmaker[Session],
) -> None:
    """A non-isolated target container => not concluded; container + reason reported.
    _Requirements: 9.6, 9.7, 9.9_"""
    # Use a distinct curated slug so persisted state is independent of the
    # happy-path test in this module-scoped DB.
    slug = "costaricto"
    client = _make_client(
        session_factory,
        inspector=_NotIsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The environment is still returned for display (Req. 9.8).
    environment = body["environment"]
    assert environment["isolation"]["approved"] is False
    non_isolated = [v for v in environment["isolation"]["verdicts"] if not v["isolated"]]
    assert non_isolated, "at least one container must be flagged not isolated"

    # Stage 5 does NOT conclude; the offending container(s)/reason are reported.
    assert body["completed"] is False
    assert body["reason"], "a non-isolation reason must be reported"
    offending = non_isolated[0]["container"]
    assert offending in body["reason"]

    # The per-stage state keeps Stage 5 not concluded (Stage 6 stays blocked).
    stage5 = next(e for e in body["states"]["stages"] if e["stage"] == 5)
    assert stage5["state"] != "concluido"


# ---------------------------------------------------------------------------
# provision — Abilities load failed => Stage 5 NOT concluded (Req. 9.5, 9.9)
# ---------------------------------------------------------------------------


def test_provision_abilities_not_loaded_does_not_conclude(
    session_factory: sessionmaker[Session],
) -> None:
    """Isolated but Abilities not loaded => not concluded with a reason (Req. 9.5, 9.9)."""
    # Distinct curated slug: this test never concludes Stage 5, so an
    # independent slug keeps the assertion free of cross-test persisted state.
    slug = "c0026"
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCalderaThatFails(),
    )
    resp = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["environment"]["loaded_abilities"] is False
    assert body["completed"] is False
    assert body["reason"] and "Abilities" in body["reason"]


# ---------------------------------------------------------------------------
# provision — unknown slug => 404 (error-mapping convention)
# ---------------------------------------------------------------------------


def test_provision_unknown_case_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug on the provision endpoint is a 404."""
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.post("/api/campaigns/does-not-exist/stage/5/provision")
    assert resp.status_code == 404, resp.text
