"""Integration tests for the Stage 6 auto-start REST surface (task 10.2).

These exercise the **redesign** endpoints added in task 10.1 against the FastAPI
app with a ``TestClient`` (ephemeral SQLite, mocked Caldera/runner via the
existing ``get_runner_factory`` dependency-override seam):

* ``POST /api/campaigns/{campaign}/stage/6/execute`` (auto-start) — delegates to
  :meth:`AutostartExecutionStageService.autostart` through the thin
  ``_AutostartRunnerAdapter`` over a real :class:`OperationRunner`.
* ``GET /api/campaigns/{campaign}/stage/6`` — the read-only snapshot.

The three behaviours under test (Req. 1.1, 1.4, 5.1, 5.2):

1. **Stage 5 concluded ⇒ one start.** With a persisted Stage-5
   :class:`StageRun` in ``COMPLETED`` state, ``POST .../stage/6/execute`` with
   ``confirmed=true`` triggers the runner **exactly once** (one Caldera
   ``POST /operations`` create call) and returns **200** with
   ``executed=true`` + the per-Ability snapshot.
2. **Stage 5 incomplete ⇒ blocked, runner NOT called.** Without a concluded
   Stage-5 row the auto-start is **blocked**: **200** with ``blocked=true`` +
   ``redirect_to_stage=5`` and **no** Caldera operation is ever created (the
   runner is never reached — Req. 1.4).
3. **Environment checks are NOT re-run in Stage 6, but the Stage-6 isolation
   gate IS still exercised.** The auto-start service only *reads* the Stage-5
   ``StageRun`` state — it never invokes any environment verifier — yet the
   runner's own isolation pre-flight still runs: a non-isolated environment with
   Stage 5 concluded yields **409** (Req. 5.1/5.2 + the inegotiable isolation
   gate).

Everything external is mocked (no real Caldera/Docker): the Caldera is a real
:class:`CalderaClient` over :class:`httpx.MockTransport`, and the isolation
verifier is a fake injected into the real :class:`OperationRunner` built by the
``get_runner_factory`` override — the same convention as
``tests/test_stage6_execution_endpoints.py`` and ``tests/test_execution_stage6.py``.

Uses the real curated ``shadowray`` slug (11 internal Abilities, **no** external
destinations), so the containment gate passes and the isolation gate can be
exercised in isolation.

_Requirements: 1.1, 1.4, 5.1, 5.2_
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

from app.api.emulation import get_caldera_client, get_runner_factory
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import StageRun
from app.models.enums import StageState
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
    IsolationResult,
)
from app.services.operation.runner import OperationRunner
from app.services.stage.stage_service import STAGE_ENVIRONMENT


_SLUG = "shadowray"  # real curated case (11 internal Abilities, no external)


# ---------------------------------------------------------------------------
# Mocked Caldera transport — healthy chain + a counter of create-operation calls
# ---------------------------------------------------------------------------


class _CountingHealthyCaldera:
    """A healthy Caldera :class:`httpx.MockTransport` handler + a start counter.

    The ``POST {OPERATIONS_PATH}`` call is the single point where the runner
    *creates* (starts) an Operation in Caldera. Counting it is a precise proxy
    for "the runner was triggered": it is reached only after the four pre-flight
    gates pass, so it is zero whenever the auto-start blocks or a gate refuses.
    The first ``GET`` poll returns a ``finished`` state so the collection loop
    terminates immediately (no sleep).
    """

    def __init__(self) -> None:
        self.create_operation_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == ABILITIES_PATH:
            return httpx.Response(
                200, json=json.loads(request.content.decode("utf-8"))
            )
        if method == "POST" and path == ADVERSARIES_PATH:
            return httpx.Response(
                200, json=json.loads(request.content.decode("utf-8"))
            )
        if method == "POST" and path == OPERATIONS_PATH:
            self.create_operation_calls += 1
            return httpx.Response(
                200, json={"id": "op-test", "name": "run", "state": "running"}
            )
        if method == "GET" and path == f"{OPERATIONS_PATH}/op-test":
            return httpx.Response(
                200, json={"id": "op-test", "state": "finished", "chain": []}
            )
        raise AssertionError(f"unexpected request: {method} {path}")


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """Fake isolation verifier: every requested container is isolated (passes)."""
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
            reasons=("conectado à bridge externa", "rota ao host"),
        )
        for name in (containers or ["nginx"])
    )
    return IsolationResult(passed=False, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Ephemeral SQLite + app fixtures (one DB per test — isolated persisted state)
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema, torn down per test."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_stage6_autostart_")
    db_path = Path(tmp_dir) / "stage6_autostart.db"
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


def _seed_stage5(session_factory: sessionmaker[Session], *, state: StageState) -> None:
    """Persist a Stage-5 (environment) ``StageRun`` in ``state`` for the case."""
    db = session_factory()
    try:
        db.add(StageRun(campaign_id=_SLUG, stage=STAGE_ENVIRONMENT, state=state))
        db.commit()
    finally:
        db.close()


def _make_client(
    session_factory: sessionmaker[Session],
    *,
    caldera_handler: _CountingHealthyCaldera,
    isolation_verifier=_isolated_verifier,
) -> TestClient:
    """Build a TestClient with DB + Caldera + runner isolation verifier mocked."""
    transport = httpx.MockTransport(caldera_handler)
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

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_caldera_client] = _get_caldera_override
    app.dependency_overrides[get_runner_factory] = _get_runner_factory_override
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Stage 5 concluded ⇒ auto-start triggers the runner exactly once (Req. 1.1)
# ---------------------------------------------------------------------------


def test_autostart_with_stage5_concluded_starts_once(
    session_factory: sessionmaker[Session],
) -> None:
    """Stage 5 concluded + isolated ⇒ one start (200, executed, snapshot).
    _Requirements: 1.1, 5.2_"""
    _seed_stage5(session_factory, state=StageState.COMPLETED)
    caldera = _CountingHealthyCaldera()
    client = _make_client(
        session_factory,
        caldera_handler=caldera,
        isolation_verifier=_isolated_verifier,
    )

    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execute", json={"confirmed": True}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _SLUG
    assert body["executed"] is True
    assert body["blocked"] is False
    assert body["reused"] is False
    # The runner was triggered exactly once (a single Caldera Operation created).
    assert caldera.create_operation_calls == 1
    # The 200 carries the per-Ability snapshot the frontend renders (Req. 1.1).
    assert body["output"] is not None
    assert "results" in body["output"] and "aggregate" in body["output"]
    assert body["operation_id"] is not None


# ---------------------------------------------------------------------------
# 2. Stage 5 incomplete ⇒ blocked / redirect to Stage 5, runner NOT called (1.4)
# ---------------------------------------------------------------------------


def test_autostart_with_stage5_incomplete_is_blocked_and_runs_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    """Stage 5 not concluded ⇒ blocked (200, redirect_to_stage=5), no run.
    _Requirements: 1.4_"""
    # Stage 5 present but still in progress (not concluded) — env not ready.
    _seed_stage5(session_factory, state=StageState.IN_PROGRESS)
    caldera = _CountingHealthyCaldera()
    client = _make_client(
        session_factory,
        caldera_handler=caldera,
        isolation_verifier=_isolated_verifier,
    )

    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execute", json={"confirmed": True}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blocked"] is True
    assert body["executed"] is False
    assert body["redirect_to_stage"] == STAGE_ENVIRONMENT  # 5
    assert body["output"] is None
    assert body["message"]
    # The runner was never reached: no Caldera Operation was created (Req. 1.4).
    assert caldera.create_operation_calls == 0


def test_autostart_with_no_stage5_row_is_blocked(
    session_factory: sessionmaker[Session],
) -> None:
    """A missing Stage-5 row is treated as not-ready ⇒ blocked, no run (Req. 1.4)."""
    caldera = _CountingHealthyCaldera()
    client = _make_client(
        session_factory,
        caldera_handler=caldera,
        isolation_verifier=_isolated_verifier,
    )

    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execute", json={"confirmed": True}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blocked"] is True
    assert body["executed"] is False
    assert body["redirect_to_stage"] == STAGE_ENVIRONMENT
    assert caldera.create_operation_calls == 0


# ---------------------------------------------------------------------------
# 3. Stage 6 does NOT re-run environment checks, but its isolation gate IS run
# ---------------------------------------------------------------------------


def test_autostart_isolation_gate_still_runs_in_stage6(
    session_factory: sessionmaker[Session],
) -> None:
    """Stage 5 concluded, yet the Stage-6 isolation gate still refuses a
    non-isolated environment (409) — the environment checks are not re-run (the
    service only reads the Stage-5 verdict), but the runner's own isolation
    pre-flight is exercised and nothing is executed.
    _Requirements: 5.1, 5.2_"""
    _seed_stage5(session_factory, state=StageState.COMPLETED)
    caldera = _CountingHealthyCaldera()
    client = _make_client(
        session_factory,
        caldera_handler=caldera,
        isolation_verifier=_not_isolated_verifier,
    )

    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execute", json={"confirmed": True}
    )

    # The inegotiable Stage-6 isolation gate refuses ⇒ 409, identifying the
    # offending container; nothing was executed.
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "isolamento_falhou"
    assert detail["containers"], "the offending container must be identified"
    # No Caldera Operation was created: the gate refused before any command.
    assert caldera.create_operation_calls == 0


def test_autostart_not_confirmed_runs_nothing_with_stage5_concluded(
    session_factory: sessionmaker[Session],
) -> None:
    """Stage 5 concluded but inline consent absent ⇒ nothing runs (Req. 1.4/1.6).

    Complements the isolation gate: with ``confirmed=false`` the runner's
    explicit-confirmation gate refuses before any Caldera command — a 200 with
    ``executed=false`` and no Operation created.
    """
    _seed_stage5(session_factory, state=StageState.COMPLETED)
    caldera = _CountingHealthyCaldera()
    client = _make_client(
        session_factory,
        caldera_handler=caldera,
        isolation_verifier=_isolated_verifier,
    )

    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/6/execute", json={"confirmed": False}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executed"] is False
    assert body["blocked"] is False
    assert caldera.create_operation_calls == 0
