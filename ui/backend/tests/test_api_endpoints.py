"""Integration tests of the REST surface with mocked services (task 11.4).

This suite complements the focused ``test_emulacao_endpoints.py`` (task 11.2) by
covering the *broader* REST surface end-to-end at the HTTP layer, plus the full
``preview -> emulacao`` sequence, all with the external world mocked (no real
Caldera, no real Docker daemon):

* the casos / estágios surface — ``GET /api/casos`` (the 8 curated cases +
  aggregate progress), ``GET /api/casos/{caso}``, the stage reads
  ``GET /api/casos/{caso}/estagio/{1,3,5}`` (estágio 3 é a Geração do DAG —
  DagView/Mermaid), ``POST .../estagio/1/executar``, the DAG endpoints
  (``GET .../dags`` and ``POST .../estagio/3/escolher-dag`` with its 200/409/404
  outcomes), the sequential-block ``POST .../estagio/3/concluir`` and
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
    StageRun,
)
from app.models.enums import (
    OperationState,
    StageState,
    TranslationSource,
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
                        command="curl http://192.168.20.30/x",
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
    """``GET /api/casos`` lists all 8 curated cases with per-stage state (6
    stages each) and the aggregate progress over 8 (Req. 5.1, 5.5)."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Exactly the 8 curated cases, by their registered slugs.
    slugs = {c["id"] for c in body["casos"]}
    assert slugs == set(CURATED_CASES), slugs
    assert len(body["casos"]) == 8

    # Each successfully-loaded case carries its per-stage states (6 stages, the
    # renumbered 1..6 scheme) and no load error.
    for caso in body["casos"]:
        assert caso["erro"] is None, caso
        assert caso["estados"] is not None
        assert len(caso["estados"]["estagios"]) == 6
        # The six stages are numbered 1..6 in order.
        assert [e["estagio"] for e in caso["estados"]["estagios"]] == [
            1,
            2,
            3,
            4,
            5,
            6,
        ]

    # Aggregate progress spans the 8-case universe. No single case has all six
    # stages concluido, so nothing is counted as fully replicated.
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
# GET /api/casos/{caso}/estagio/{1,2,3,4,5} — renumbered 1..5 scheme
#
# No esquema renumerado (spec pipeline-cinco-estagios): Estágio 1 = Modelagem
# Estrutural, Estágio 2 = Geração do DAG (coberto na seção de DAGs abaixo),
# Estágio 3 = Operacionalização, Estágio 4 = Provisionamento do Ambiente
# (topologia + isolamento), Estágio 5 = Execução da Campanha.
# ===========================================================================


