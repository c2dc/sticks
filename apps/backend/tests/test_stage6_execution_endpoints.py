"""Endpoint tests for the Estágio 6 (Execução + relatório) REST surface (task 8.5).

Cobre os três endpoints novos do esquema de seis Estágios adicionados em
``app/api/campaigns.py``:

* ``POST /api/campaigns/{campaign}/stage/6/execution/preview`` — a lista completa de
  commands concretos com o container-target de cada e a sinalização de destino
  external, **sem execute** (Req. 13.6, 13.7).
* ``POST /api/campaigns/{campaign}/stage/6/execution`` — confirmação + execução com os
  quatro gates do ``OperationRunner``, mapeando o result tipado para HTTP:
    - ``confirmed=false`` → **200** nada iniciou (``nao_confirmada``, Req. 13.2);
    - contêiner-target não isolated → **409** identificando o contêiner (Req. 14.2);
    - Caldera silencioso em 10s → **503** (Req. 10.1);
    - caminho feliz (interno + isolated + Caldera saudável) → **200** com execução
      completed, report and deprovisioning (Req. 10.7, 10.8, 12.1).
* ``GET /api/campaigns/{campaign}/report`` — o relatório consolidado + métricas de
  suficiência procedural derivadas dos results por Ability persistidos + o DAG
  curated (Req. 15.7).

Tudo external é *mockado* (sem Caldera nem Docker reais), reusando o mesmo molde de
``test_emulation_endpoints.py``:
- a Base_de_Dados é um SQLite temporário por módulo, ligado via ``get_db``;
- o Caldera é um :class:`CalderaClient` real sobre :class:`httpx.MockTransport`
  (saudável ou com timeout), injetado via ``get_caldera_client`` e no provisioner;
- o isolation é um verificador *fake* injetado no runner (via ``get_runner_factory``)
  e um inspector *fake* no provisioner (via ``get_provisioner``).

Usa o campaign curated real ``shadowray`` (11 Abilities, **sem** destino external), de
modo que o gate de contenção passe e os gates de isolation/disponibilidade possam
ser exercitados isoladamente.

_Requirements: 13.6, 13.7, 13.2, 14.2, 10.1, 10.7, 10.8, 12.1, 15.7_
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

from app.api.campaigns import get_provisioner
from app.api.emulation import get_caldera_client, get_runner_factory
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
from app.services.environment.compose_executor import CommandResult
from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.environment.health import ContainerStateInfo
from app.services.operation.runner import OperationRunner


_INTERNAL_NET = "caldera-internal-network"


class _IsolatedInspector:
    """Permissive read-only inspector: every container is on an internal net.

    Used only to make the provisioner's own ``inspect_isolation`` pass during
    ``provision`` (no Docker daemon). The *runner*'s isolation gate is driven
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
            reasons=("conectado à local-network (bridge)", "dns external 8.8.8.8"),
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

    A step ``subir_conteineres`` do pipeline da task 3.2 usa o ``ComposeExecutor``
    injetado; sem este *fake* a conclusão do Estágio 6 (que provisiona antes de
    execute) construiria o ``DockerComposeExecutor`` real e travaria o teste.
    """

    def up(self, compose_file: str, *, on_log: object = None) -> CommandResult:
        return CommandResult(code=0, lines=[])

    def down(self, compose_file: str, *, on_log: object = None) -> CommandResult:
        return CommandResult(code=0, lines=[])


class _HealthyStateInspector:
    """``ContainerStateInspector`` *fake* — todo contêiner-target running+healthy."""

    def get_container_state(self, container: str) -> ContainerStateInfo:
        return ContainerStateInfo(name=container, running=True, health="healthy")


# ---------------------------------------------------------------------------
# Ephemeral module-scoped SQLite + app fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_stage6_")
    db_path = Path(tmp_dir) / "stage6.db"
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

    def _get_provisioner_override() -> CurationEnvironmentProvisioner:
        # Fake inspector (never consulted — the fake verifier short-circuits
        # isolation) + fake Caldera loader + fakes de compose/state, so
        # provision() touches no subprocess nor daemon (pipeline da task 3.2).
        return CurationEnvironmentProvisioner(
            inspector=_IsolatedInspector(),
            caldera=_FakeCaldera(),
            compose_executor=_FakeComposeExecutor(),
            state_inspector=_HealthyStateInspector(),
        )

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_caldera_client] = _get_caldera_override
    app.dependency_overrides[get_runner_factory] = _get_runner_factory_override
    app.dependency_overrides[get_provisioner] = _get_provisioner_override
    return TestClient(app)


# ---------------------------------------------------------------------------
# preview — real curated case, no execution (Req. 13.6, 13.7)
# ---------------------------------------------------------------------------


def test_preview_returns_commands_and_containers(
    session_factory: sessionmaker[Session],
) -> None:
    """Preview lists concrete commands + target of each, no execution (Req. 13.6)."""
    client = _make_client(session_factory)
    resp = client.post(f"/api/campaigns/{_SLUG}/stage/6/execution/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["campaign_id"] == _SLUG
    # shadowray has no external destinations.
    assert body["has_external"] is False
    assert isinstance(body["abilities"], list) and body["abilities"]
    total_commands = 0
    for ability in body["abilities"]:
        for command in ability["commands"]:
            total_commands += 1
            assert isinstance(command["command"], str) and command["command"].strip()
            assert command["targets"], "each command must present a target"
    assert total_commands > 0


def test_preview_unknown_case_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug on the preview endpoint is a 404."""
    client = _make_client(session_factory)
    resp = client.post("/api/campaigns/does-not-exist/stage/6/execution/preview")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# execução — outcome mapping (Req. 13.2, 14.2, 10.1, 10.7/10.8/12.1)
