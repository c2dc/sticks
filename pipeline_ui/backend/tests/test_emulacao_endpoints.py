"""Lightweight verification of the emulation preview/execute endpoints
(task 11.2) — SECURITY CORE (the HTTP surface of containment).

Full endpoint coverage is task 11.4; this suite is a focused smoke test that the
two endpoints wire the security core correctly and translate the runner's typed
outcomes into the HTTP contract of the design ("Endpoints REST" + emulation
sequence diagram):

* ``POST /api/casos/{caso}/emulacao/preview`` returns the concrete commands +
  target containers for a real curated case (``shadowray``) without executing
  (Req. 6.5).
* ``POST /api/casos/{caso}/emulacao``:
    - ``confirmado=false`` runs nothing (NOT_CONFIRMED — Req. 6.6/6.7),
    - an external destination -> 409 (Req. 6.2),
    - a non-isolated target container -> 409 (Req. 6.4),
    - a Caldera timeout -> 503 (Req. 4.6),
    - the happy path (internal-only + isolated + healthy Caldera) -> success
      with the aggregate.

Everything external is mocked (no real Caldera, no real Docker daemon):
- The database is an ephemeral temp-file SQLite created once for the module and
  wired into the app via the ``get_db`` dependency override.
- Caldera is a real :class:`CalderaClient` backed by :class:`httpx.MockTransport`
  (healthy, or a timeout-raising transport) injected via ``get_caldera_client``.
- Isolation is a fake verifier injected by overriding ``get_runner_factory`` so
  the runner is built with a controllable ``isolation_verifier``.
- Containment uses the real validator over abilities we control per test.

_Requisitos: 6.5, 6.6, 6.7, 6.2, 6.4, 4.6_
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

from app.api.emulacao import get_caldera_client, get_runner_factory
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.services.audit import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.containment.isolation import (
    ContainerIsolationVerdict,
    IsolationResult,
)
from app.services.operation.runner import OperationRunner


# ---------------------------------------------------------------------------
# Mocked Caldera transports (no real network).
# ---------------------------------------------------------------------------


def _healthy_router(request: httpx.Request) -> httpx.Response:
    """A MockTransport handler that serves a healthy Caldera chain."""
    method = request.method.upper()
    path = request.url.path
    if method == "GET" and path == HEALTH_PATH:
        return httpx.Response(200, json={"status": "ok"})
    if method == "POST" and path == ABILITIES_PATH:
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
                "chain": [
                    {
                        "id": "link-1",
                        "ability": {"ability_id": "ability-run"},
                        "command": "",
                        "status": 0,
                    }
                ],
            },
        )
    raise AssertionError(f"unexpected request: {method} {path}")


def _timeout_router(request: httpx.Request) -> httpx.Response:
    """A MockTransport handler that always times out (Caldera silent, Req. 4.6)."""
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
    """Fake isolation verifier: every requested container FAILS isolation.

    Reports each container attached to a bridge with external DNS, matching the
    exact leak the design guards against (Req. 6.4).
    """
    verdicts = tuple(
        ContainerIsolationVerdict(
            container=name,
            isolated=False,
            reasons=("conectado à local-network (bridge)", "dns externo 8.8.8.8"),
        )
        for name in containers
    )
    return IsolationResult(passed=False, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Ephemeral module-scoped SQLite + app fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_emulacao_")
    db_path = Path(tmp_dir) / "emulacao.db"
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
    transport: httpx.MockTransport,
    isolation_verifier,
) -> TestClient:
    """Build a TestClient with the DB, Caldera and isolation dependencies mocked."""
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
        # Depend on the (overridden) Caldera client and build a runner with the
        # injected isolation verifier, so no real Docker daemon is touched.
        client = CalderaClient(transport=transport)

        def _factory(session: Session) -> OperationRunner:
            return OperationRunner(
                session=session,
                caldera=client,
                audit_logger=AuditLogger(session),
                isolation_verifier=isolation_verifier,
            )

        return _factory

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_caldera_client] = _get_caldera_override
    app.dependency_overrides[get_runner_factory] = _get_runner_factory_override
    return TestClient(app)


# ---------------------------------------------------------------------------
# preview — real curated case (shadowray), no execution (Req. 6.5)
# ---------------------------------------------------------------------------


def test_preview_returns_commands_and_containers_for_shadowray(
    session_factory: sessionmaker[Session],
) -> None:
    """Preview lists concrete commands + target of each for a real case (Req. 6.5)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_isolated_verifier,
    )
    resp = client.post("/api/casos/shadowray/emulacao/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["caso_id"] == "shadowray"
    assert isinstance(body["abilities"], list) and body["abilities"]
    # Every command carries its concrete text and at least one target label.
    total_commands = 0
    for ability in body["abilities"]:
        for command in ability["comandos"]:
            total_commands += 1
            assert isinstance(command["comando"], str) and command["comando"].strip()
            assert command["alvos"], "each command must present a target"
    assert total_commands > 0


# ---------------------------------------------------------------------------
# emulation — outcome mapping (Req. 6.6/6.7, 6.2, 6.4, 4.6)
# ---------------------------------------------------------------------------


def test_emulacao_not_confirmed_runs_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    """confirmado=false => nothing started (Req. 6.6, 6.7)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_isolated_verifier,
    )
    resp = client.post("/api/casos/shadowray/emulacao", json={"confirmado": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iniciada"] is False
    assert body["resultado"] == "nao_confirmada"
    assert body["estado"] == "nao_iniciada"
    assert body["operacao_id"] is None


def test_emulacao_external_destination_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """A case with an external destination -> 409 identifying the violation (Req. 6.2).

    ``shadowray`` (like the curated cases) contains commands reaching external
    destinations (e.g. downloads), so its containment pre-flight refuses.
    """
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_isolated_verifier,
    )
    resp = client.post("/api/casos/shadowray/emulacao", json={"confirmado": True})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "contencao_recusada"
    assert detail["violacoes"], "the offending Ability/command must be identified"


def _internal_only_case(caso: str) -> object:
    """A curated-case stand-in with a single internal-only ability.

    Containment passes (destination ⊆ 172.21.0.0/24) so the isolation and
    availability gates can be exercised in isolation from the destination gate.
    """
    from app.services.case_service import (
        AbilityData,
        AdversaryData,
        CaseData,
        Executor,
    )

    return CaseData(
        id=caso,
        nome=caso,
        arquivo_ability="x",
        arquivo_adversary="x",
        arquivo_dag="x",
        abilities=[
            AbilityData(
                ability_id="a-internal",
                name="internal only",
                executors=[
                    Executor(
                        name="sh",
                        platform="linux",
                        command="curl http://172.21.0.20/x",
                    )
                ],
            )
        ],
        adversary=AdversaryData(id="adv-1", atomic_ordering=["a-internal"]),
    )


def _client_with_internal_case(
    session_factory: sessionmaker[Session],
    *,
    transport: httpx.MockTransport,
    isolation_verifier,
) -> tuple[TestClient, object]:
    """A TestClient whose case loader yields the internal-only case stand-in.

    Returns ``(client, restore)`` where ``restore`` is the original loader to put
    back after the request. Monkeypatching the handler's ``_load_case_or_error``
    isolates the isolation/availability gate from the real curated file contents.
    """
    client = _make_client(
        session_factory, transport=transport, isolation_verifier=isolation_verifier
    )
    from app.api import emulacao as emulacao_module

    original_loader = emulacao_module._load_case_or_error
    emulacao_module._load_case_or_error = (  # type: ignore[assignment]
        lambda caso, case_service: _internal_only_case(caso)
    )
    return client, original_loader


def _restore_loader(original_loader: object) -> None:
    from app.api import emulacao as emulacao_module

    emulacao_module._load_case_or_error = original_loader  # type: ignore[assignment]


def test_emulacao_non_isolated_container_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """A non-isolated target container -> 409 identifying the container (Req. 6.4).

    Uses an internal-only ability so the destination gate passes, then the fake
    isolation verifier fails, exercising the isolation gate specifically.
    """
    client, original = _client_with_internal_case(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_not_isolated_verifier,
    )
    try:
        resp = client.post(
            "/api/casos/shadowray/emulacao", json={"confirmado": True}
        )
    finally:
        _restore_loader(original)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "isolamento_falhou"
    assert detail["containers"], "the offending container must be identified"


def test_emulacao_caldera_timeout_returns_503(
    session_factory: sessionmaker[Session],
) -> None:
    """Caldera silent within 10s -> 503 (Req. 4.6).

    Uses an internal-only ability (containment passes) and isolated containers
    (isolation passes) so the availability gate is the one that fails, driven by
    a timeout-raising transport that simulates the silent Caldera.
    """
    client, original = _client_with_internal_case(
        session_factory,
        transport=httpx.MockTransport(_timeout_router),
        isolation_verifier=_isolated_verifier,
    )
    try:
        resp = client.post(
            "/api/casos/shadowray/emulacao", json={"confirmado": True}
        )
    finally:
        _restore_loader(original)

    assert resp.status_code == 503, resp.text
    assert "indisponível" in resp.json()["detail"].lower()


def test_emulacao_happy_path_success_with_aggregate(
    session_factory: sessionmaker[Session],
) -> None:
    """Internal-only + isolated + healthy Caldera -> success with aggregate."""
    client, original = _client_with_internal_case(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_isolated_verifier,
    )
    try:
        resp = client.post(
            "/api/casos/shadowray/emulacao", json={"confirmado": True}
        )
    finally:
        _restore_loader(original)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iniciada"] is True
    assert body["resultado"] == "concluida"
    assert body["estado"] == "finalizada"
    assert body["total_sucesso"] == 1
    assert body["total_falha"] == 0
    assert body["resultados"], "per-Ability results must be present"
