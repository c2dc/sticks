"""Integration tests of the REST surface with mocked services (task 11.4).

This suite complements the focused ``test_emulacao_endpoints.py`` (task 11.2) by
covering the *broader* REST surface end-to-end at the HTTP layer, plus the full
``preview -> emulacao`` sequence, all with the external world mocked (no real
Caldera, no real Docker daemon):

* the casos / estágios surface — ``GET /api/casos`` (the 8 curated cases +
  aggregate progress), ``GET /api/casos/{caso}``, the three
  ``GET /api/casos/{caso}/estagio/{1,2,3}``, ``POST .../estagio/1/executar`` and
  ``GET /api/casos/{caso}/operacao``;
* the session / preferences / audit surface — ``GET /api/estado-sessao``,
  ``PUT /api/preferencias`` and ``GET /api/auditoria/{operacao}``;
* the emulation flow end-to-end — ``POST .../emulacao/preview`` followed by
  ``POST .../emulacao``, exercising the four gates as HTTP outcomes:
    - 409 on a containment violation (external destination, Req. 6.2),
    - 409 on an isolation failure (non-isolated container, Req. 6.4),
    - 503 on Caldera unavailable/timeout (Req. 4.6),
    - ``confirmado=false`` -> nothing started (Req. 6.6),
    - the happy path (internal-only + isolated + healthy Caldera).

Isolation from ``test_emulacao_endpoints.py``
--------------------------------------------
The emulation-outcome cases here are driven through the full ``preview ->
emulacao`` *sequence* (a preview call always precedes the execute call, matching
the design's sequence diagram and the confirmation-modal flow), rather than the
single execute calls the sibling suite uses. The REST-surface tests (casos /
stages / estado-sessão / preferências / auditoria) are entirely new coverage
not present in the sibling suite.

Everything external is mocked:
- The database is an ephemeral temp-file SQLite created per test module and
  wired into the app via the ``get_db`` dependency override.
- Caldera is a real :class:`CalderaClient` backed by :class:`httpx.MockTransport`
  (healthy or timeout-raising) injected via ``get_caldera_client``.
- Isolation is a fake verifier injected by overriding ``get_runner_factory``.
- Containment uses the real validator over the (real or stubbed) case abilities.

_Requisitos: 6.2, 6.4, 4.6, 6.6_
"""

from __future__ import annotations