# ---------------------------------------------------------------------------


def test_execution_not_confirmed_runs_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    """confirmed=false => nothing started; Stage 6 not concluded (Req. 13.2)."""
    client = _make_client(session_factory)
    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execution", json={"confirmed": False}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["execution"]["started"] is False
    assert body["execution"]["result"] == "nao_confirmada"
    assert body["execution"]["state"] == "nao_iniciada"
    assert body["completed"] is False
    # The environment is torn down even when nothing ran (Req. 10.8, 12.1).
    assert body["deprovisioning"]["deprovisioned"] is True


def test_execution_non_isolated_container_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """A non-isolated target container -> 409 identifying the container (Req. 14.2)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_not_isolated_verifier,
    )
    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execution", json={"confirmed": True}
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "isolamento_falhou"
    assert detail["containers"], "the offending container must be identified"


def test_execution_caldera_timeout_returns_503(
    session_factory: sessionmaker[Session],
) -> None:
    """Caldera silent within 10s -> 503; nothing started (Req. 10.1)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_timeout_router),
        isolation_verifier=_isolated_verifier,
    )
    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execution", json={"confirmed": True}
    )
    assert resp.status_code == 503, resp.text
    assert "indisponível" in resp.json()["detail"].lower()


def test_execution_happy_path_concludes_with_report_and_teardown(
    session_factory: sessionmaker[Session],
) -> None:
    """Internal + isolated + healthy Caldera -> 200, concluded, report + teardown.
    _Requirements: 10.7, 10.8, 12.1_"""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_isolated_verifier,
    )
    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execution", json={"confirmed": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _SLUG
    assert body["execution"]["started"] is True
    assert body["execution"]["result"] == "concluida"
    # A report was consolidated (Req. 10.7) and the environment deprovisioned.
    assert body["report"]["campaign_id"] == _SLUG
    assert "metrics" in body["report"]
    assert body["deprovisioning"]["deprovisioned"] is True
    assert body["report"]["environment_deprovisioned"] is True
    # Execution concluded, env torn down, report persisted -> Stage 6 concluded.
    assert body["completed"] is True


# ---------------------------------------------------------------------------
# relatório — métricas derivadas dos results persistidos + DAG (Req. 15.7)
# ---------------------------------------------------------------------------


def test_report_shape_for_case_without_execution(
    session_factory: sessionmaker[Session],
) -> None:
    """A case with no execution yet returns a reportable, zeroed report (Req. 15.7)."""
    client = _make_client(session_factory)
    # Use a different real curated slug that this module never executes, so the
    # persisted results are empty regardless of other tests in the module.
    resp = client.get("/api/campaigns/c0010/report")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == "c0010"
    metrics = body["metrics"]
    # The DAG defines the technique universe (denominator); none succeeded.
    assert metrics["total_dag_techniques"] >= 0
    assert metrics["successful_techniques"] == 0
    assert metrics["success_rate"] == 0.0
    assert metrics["success_verdict"] is False
    assert body["environment_deprovisioned"] is False