def test_get_estagio1_returns_bundle_dashboard(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET .../estagio/1`` returns the Stage-1 (Seleção da base STIX) bundle
    dashboard + conclusion verdict (seis Estágios, Req. 5.7, 5.8, 5.10).

    No esquema de SEIS Estágios o Estágio 1 é a Seleção da base STIX. The handler
    delegates to ``Estagio1Service``; we pin its ``StixService`` to a temp data
    dir and seed the curated bundle so the stage concludes with no network
    egress (opening the screen without an explicit version defaults to the
    curated version)."""
    import app.services.stix.estagio1_service as estagio1_module
    from app.services.stix.stix_service import VERSAO_CURADA, StixService

    base = StixService(data_dir=tmp_path).base_path(VERSAO_CURADA)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        json.dumps({"type": "bundle", "objects": []}), encoding="utf-8"
    )
    monkeypatch.setattr(
        estagio1_module,
        "StixService",
        lambda *a, **k: StixService(data_dir=tmp_path),
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/estagio/1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versao"] == VERSAO_CURADA
    assert body["concluido"] is True
    assert body["bundle_em_disco"] is True
    assert body["dashboard"] is not None


def test_get_estagio3_returns_dag_view(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../estagio/3`` returns the DAG view (Geração do DAG — Req. 7.2, 7.3).

    No esquema de seis Estágios, a Geração do DAG é o Estágio 3, servido como
    :class:`DagView` (grafo + Mermaid). Para um Caso curado válido o DAG é
    ``curado``, ``pode_avancar`` e traz nós e o diagrama Mermaid."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/estagio/3")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    assert body["curado"] is True
    assert body["pode_avancar"] is True
    assert len(body["nos"]) > 0
    assert body["mermaid"]


def test_get_estagio4_returns_abilities_and_parametros(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../estagio/4`` returns the Operacionalização view — Abilities +
    ParametrosDeAmbiente (seis Estágios, Req. 8.4, 8.7).

    No esquema de SEIS Estágios, o Estágio 4 é a Operacionalização: a Saída são
    **dois artefatos tipados separados** — as Abilities ordenadas pelo DAG e os
    ParametrosDeAmbiente derivados dos nós curados. Para um Caso curado válido,
    ambos são produzidos (``ambos_produzidos``), a plataforma é ``conteiner`` e a
    origem é ``curadoria_humana``. É read-only: abrir a tela não conclui o
    Estágio."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/estagio/4")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    # 1º artefato: Abilities ordenadas (Stage2Output) com ao menos uma Ability.
    assert body["abilities"] is not None
    assert body["abilities"]["has_abilities"] is True
    assert len(body["abilities"]["abilities"]) > 0
    assert len(body["abilities"]["ordering"]) > 0
    # 2º artefato: ParametrosDeAmbiente (plataforma conteiner, origem curada).
    assert body["parametros"]["caso_id"] == _REAL_SLUG
    assert body["parametros"]["plataforma"] == "conteiner"
    assert body["parametros"]["origem"] == TranslationSource.HUMAN_CURATION.value
    # Ambos produzidos → critério de conclusão satisfeito (Req. 8.8).
    assert body["ambos_produzidos"] is True
    assert body["origem"] == TranslationSource.HUMAN_CURATION.value
    assert "estagios" in body["estados"]


def test_get_estagio6_returns_input_and_accumulated_output(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../estagio/6`` returns the Adversary+Abilities Entrada and the
    accumulated Saída (Execução, medição e relatório — Req. 10.x).

    No esquema de SEIS Estágios, a Execução é o Estágio 6 (antigo Estágio 5 do
    esquema de cinco). The accumulated Saída is a list (empty before any
    emulation runs)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/estagio/6")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entrada"]["caso_id"] == _REAL_SLUG
    assert isinstance(body["saida"], list)


def test_get_estagio_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug is a 404 on every stage endpoint (1..6)."""
    client = _make_client(session_factory)
    for estagio in (1, 2, 3, 4, 5, 6):
        resp = client.get(f"/api/casos/nao_existe/estagio/{estagio}")
        assert resp.status_code == 404, resp.text


# ===========================================================================
# POST /api/casos/{caso}/estagio/1/executar — structural modeling (Estágio 1)
# ===========================================================================


def test_post_estagio1_executar_runs_structural_modeling(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../estagio/1/executar`` triggers structural modeling and reports
    a typed Stage result (Req. 1.3, 1.7). For a real curated case it completes
    and persists a StageRun row for Estágio 1.

    No esquema de cinco Estágios a Modelagem Estrutural é o Estágio 1, então a
    execução vive em ``.../estagio/1/executar``."""
    client = _make_client(session_factory)
    resp = client.post(f"/api/casos/{_REAL_SLUG}/estagio/1/executar")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    # A real curated case yields a completed (concluido) stage, not an error:
    # the structural modeling ran and persisted its typed result.
    assert body["estado"] == StageState.COMPLETED.value
    assert body["saida"] is not None
    assert body["stage_run_id"] is not None

    # The modeling write reached the shared DB: a concluido StageRun is now
    # visible in GET /api/casos for this case (the per-stage state reflects the
    # persisted modeling run).
    listing = client.get("/api/casos").json()
    caso = next(c for c in listing["casos"] if c["id"] == _REAL_SLUG)
    assert any(
        e["estado"] == StageState.COMPLETED.value
        for e in caso["estados"]["estagios"]
    ), caso["estados"]["estagios"]


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
                    comando="curl http://192.168.20.30/a",
                    container_destino="target-a",
                    resultado="sucesso",
                    registrado_em=now,
                ),
                AuditLogEntry(
                    operacao_id=op.id,
                    ability_id="a-2",
                    comando="curl http://192.168.20.31/b",
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
    assert body["registros"][0]["comando"] == "curl http://192.168.20.30/a"
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
        preview = client.post(f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao/preview")
        assert preview.status_code == 200, preview.text
        assert preview.json()["tem_externo"] is True

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao", json={"confirmado": True}
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
        preview = client.post(f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao/preview")
        assert preview.status_code == 200, preview.text
        # Internal-only: nothing external in the preview.
        assert preview.json()["tem_externo"] is False

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao", json={"confirmado": True}
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
        preview = client.post(f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao", json={"confirmado": True}
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
        preview = client.post(f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao", json={"confirmado": False}
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
        preview = client.post(f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/casos/{_REAL_SLUG}/estagio/5/emulacao", json={"confirmado": True}
        )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["iniciada"] is True
    assert body["resultado"] == "concluida"
    assert body["estado"] == "finalizada"
    assert body["total_sucesso"] == 1
    assert body["total_falha"] == 0
    assert body["resultados"], "per-Ability results must be present"


# ===========================================================================
# Estágio 3 — Geração do DAG: GET /api/casos/{caso}/dags and
# POST /api/casos/{caso}/estagio/3/escolher-dag (Req. 4.8, 7.2, 7.3)
#
# No esquema de seis Estágios a Geração do DAG é o Estágio 3
# (``ESTAGIO_GERACAO_DAG == 3``; sua Entrada são os atributos da campanha
# selecionada no Estágio 2). A listagem (``GET .../dags``) faz um backfill
# idempotente dos DAGs curados, de modo que um Caso curado sempre exibe ao menos
# o seu DAG de curadoria. Por isso os testes de escolha usam slugs curados
# dedicados: o 409 (SEM_DAG) exige um Caso cujo DAG ainda NÃO foi backfilled,
# então ``escolher-dag`` é chamado ANTES de qualquer ``.../dags`` para esse slug.
#
# Esta seção fica no fim do arquivo de propósito: o ``escolher-dag`` conclui o
# Estágio 3 e o ``.../estagio/3/concluir`` persiste a session-state (via
# SessionStateService), então, no DB compartilhado pelo módulo, estes testes
# precisam rodar DEPOIS de ``test_get_estado_sessao_default_when_none_persisted``
# (que exige nenhuma session-state persistida).
# ===========================================================================


def test_get_dags_lists_curated_backfill(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../dags`` lista os DAGs persistidos; o backfill curado garante ao
    menos 1 DAG ``curadoria_humana`` para um Caso curado (Req. 4.8)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/dags")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    assert isinstance(body["dags"], list) and body["dags"], body
    # Ao menos um DAG curado (curadoria_humana) aparece após o backfill.
    assert any(d["origem"] == "curadoria_humana" for d in body["dags"]), body
    # Cada resumo carrega a identidade esperada (id, origem, identificador).
    for dag in body["dags"]:
        assert isinstance(dag["id"], int)
        assert dag["origem"]
        assert dag["identificador"]
        # criado_em presente; conteudo gráfico NÃO vem no resumo.
        assert dag["criado_em"]
        assert "conteudo" not in dag


def test_get_dags_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """Um slug desconhecido é 404 na listagem de DAGs (convenção de casos.py)."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos/nao_existe/dags")
    assert resp.status_code == 404, resp.text


def test_get_dag_por_id_returns_graph_and_mermaid(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../dags/{dag_id}`` devolve o grafo + Mermaid de um DAG persistido
    (Req. 4.4). O backfill curado garante um ``dag_id`` válido; o payload traz a
    identidade (``dag_id``/``origem``/``identificador``) e a forma gráfica
    (``nos``/``arestas``/``mermaid``) consistentes com ``obter_dag``."""
    client = _make_client(session_factory)

    # Backfill + listagem para obter um dag_id válido do Caso.
    dags = client.get(f"/api/casos/{_REAL_SLUG}/dags").json()["dags"]
    assert dags, "o backfill curado deve persistir ao menos 1 DAG"
    resumo = dags[0]
    dag_id = resumo["id"]

    resp = client.get(f"/api/casos/{_REAL_SLUG}/dags/{dag_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    assert body["dag_id"] == dag_id
    assert body["origem"] == resumo["origem"]
    assert body["identificador"] == resumo["identificador"]
    # Forma gráfica: nós com identidade ATT&CK, arestas {de, para} e Mermaid.
    assert isinstance(body["nos"], list) and body["nos"], body
    assert all("node_id" in no for no in body["nos"])
    assert isinstance(body["arestas"], list)
    assert all({"de", "para"} <= set(a) for a in body["arestas"])
    assert body["mermaid"].startswith("flowchart TD")
    # Cada aresta referencia node_ids presentes entre os nós (consistência).
    node_ids = {no["node_id"] for no in body["nos"]}
    for aresta in body["arestas"]:
        assert aresta["de"] in node_ids
        assert aresta["para"] in node_ids


def test_get_dag_por_id_unknown_dag_id_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """Um ``dag_id`` inexistente/alheio ao Caso → 404 (Req. 4.4). O backfill
    garante que o Caso TEM DAGs, então a ausência é só do ``dag_id`` pedido."""
    client = _make_client(session_factory)
    # Garante que o Caso tem ao menos 1 DAG persistido (backfill).
    client.get(f"/api/casos/{_REAL_SLUG}/dags")

    resp = client.get(f"/api/casos/{_REAL_SLUG}/dags/999999")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["motivo"] == "dag_nao_encontrado"


def test_get_dag_por_id_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """Um slug desconhecido é 404 no detalhe de DAG (convenção de casos.py)."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos/nao_existe/dags/1")
    assert resp.status_code == 404, resp.text


def test_escolher_dag_success_concludes_stage3_and_unblocks_stage4(
    session_factory: sessionmaker[Session],
) -> None:
    """``escolher-dag`` com um ``dag_id`` válido do Caso conclui o Estágio 3
    (Geração do DAG) e libera o Estágio 4 → 200 (Req. 7.6, 6.3).

    No pipeline de seis Estágios a Geração do DAG é o Estágio 3; a escolha grava
    ``dag_escolhido_id`` no ``StageRun`` de ``estagio=3`` e o conclui. O retorno
    é o estado por Estágio (:class:`CaseStageStates`) com o Estágio 3
    ``concluido`` e o Estágio 4 ``iniciavel`` (não bloqueado)."""
    client = _make_client(session_factory)

    # Backfill + listagem para obter um dag_id válido do Caso.
    dags = client.get(f"/api/casos/{_REAL_SLUG}/dags").json()["dags"]
    assert dags, "o backfill curado deve persistir ao menos 1 DAG"
    dag_id = dags[0]["id"]

    resp = client.post(
        f"/api/casos/{_REAL_SLUG}/estagio/3/escolher-dag", json={"dag_id": dag_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _REAL_SLUG
    estagios = {e["estagio"]: e for e in body["estagios"]}
    assert estagios[3]["estado"] == StageState.COMPLETED.value
    assert estagios[3]["progresso"] == 100
    # O Estágio 4 (Operacionalização) fica liberado (não bloqueado) após o 3.
    assert estagios[4]["bloqueado"] is False
    assert estagios[4]["iniciavel"] is True


def test_escolher_dag_unknown_dag_id_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """Um ``dag_id`` inexistente/alheio ao Caso (que já possui DAG) → 404 (Req.
    erro). O backfill garante que o Caso TEM DAG, então a recusa é
    ``DAG_NAO_ENCONTRADO`` (404), não ``SEM_DAG`` (409)."""
    client = _make_client(session_factory)
    # Garante que o Caso tem ao menos 1 DAG persistido (backfill).
    client.get(f"/api/casos/{_REAL_SLUG}/dags")

    resp = client.post(
        f"/api/casos/{_REAL_SLUG}/estagio/3/escolher-dag",
        json={"dag_id": 999999},
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "dag_nao_encontrado"


def test_escolher_dag_without_persisted_dag_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """``escolher-dag`` numa Campanha SEM nenhum DAG persistido → 409 (SEM_DAG,
    Req. 6.2).

    O backfill só roda ao LISTAR (``GET .../dags``); portanto, chamar
    ``escolher-dag`` para um slug curado dedicado ANTES de qualquer listagem
    atinge o ramo ``SEM_DAG`` (nenhum DAG persistido). Usa um slug próprio para
    não colidir com o backfill disparado por outros testes no DB compartilhado
    do módulo."""
    client = _make_client(session_factory)
    slug_sem_dag = "operation_midnighteclipse"

    resp = client.post(
        f"/api/casos/{slug_sem_dag}/estagio/3/escolher-dag",
        json={"dag_id": 1},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "sem_dag"


# ===========================================================================
# Estágio 2 — Seleção da campanha (esquema de SEIS Estágios, task 3.2):
# GET  /api/casos/{caso}/estagio/2            — campanhas do bundle + atributos
# POST /api/casos/{caso}/estagio/2/selecionar — seleciona → extrai atributos
#
# No esquema de SEIS Estágios o Estágio 2 é a "Seleção da campanha": o GET lista
# as campanhas do bundle selecionado no Estágio 1 (marcando curada/selecionável)
# e devolve os atributos numa visão read-only; o POST seleciona uma campanha e
# extrai os seus atributos, concluindo o Estágio quando a extração tem sucesso.
# A Geração do DAG migrou para o Estágio 3 (tarefa 4.1). Estes testes usam o
# slug curado real (``_REAL_SLUG``) e o bundle real em disco (mesma convenção
# dos demais testes de superfície que leem artefatos curados reais).
# ===========================================================================


def test_get_estagio2_lists_campaigns_and_readonly_attributes(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../estagio/2`` lista as campanhas do bundle (com ``curada``/
    ``selecionavel``) e devolve a visão read-only dos atributos (Req. 6.2, 6.4).

    A versão padrão é a curada (18.0); as 8 campanhas curadas aparecem
    selecionáveis. A visão read-only (``selecao``) carrega o ``caso_id`` e a
    identidade, sem executar nem concluir o Estágio ao abrir a tela."""
    from app.services.stix.stix_service import VERSAO_CURADA

    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_REAL_SLUG}/estagio/2")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["caso_id"] == _REAL_SLUG
    assert body["versao"] == VERSAO_CURADA

    # Lista de campanhas do bundle: ao menos as 8 curadas selecionáveis.
    campanhas = body["campanhas"]
    assert isinstance(campanhas, list) and campanhas, body
    selecionaveis = [c for c in campanhas if c["selecionavel"]]
    assert len(selecionaveis) >= 8, selecionaveis
    # Toda campanha selecionável é curada e tem slug_curado.
    for c in selecionaveis:
        assert c["curada"] is True
        assert c["slug_curado"]

    # Visão read-only da seleção corrente: identidade presente, nunca executa.
    selecao = body["selecao"]
    assert selecao["caso_id"] == _REAL_SLUG
    assert selecao["identidade"] is not None


def test_get_estagio2_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """Um slug de Caso desconhecido é 404 no Estágio 2 (convenção de casos.py)."""
    client = _make_client(session_factory)
    resp = client.get("/api/casos/nao_existe/estagio/2")
    assert resp.status_code == 404, resp.text


def test_post_estagio2_selecionar_extracts_attributes_and_concludes(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../estagio/2/selecionar`` extrai os atributos da campanha curada e
    conclui o Estágio quando a extração tem sucesso (Req. 6.3, 6.4, 6.6).

    Para uma campanha curada real (``_REAL_SLUG``) a extração estrutural do DAG
    curado produz as técnicas associadas; o resultado tipado traz a identidade,
    as técnicas e ``concluido == True`` sem causa de erro."""
    client = _make_client(session_factory)
    resp = client.post(
        f"/api/casos/{_REAL_SLUG}/estagio/2/selecionar",
        json={"slug": _REAL_SLUG},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["caso_id"] == _REAL_SLUG
    assert body["concluido"] is True
    assert body["identidade"] is not None
    assert isinstance(body["tecnicas"], list) and body["tecnicas"], body
    assert body["mensagem_erro"] is None
    assert body["etapa_falha"] is None


def test_post_estagio2_selecionar_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """Um slug de Caso desconhecido na rota é 404 (validação de casos.py)."""
    client = _make_client(session_factory)
    resp = client.post(
        "/api/casos/nao_existe/estagio/2/selecionar",
        json={"slug": "nao_existe"},
    )
    assert resp.status_code == 404, resp.text


# ===========================================================================
# POST /api/casos/{caso}/estagio/3/concluir — bloqueio sequencial (antigo 2)
# ===========================================================================


def test_concluir_estagio3_respects_sequential_block(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../estagio/3/concluir`` respeita o bloqueio sequencial: 409
    enquanto o Estágio 2 não está concluído; sucesso após satisfazer o
    pré-requisito (Req. 1.4, 1.7).

    Usa um slug curado dedicado para não colidir com o estado por Estágio que
    outros testes do módulo escrevem no DB compartilhado."""
    client = _make_client(session_factory)
    slug = "c0026"

    # Sem o Estágio 2 concluído, concluir o Estágio 3 é 409 (bloqueio).
    bloqueado = client.post(f"/api/casos/{slug}/estagio/3/concluir")
    assert bloqueado.status_code == 409, bloqueado.text
    assert bloqueado.json()["detail"]["motivo"] == "bloqueio_sequencial"

    # Satisfaz o pré-requisito do gate: o Estágio 2 (Modelagem Estrutural) do
    # Caso precisa estar concluido. Semeia essa conclusão gravando diretamente a
    # linha de StageRun do Estágio 2 (a fonte da verdade lida pelo gate
    # sequencial do endpoint), sem tocar a session-state — assim o gate é
    # exercido de forma determinística e isolada dos demais testes do módulo.
    db = session_factory()
    try:
        db.add(
            StageRun(
                caso_id=slug,
                estagio=2,
                estado=StageState.COMPLETED,
                progresso=100,
            )
        )
        db.commit()
    finally:
        db.close()

    # Agora o Estágio 3 conclui com sucesso e o retorno traz o 3 concluido.
    ok = client.post(f"/api/casos/{slug}/estagio/3/concluir")
    assert ok.status_code == 200, ok.text
    estagios = {e["estagio"]: e for e in ok.json()["estagios"]}
    assert estagios[3]["estado"] == StageState.COMPLETED.value


# ===========================================================================
# POST /api/casos/{caso}/estagio/4/concluir — Operacionalização (seis Estágios)
#
# No esquema de SEIS Estágios, a Operacionalização é o Estágio 4: a conclusão
# exige o Estágio 3 (Geração do DAG) concluído (bloqueio sequencial) E os dois
# artefatos produzidos (Abilities ordenadas + ParametrosDeAmbiente, Req. 8.8).
# Como estes testes PERSISTEM session-state (via persist_stage_completion), eles
# ficam no fim do arquivo — junto dos demais testes que escrevem no DB
# compartilhado do módulo — para não interferir com os testes de estado-sessão
# que esperam nenhuma persistência prévia.
# ===========================================================================


def test_post_estagio4_concluir_requires_estagio3(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../estagio/4/concluir`` is blocked while Estágio 3 is not
    concluído (sequential block — 409, Req. 3.x, 8.8).

    Após um ``reset`` o Estágio 3 (Geração do DAG) volta a não concluído; como
    ``concluir_estagio4`` lê o estado persistido diretamente (sem disparar a
    auto-conclusão curada), a conclusão do Estágio 4 é recusada com 409 e o motivo
    ``bloqueio_sequencial``, sem persistir nada."""
    slug = "costaricto"  # slug curado dedicado a este teste no DB do módulo
    client = _make_client(session_factory)
    # Garante Estágio 3 NÃO concluído (reset limpa os StageRun do Caso).
    assert client.post(f"/api/casos/{slug}/reset").status_code == 200
    resp = client.post(f"/api/casos/{slug}/estagio/4/concluir")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["motivo"] == "bloqueio_sequencial"


def test_post_estagio4_concluir_completes_after_estagio3(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../estagio/4/concluir`` conclui o Estágio 4 (Operacionalização)
    quando o Estágio 3 está concluído e os dois artefatos existem (Req. 8.7, 8.8).

    Primeiro escolhe-se um DAG (conclui o Estágio 3 e libera o 4); então concluir
    o Estágio 4 produz as Abilities + ParametrosDeAmbiente, persiste a conclusão
    e devolve o estado por Estágio com o 4 ``concluido`` e o 5 liberado."""
    slug = "apt41_dust"  # slug curado dedicado a este teste no DB do módulo
    client = _make_client(session_factory)

    # Conclui o Estágio 3 escolhendo o DAG curado (backfilled ao listar).
    dags = client.get(f"/api/casos/{slug}/dags").json()["dags"]
    assert dags, "esperado ao menos 1 DAG curado backfilled"
    dag_id = dags[0]["id"]
    escolha = client.post(
        f"/api/casos/{slug}/estagio/3/escolher-dag", json={"dag_id": dag_id}
    )
    assert escolha.status_code == 200, escolha.text

    resp = client.post(f"/api/casos/{slug}/estagio/4/concluir")
    assert resp.status_code == 200, resp.text
    estagios = {e["estagio"]: e for e in resp.json()["estagios"]}
    assert estagios[4]["estado"] == StageState.COMPLETED.value
    assert estagios[4]["progresso"] == 100
    # O Estágio 5 (Criação do ambiente) fica liberado após o 4.
    assert estagios[5]["bloqueado"] is False
    assert estagios[5]["iniciavel"] is True