import datetime as dt
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
from app.models.domain import (
    AuditLogEntry,
    Operation,
)
from app.models.enums import (
    OperationState,
    StageState,
)
from app.services.audit import AuditLogger
from app.services.caldera import (
    ADVERSARIES_PATH,
    ABILITIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.case_service import CURATED_CASES
from app.services.containment.isolation import (
    ContainerIsolationVerdict,
    IsolationResult,
)
from app.services.operation.runner import OperationRunner


# ---------------------------------------------------------------------------
# Mocked Caldera transports (no real network) — same contract as the sibling
# suite's healthy chain, kept local so this file is self-contained.
# ---------------------------------------------------------------------------


def _healthy_router(request: httpx.Request) -> httpx.Response:
    """A MockTransport handler that serves a healthy Caldera chain."""
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
            200, json={"id": "op-api", "name": "run", "state": "running"}
        )
    if method == "GET" and path == f"{OPERATIONS_PATH}/op-api":
        return httpx.Response(
            200,
            json={
                "id": "op-api",
                "state": "finished",
                "chain": [
                    {
                        "id": "link-1",
                        "ability": {"ability_id": "a-internal"},
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
    """Fake isolation verifier: every requested container FAILS isolation (Req. 6.4)."""
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
    """A fresh temp-file SQLite engine + schema for the module.

    Kept module-scoped so the whole REST surface exercises the same ephemeral
    DB; the audit-trail test seeds its own rows via the same factory. The temp
    file is removed on teardown (Windows-friendly cleanup).
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_api_endpoints_")
    db_path = Path(tmp_dir) / "api_endpoints.db"
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
    """Build a TestClient with the DB, Caldera and isolation dependencies mocked.

    ``transport`` defaults to a healthy Caldera; ``isolation_verifier`` defaults
    to the all-isolated verifier. The pure REST-surface tests do not touch
    Caldera/isolation, but wiring the overrides unconditionally keeps every
    client fully mocked (no real Caldera/Docker is ever contacted).
    """
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

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_caldera_client] = _get_caldera_override
    app.dependency_overrides[get_runner_factory] = _get_runner_factory_override
    return TestClient(app)


# A curated slug that really exists on disk and is used across the surface tests.
_REAL_SLUG = "shadowray"


# ---------------------------------------------------------------------------
# Internal-only case stand-in (containment passes) — shared with the sibling
# suite's approach so the isolation/availability gates can be reached without an
# external-destination refusal. Kept local for self-containment.
# ---------------------------------------------------------------------------


def _internal_only_case(caso: str) -> object:
    """A curated-case stand-in with a single internal-only ability (⊆ 172.21/24)."""
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


def _external_destination_case(caso: str) -> object:
    """A deterministic stand-in containing one destination outside the lab."""
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
                ability_id="a-external",
                name="external destination",
                executors=[
                    Executor(
                        name="sh",
                        platform="linux",
                        command="curl https://example.com/payload",
                    )
                ],
            )
        ],
        adversary=AdversaryData(id="adv-1", atomic_ordering=["a-external"]),
    )

class _internal_case_loader:
    """Context manager swapping the emulacao handler's case loader for a stub.

    Monkeypatches ``app.api.emulacao._load_case_or_error`` to yield the
    internal-only stand-in so the destination gate passes and the isolation /
    availability gates can be exercised in isolation. Restores the original
    loader on exit even if the request raised.
    """

    def __enter__(self):
        from app.api import emulacao as emulacao_module

        self._module = emulacao_module
        self._original = emulacao_module._load_case_or_error
        emulacao_module._load_case_or_error = (  # type: ignore[assignment]
            lambda caso, case_service: _internal_only_case(caso)
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self._module._load_case_or_error = self._original  # type: ignore[assignment]


class _external_case_loader(_internal_case_loader):
    """Swap the emulation loader for the deterministic external case."""

    def __enter__(self):
        from app.api import emulacao as emulacao_module

        self._module = emulacao_module
        self._original = emulacao_module._load_case_or_error
        emulacao_module._load_case_or_error = (  # type: ignore[assignment]
            lambda caso, case_service: _external_destination_case(caso)
        )
        return self


# ===========================================================================
# GET /api/casos — the 8 curated cases + aggregate progress
# ===========================================================================


def test_listar_casos_lists_eight_cases_and_aggregate_progress(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/casos`` lists all 8 curated cases with per-stage state and
    the aggregate progress over 8 (Req. 5.1, 5.5)."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Exactly the 8 curated cases, by their registered slugs.
    slugs = {c["id"] for c in body["casos"]}
    assert slugs == set(CURATED_CASES), slugs
    assert len(body["casos"]) == 8

    # Each successfully-loaded case carries its per-stage states (3 stages) and
    # no load error.
    for caso in body["casos"]:
        assert caso["erro"] is None, caso
        assert caso["estados"] is not None
        assert len(caso["estados"]["estagios"]) == 3

    # Aggregate progress spans the 8-case universe; nothing completed yet.
    progresso = body["progresso"]
    assert progresso["total_casos"] == 8
    assert progresso["casos_concluidos"] == 0
    assert progresso["percentual"] == 0


# ===========================================================================
# GET /api/casos/{caso} — case detail
# ===========================================================================


def test_detalhar_caso_returns_abilities_adversary_and_origin(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/casos/{caso}`` returns the curated Abilities, Adversary,
    ordering and the explicit translation source (Req. 3.2, 3.3, 3.5, 11.1)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["id"] == _REAL_SLUG
    assert body["origem_traducao"] == "curadoria_humana"
    assert isinstance(body["abilities"], list) and body["abilities"]
    # Every ability carries its stable id.
    for ability in body["abilities"]:
        assert ability["ability_id"]
    # The ordering mirrors the adversary's atomic_ordering when present.
    if body["adversary"] is not None:
        assert body["atomic_ordering"] == body["adversary"]["atomic_ordering"]


def test_detalhar_caso_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown case slug is a 404 (not one of the 8 curated cases)."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos/nao_existe")
    assert resp.status_code == 404, resp.text


# ===========================================================================
# GET /api/casos/{caso}/estagio/{1,2,3}
# ===========================================================================


def test_get_estagio1_returns_input_output(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../estagio/1`` returns the Stage-1 Entrada/Saída for the case."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/estagio/1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    assert body["entrada"]["caso_id"] == _REAL_SLUG
    # estado is one of the StageState values.
    assert body["estado"] in {s.value for s in StageState}


def test_get_estagio2_returns_curated_translation(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../estagio/2`` returns the abstract-techniques Entrada and the
    curated-abilities Saída (Req. 3.1)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/estagio/2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    assert "entrada" in body


def test_get_estagio3_returns_input_and_accumulated_output(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../estagio/3`` returns the Adversary+Abilities Entrada and the
    accumulated Saída (empty before any emulation) (Req. 4.1)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/estagio/3")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entrada"]["caso_id"] == _REAL_SLUG
    assert isinstance(body["saida"], list)


def test_get_estagio_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug is a 404 on the stage endpoints too."""
    client = _make_client(session_factory)
    for estagio in (1, 2, 3):
        resp = client.get(f"/api/casos/nao_existe/estagio/{estagio}")
        assert resp.status_code == 404, resp.text


# ===========================================================================
# POST /api/casos/{caso}/estagio/1/executar
# ===========================================================================


def test_post_estagio1_executar_runs_structural_modeling(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../estagio/1/executar`` triggers structural modeling and reports
    a typed Stage-1 result (Req. 2.2). For a real curated case it completes and
    persists a StageRun row."""
    client = _make_client(session_factory)
    resp = client.post(f"/api/casos/{_REAL_SLUG}/estagio/1/executar")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    # A real curated case yields a completed (concluido) stage, not an error.
    assert body["estado"] == StageState.COMPLETED.value
    assert body["saida"] is not None
    assert body["stage_run_id"] is not None

    # The persisted StageRun is now visible in GET /api/casos (its estado is
    # concluido for stage 1), confirming the write reached the shared DB.
    listing = client.get("/api/casos").json()
    caso = next(c for c in listing["casos"] if c["id"] == _REAL_SLUG)
    estagio1 = caso["estados"]["estagios"][0]
    assert estagio1["estagio"] == 1
    assert estagio1["estado"] == StageState.COMPLETED.value


# ===========================================================================
# GET /api/casos/{caso}/operacao
# ===========================================================================


def test_get_operacao_empty_before_any_emulation(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../operacao`` returns an empty operations list before any run."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos/costaricto/operacao")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == "costaricto"
    assert body["operacoes"] == []


def test_get_operacao_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug is a 404 on the operation endpoint."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos/nao_existe/operacao")
    assert resp.status_code == 404, resp.text


# ===========================================================================
# GET /api/estado-sessao
# ===========================================================================


def test_get_estado_sessao_default_when_none_persisted(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/estado-sessao`` returns the default all-not-started state when
    no session state is persisted (Req. 9.5)."""
    client = _make_client(session_factory)
    resp = client.get("/api/estado-sessao")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exists"] is False
    assert body["restore_failed"] is False
    assert body["caso_atual"] is None
    assert isinstance(body["estagios_concluidos_por_caso"], dict)


# ===========================================================================
# PUT /api/preferencias
# ===========================================================================


def test_put_preferencias_persists_theme_and_language(
    session_factory: sessionmaker[Session],
) -> None:
    """``PUT /api/preferencias`` persists the theme + language and echoes the
    stored values back (Req. 7.4, 8.4)."""
    client = _make_client(session_factory)
    resp = client.put(
        "/api/preferencias", json={"tema": "escuro", "idioma": "en"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tema": "escuro", "idioma": "en"}

    # A partial update keeps the omitted field at its previously persisted value.
    resp2 = client.put("/api/preferencias", json={"tema": "claro"})
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["tema"] == "claro"
    assert body2["idioma"] == "en"


def test_put_preferencias_rejects_invalid_theme(
    session_factory: sessionmaker[Session],
) -> None:
    """An out-of-enum theme value is a 422 (schema validation)."""
    client = _make_client(session_factory)
    resp = client.put("/api/preferencias", json={"tema": "neon"})
    assert resp.status_code == 422, resp.text


# ===========================================================================
# GET /api/auditoria/{operacao}
# ===========================================================================


def test_get_auditoria_returns_persisted_trail_oldest_first(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/auditoria/{operacao}`` returns the persisted audit trail of an
    Operation — one row per command, oldest first (Req. 6.8)."""
    # Seed an Operation with two audit rows directly via the shared factory.
    db = session_factory()
    try:
        op = Operation(
            caso_id="c0010",
            estado=OperationState.FINISHED,
            total_sucesso=1,
            total_falha=1,
        )
        db.add(op)
        db.flush()
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        db.add_all(
            [
                AuditLogEntry(
                    operacao_id=op.id,
                    ability_id="a-1",
                    comando="curl http://172.21.0.20/a",
                    container_destino="target-a",
                    resultado="sucesso",
                    registrado_em=now,
                ),
                AuditLogEntry(
                    operacao_id=op.id,
                    ability_id="a-2",
                    comando="curl http://172.21.0.21/b",
                    container_destino="target-b",
                    resultado="falha",
                    registrado_em=now + dt.timedelta(seconds=1),
                ),
            ]
        )
        db.commit()
        op_id = op.id
    finally:
        db.close()

    client = _make_client(session_factory)
    resp = client.get(f"/api/auditoria/{op_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operacao_id"] == op_id
    assert len(body["registros"]) == 2
    # Oldest first: ids strictly ascending, one row per executed command.
    ids = [r["id"] for r in body["registros"]]
    assert ids == sorted(ids)
    assert body["registros"][0]["comando"] == "curl http://172.21.0.20/a"
    assert body["registros"][0]["container_destino"] == "target-a"


def test_get_auditoria_unknown_operation_returns_empty_trail(
    session_factory: sessionmaker[Session],
) -> None:
    """An operation with no audit rows returns an empty (but 200) trail."""
    client = _make_client(session_factory)
    resp = client.get("/api/auditoria/999999")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operacao_id"] == 999999
    assert body["registros"] == []


# ===========================================================================
# Emulation flow end-to-end: POST .../emulacao/preview -> POST .../emulacao
# ===========================================================================


def test_preview_then_external_destination_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulacao: an external destination is flagged and refused."""
    client = _make_client(session_factory)

    with _external_case_loader():
        preview = client.post(f"/api/casos/{_REAL_SLUG}/emulacao/preview")
        assert preview.status_code == 200, preview.text
        assert preview.json()["tem_externo"] is True

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/emulacao", json={"confirmado": True}
        )
    assert run.status_code == 409, run.text
    detail = run.json()["detail"]
    assert detail["motivo"] == "contencao_recusada"
    assert detail["violacoes"], "the offending Ability/command must be identified"


def test_preview_then_non_isolated_container_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulacao (internal-only case): containment passes but a
    non-isolated container yields 409 identifying the container (Req. 6.4)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_not_isolated_verifier,
    )
    with _internal_case_loader():
        preview = client.post(f"/api/casos/{_REAL_SLUG}/emulacao/preview")
        assert preview.status_code == 200, preview.text
        # Internal-only: nothing external in the preview.
        assert preview.json()["tem_externo"] is False

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/emulacao", json={"confirmado": True}
        )
    assert run.status_code == 409, run.text
    detail = run.json()["detail"]
    assert detail["motivo"] == "isolamento_falhou"
    assert detail["containers"], "the offending container must be identified"


def test_preview_then_caldera_timeout_returns_503(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulacao (internal-only + isolated): a silent Caldera within
    10s yields 503; nothing started (Req. 4.6)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_timeout_router),
        isolation_verifier=_isolated_verifier,
    )
    with _internal_case_loader():
        preview = client.post(f"/api/casos/{_REAL_SLUG}/emulacao/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/emulacao", json={"confirmado": True}
        )
    assert run.status_code == 503, run.text
    assert "indisponível" in run.json()["detail"].lower()


def test_preview_then_not_confirmed_runs_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulacao with confirmado=false starts nothing (Req. 6.6).

    The preview is shown (the modal step) and then, without explicit
    confirmation, the execute call runs nothing: no operation id, state stays
    not-started, and no audit rows are written for it.
    """
    client = _make_client(session_factory)
    with _internal_case_loader():
        preview = client.post(f"/api/casos/{_REAL_SLUG}/emulacao/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/emulacao", json={"confirmado": False}
        )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["iniciada"] is False
    assert body["resultado"] == "nao_confirmada"
    assert body["estado"] == "nao_iniciada"
    assert body["operacao_id"] is None

    # Nothing ran => no Operation was created for this case (and therefore no
    # ability results / audit rows tied to it). Scoped to the case so the
    # module-shared DB (which other tests seed) does not create false failures.
    db = session_factory()
    try:
        assert (
            db.query(Operation).filter(Operation.caso_id == _REAL_SLUG).count() == 0
        )
    finally:
        db.close()


def test_preview_then_confirmed_happy_path_succeeds_with_aggregate(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulacao (internal-only + isolated + healthy Caldera) with
    confirmado=true succeeds, returning the aggregate and per-Ability results."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_isolated_verifier,
    )
    with _internal_case_loader():
        preview = client.post(f"/api/casos/{_REAL_SLUG}/emulacao/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/emulacao", json={"confirmado": True}
        )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["iniciada"] is True
    assert body["resultado"] == "concluida"
    assert body["estado"] == "finalizada"
    assert body["total_sucesso"] == 1
    assert body["total_falha"] == 0
    assert body["resultados"], "per-Ability results must be present"