def test_report_unknown_case_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug on the report endpoint is a 404."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/does-not-exist/report")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# relatório — tempo total real derivado dos instantes + desprovisionamento
# estruturado recuperado da linha ``execution_reports`` persistida (task 2.5).
# _Requirements: 4.1, 4.3, 7.2, 7.3_
# ---------------------------------------------------------------------------

import datetime as _dt

from app.models.domain import (
    AbilityResult as _AbilityResultRow,
    ExecutionReport as _ExecutionReportRow,
    Operation as _OperationRow,
)
from app.models.enums import AbilityResultStatus, OperationState


def _seed_finished_operation(
    session_factory: sessionmaker[Session],
    *,
    campaign_id: str,
    started_at: _dt.datetime | None,
    finished_at: _dt.datetime | None,
) -> int:
    """Persist a FINISHED Operation (with one Ability result) for ``campaign_id``.

    Writes the Operation lifecycle instants (``started_at``/``finished_at``) that
    ``get_report`` reads through :class:`OperationResultView` to derive the real
    ``total_time_s`` (``finished_at - started_at`` in seconds). Returns the
    persisted ``operation_id`` so a teardown row can link to it.
    """
    db = session_factory()
    try:
        op = _OperationRow(
            campaign_id=campaign_id,
            state=OperationState.FINISHED,
            total_success=1,
            total_failure=0,
            started_at=started_at,
            finished_at=finished_at,
        )
        db.add(op)
        db.flush()
        db.add(
            _AbilityResultRow(
                operation_id=op.id,
                ability_id="ability-ok",
                status=AbilityResultStatus.SUCCESS,
                command_output="ok",
            )
        )
        db.commit()
        return op.id
    finally:
        db.close()


def test_report_total_time_s_derived_from_operation_instants(
    session_factory: sessionmaker[Session],
) -> None:
    """``total_time_s`` is ``finished_at - started_at`` in seconds (Req. 4.1, 4.3)."""
    slug = "c0026"
    started = _dt.datetime(2024, 5, 6, 7, 0, 0)
    finished = started + _dt.timedelta(seconds=42)
    _seed_finished_operation(
        session_factory, campaign_id=slug, started_at=started, finished_at=finished
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{slug}/report")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == slug
    # The known duration surfaces verbatim as the report's total time.
    assert body["metrics"]["total_time_s"] == 42.0


def test_report_total_time_s_is_null_when_a_timestamp_is_missing(
    session_factory: sessionmaker[Session],
) -> None:
    """A missing ``finished_at`` yields ``total_time_s = null`` (Req. 4.3)."""
    slug = "costaricto"
    started = _dt.datetime(2024, 5, 6, 7, 0, 0)
    _seed_finished_operation(
        session_factory, campaign_id=slug, started_at=started, finished_at=None
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{slug}/report")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == slug
    # Absent instant -> the derived total time is not measurable -> null.
    assert body["metrics"]["total_time_s"] is None


def test_report_deprovisioning_surfaces_failed_teardown_remaining_resources(
    session_factory: sessionmaker[Session],
) -> None:
    """A failed-teardown persisted row surfaces the structured ``deprovisioning``.

    The ``execution_reports`` row recorded ``deprovisioned=False`` plus the
    resources that stayed provisioned; ``get_report`` recovers them into the
    report's structured ``deprovisioning`` field (Req. 7.2, 7.3).
    """
    slug = "outer_space"
    started = _dt.datetime(2024, 5, 6, 7, 0, 0)
    finished = started + _dt.timedelta(seconds=10)
    op_id = _seed_finished_operation(
        session_factory, campaign_id=slug, started_at=started, finished_at=finished
    )

    remaining = ["nginx-target", "caldera-internal-network"]
    db = session_factory()
    try:
        db.add(
            _ExecutionReportRow(
                campaign_id=slug,
                operation_id=op_id,
                metrics={},
                success_verdict=False,
                environment_deprovisioned=False,
                deprovisioned=False,
                remaining_resources=remaining,
                generated_at=_dt.datetime(2024, 5, 6, 7, 0, 20),
            )
        )
        db.commit()
    finally:
        db.close()

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{slug}/report")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == slug
    deprovisioning = body["deprovisioning"]
    assert deprovisioning is not None
    assert deprovisioning["deprovisioned"] is False
    assert deprovisioning["remaining_resources"] == remaining
