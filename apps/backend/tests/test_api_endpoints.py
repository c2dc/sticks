"""Integration tests of the REST surface with mocked services (task 11.4).

This suite complements the focused ``test_emulation_endpoints.py`` (task 11.2) by
covering the *broader* REST surface end-to-end at the HTTP layer, plus the full
``preview -> emulation`` sequence, all with the external world mocked (no real
Caldera, no real Docker daemon):

* the campaigns / stages surface — ``GET /api/campaigns`` (the 8 curated cases +
  aggregate progress), ``GET /api/campaigns/{campaign}``, the stage reads
  ``GET /api/campaigns/{campaign}/stage/{1,3,5}`` (stage 3 is DAG Generation —
  DagView/Mermaid), ``POST .../stage/1/execute``, the DAG endpoints
  (``GET .../dags`` and ``POST .../stage/3/choose-dag`` with its 200/409/404
  outcomes), the sequential-block ``POST .../stage/3/complete`` and
  ``GET /api/campaigns/{campaign}/operation``;
* the session / preferences / audit surface — ``GET /api/state-sessao``,
  ``PUT /api/preferences`` and ``GET /api/audit/{operation}``;
* the emulation flow end-to-end — ``POST .../emulation/preview`` followed by
  ``POST .../emulation``, exercising the four gates as HTTP outcomes:
    - 409 on a containment violation (external destination, Req. 6.2),
    - 409 on an isolation failure (non-isolated container, Req. 6.4),
    - 503 on Caldera unavailable/timeout (Req. 4.6),
    - ``confirmed=false`` -> nothing started (Req. 6.6),
    - the happy path (internal-only + isolated + healthy Caldera).

Isolation from ``test_emulation_endpoints.py``
--------------------------------------------
The emulation-outcome cases here are driven through the full ``preview ->
emulation`` *sequence* (a preview call always precedes the execute call, matching
the design's sequence diagram and the confirmation-modal flow), rather than the
single execute calls the sibling suite uses. The REST-surface tests (campaigns /
stages / session-state / preferences / audit) are entirely new coverage
not present in the sibling suite.

Everything external is mocked:
- The database is an ephemeral temp-file SQLite created per test module and
  wired into the app via the ``get_db`` dependency override.
- Caldera is a real :class:`CalderaClient` backed by :class:`httpx.MockTransport`
  (healthy or timeout-raising) injected via ``get_caldera_client``.
- Isolation is a fake verifier injected by overriding ``get_runner_factory``.
- Containment uses the real validator over the (real or stubbed) case abilities.

_Requirements: 6.2, 6.4, 4.6, 6.6_
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

from app.api.emulation import get_caldera_client, get_runner_factory
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
from app.services.campaign_service import CURATED_CASES
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
            reasons=("conectado à local-network (bridge)", "dns external 8.8.8.8"),
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


def _internal_only_case(campaign: str) -> object:
    """A curated-case stand-in with a single internal-only ability (⊆ 172.21/24)."""
    from app.services.campaign_service import (
        AbilityData,
        AdversaryData,
        CaseData,
        Executor,
    )

    return CaseData(
        id=campaign,
        name=campaign,
        ability_file="x",
        adversary_file="x",
        dag_file="x",
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


def _external_destination_case(campaign: str) -> object:
    """A deterministic stand-in containing one destination outside the lab."""
    from app.services.campaign_service import (
        AbilityData,
        AdversaryData,
        CaseData,
        Executor,
    )

    return CaseData(
        id=campaign,
        name=campaign,
        ability_file="x",
        adversary_file="x",
        dag_file="x",
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
    """Context manager swapping the emulation handler's case loader for a stub.

    Monkeypatches ``app.api.emulation._load_case_or_error`` to yield the
    internal-only stand-in so the destination gate passes and the isolation /
    availability gates can be exercised in isolation. Restores the original
    loader on exit even if the request raised.
    """

    def __enter__(self):
        from app.api import emulation as emulation_module

        self._module = emulation_module
        self._original = emulation_module._load_case_or_error
        emulation_module._load_case_or_error = (  # type: ignore[assignment]
            lambda campaign, case_service: _internal_only_case(campaign)
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self._module._load_case_or_error = self._original  # type: ignore[assignment]


class _external_case_loader(_internal_case_loader):
    """Swap the emulation loader for the deterministic external case."""

    def __enter__(self):
        from app.api import emulation as emulation_module

        self._module = emulation_module
        self._original = emulation_module._load_case_or_error
        emulation_module._load_case_or_error = (  # type: ignore[assignment]
            lambda campaign, case_service: _external_destination_case(campaign)
        )
        return self


# ===========================================================================
# GET /api/campaigns — the 8 curated cases + aggregate progress
# ===========================================================================


def test_list_campaigns_lists_eight_cases_and_aggregate_progress(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/campaigns`` lists all 8 curated cases with per-stage state (6
    stages each) and the aggregate progress over 8 (Req. 5.1, 5.5)."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Exactly the 8 curated cases, by their registered slugs.
    slugs = {c["id"] for c in body["campaigns"]}
    assert slugs == set(CURATED_CASES), slugs
    assert len(body["campaigns"]) == 8

    # Each successfully-loaded case carries its per-stage states (6 stages, the
    # renumbered 1..6 scheme) and no load error.
    for campaign in body["campaigns"]:
        assert campaign["error"] is None, campaign
        assert campaign["states"] is not None
        assert len(campaign["states"]["stages"]) == 6
        # The six stages are numbered 1..6 in order.
        assert [e["stage"] for e in campaign["states"]["stages"]] == [
            1,
            2,
            3,
            4,
            5,
            6,
        ]

    # Aggregate progress spans the 8-case universe. No single case has all six
    # stages completed, so nothing is counted as fully replicated.
    progress = body["progress"]
    assert progress["total_campaigns"] == 8
    assert progress["completed_campaigns"] == 0
    assert progress["percentage"] == 0


# ===========================================================================
# GET /api/campaigns/{campaign} — case detail
# ===========================================================================


def test_detail_campaign_returns_abilities_adversary_and_origin(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/campaigns/{campaign}`` returns the curated Abilities, Adversary,
    ordering and the explicit translation source (Req. 3.2, 3.3, 3.5, 11.1)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["id"] == _REAL_SLUG
    assert body["translation_source"] == "curadoria_humana"
    assert isinstance(body["abilities"], list) and body["abilities"]
    # Every ability carries its stable id.
    for ability in body["abilities"]:
        assert ability["ability_id"]
    # The ordering mirrors the adversary's atomic_ordering when present.
    if body["adversary"] is not None:
        assert body["atomic_ordering"] == body["adversary"]["atomic_ordering"]


def test_detail_campaign_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown case slug is a 404 (not one of the 8 curated cases)."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/nao_existe")
    assert resp.status_code == 404, resp.text


# ===========================================================================
# GET /api/campaigns/{campaign}/stage/{1,2,3,4,5} — renumbered 1..5 scheme
#
# In the renumbered scheme (spec pipeline-cinco-stages): Stage 1 = Structural
# Modeling, Stage 2 = DAG Generation (covered in the DAGs section below),
# Stage 3 = Operationalization, Stage 4 = Environment Provisioning
# (topology + isolation), Stage 5 = Campaign Execution.
# ===========================================================================


def test_get_stage1_returns_bundle_dashboard(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET .../stage/1`` returns the Stage-1 (STIX-base Selection) bundle
    dashboard + conclusion verdict (six Stages, Req. 5.7, 5.8, 5.10).

    In the SIX-Stage scheme, Stage 1 is the STIX-base Selection. The handler
    delegates to ``Stage1Service``; we pin its ``StixService`` to a temp data
    dir and seed the curated bundle so the stage concludes with no network
    egress (opening the screen without an explicit version defaults to the
    curated version)."""
    import app.services.stix.stage1_service as stage1_module
    from app.services.stix.stix_service import CURATED_VERSION, StixService

    base = StixService(data_dir=tmp_path).base_path(CURATED_VERSION)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        json.dumps({"type": "bundle", "objects": []}), encoding="utf-8"
    )
    monkeypatch.setattr(
        stage1_module,
        "StixService",
        lambda *a, **k: StixService(data_dir=tmp_path),
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/stage/1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == CURATED_VERSION
    assert body["completed"] is True
    assert body["bundle_on_disk"] is True
    assert body["dashboard"] is not None


def test_get_stage3_returns_dag_view(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../stage/3`` returns the DAG view (DAG Generation — Req. 7.2, 7.3).

    In the six-Stage scheme, DAG Generation is Stage 3, served as a
    :class:`DagView` (graph + Mermaid). For a valid curated campaign the DAG is
    ``curated``, ``can_advance`` and carries nodes and the Mermaid diagram."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/stage/3")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _REAL_SLUG
    assert body["curated"] is True
    assert body["can_advance"] is True
    assert len(body["nodes"]) > 0
    assert body["mermaid"]


def test_get_stage4_returns_abilities_and_parameters(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../stage/4`` returns the Operationalization view — Abilities +
    EnvironmentParameters (six Stages, Req. 8.4, 8.7).

    In the SIX-Stage scheme, Stage 4 is Operationalization: the Output is **two
    separate typed artifacts** — the Abilities ordered by the DAG and the
    EnvironmentParameters derived from the curated nodes. For a valid curated
    campaign, both are produced (``both_produced``), the platform is
    ``conteiner`` and the origin is ``curadoria_humana``. It is read-only:
    opening the screen does not complete the Stage."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/stage/4")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _REAL_SLUG
    # 1st artifact: ordered Abilities (Stage2Output) with at least one Ability.
    assert body["abilities"] is not None
    assert body["abilities"]["has_abilities"] is True
    assert len(body["abilities"]["abilities"]) > 0
    assert len(body["abilities"]["ordering"]) > 0
    # 2nd artifact: EnvironmentParameters (platform conteiner, origin curated).
    assert body["parameters"]["campaign_id"] == _REAL_SLUG
    assert body["parameters"]["platform"] == "conteiner"
    assert body["parameters"]["origin"] == TranslationSource.HUMAN_CURATION.value
    # Both produced → completion criterion satisfied (Req. 8.8).
    assert body["both_produced"] is True
    assert body["origin"] == TranslationSource.HUMAN_CURATION.value
    assert "stages" in body["states"]


def test_get_stage6_returns_input_and_accumulated_output(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../stage/6`` returns the Adversary+Abilities Input and the
    accumulated Output (Execution, measurement and report — Req. 10.x).

    In the SIX-Stage scheme, Execution is Stage 6 (the old Stage 5 of the
    five-stage scheme). The accumulated Output is a list (empty before any
    emulation runs)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/stage/6")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["input"]["campaign_id"] == _REAL_SLUG
    assert isinstance(body["output"], list)


def test_get_stage_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug is a 404 on every stage endpoint (1..6)."""
    client = _make_client(session_factory)
    for stage in (1, 2, 3, 4, 5, 6):
        resp = client.get(f"/api/campaigns/nao_existe/stage/{stage}")
        assert resp.status_code == 404, resp.text


# ===========================================================================
# POST /api/campaigns/{campaign}/stage/1/execute — structural modeling (Stage 1)
# ===========================================================================


def test_post_stage1_execute_runs_structural_modeling(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../stage/1/execute`` triggers structural modeling and reports
    a typed Stage result (Req. 1.3, 1.7). For a real curated case it completes
    and persists a StageRun row for Stage 1.

    In the five-Stage scheme Structural Modeling is Stage 1, so execution lives
    at ``.../stage/1/execute``."""
    client = _make_client(session_factory)
    resp = client.post(f"/api/campaigns/{_REAL_SLUG}/stage/1/execute")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _REAL_SLUG
    # A real curated case yields a completed (completed) stage, not an error:
    # the structural modeling ran and persisted its typed result.
    assert body["state"] == StageState.COMPLETED.value
    assert body["output"] is not None
    assert body["stage_run_id"] is not None

    # The modeling write reached the shared DB: a completed StageRun is now
    # visible in GET /api/campaigns for this case (the per-stage state reflects the
    # persisted modeling run).
    listing = client.get("/api/campaigns").json()
    campaign = next(c for c in listing["campaigns"] if c["id"] == _REAL_SLUG)
    assert any(
        e["state"] == StageState.COMPLETED.value
        for e in campaign["states"]["stages"]
    ), campaign["states"]["stages"]


# ===========================================================================
# GET /api/campaigns/{campaign}/operation
# ===========================================================================


def test_get_operation_empty_before_any_emulation(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../operation`` returns an empty operations list before any run."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/costaricto/operation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == "costaricto"
    assert body["operations"] == []


def test_get_operation_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug is a 404 on the operation endpoint."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/nao_existe/operation")
    assert resp.status_code == 404, resp.text


# ===========================================================================
# GET /api/state-sessao
# ===========================================================================


def test_get_session_state_default_when_none_persisted(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/state-sessao`` returns the default all-not-started state when
    no session state is persisted (Req. 9.5)."""
    client = _make_client(session_factory)
    resp = client.get("/api/session-state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exists"] is False
    assert body["restore_failed"] is False
    assert body["current_campaign"] is None
    assert isinstance(body["completed_stages_by_campaign"], dict)


# ===========================================================================
# PUT /api/preferences
# ===========================================================================


def test_put_preferences_persists_theme_and_language(
    session_factory: sessionmaker[Session],
) -> None:
    """``PUT /api/preferences`` persists the theme + language and echoes the
    stored values back (Req. 7.4, 8.4)."""
    client = _make_client(session_factory)
    resp = client.put(
        "/api/preferences", json={"theme": "escuro", "language": "en"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"theme": "escuro", "language": "en"}

    # A partial update keeps the omitted field at its previously persisted value.
    resp2 = client.put("/api/preferences", json={"theme": "claro"})
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["theme"] == "claro"
    assert body2["language"] == "en"


def test_put_preferences_rejects_invalid_theme(
    session_factory: sessionmaker[Session],
) -> None:
    """An out-of-enum theme value is a 422 (schema validation)."""
    client = _make_client(session_factory)
    resp = client.put("/api/preferences", json={"theme": "neon"})
    assert resp.status_code == 422, resp.text


# ===========================================================================
# GET /api/audit/{operation}
# ===========================================================================


def test_get_audit_returns_persisted_trail_oldest_first(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/audit/{operation}`` returns the persisted audit trail of an
    Operation — one row per command, oldest first (Req. 6.8)."""
    # Seed an Operation with two audit rows directly via the shared factory.
    db = session_factory()
    try:
        op = Operation(
            campaign_id="c0010",
            state=OperationState.FINISHED,
            total_success=1,
            total_failure=1,
        )
        db.add(op)
        db.flush()
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        db.add_all(
            [
                AuditLogEntry(
                    operation_id=op.id,
                    ability_id="a-1",
                    command="curl http://192.168.20.30/a",
                    target_container="target-a",
                    result="sucesso",
                    recorded_at=now,
                ),
                AuditLogEntry(
                    operation_id=op.id,
                    ability_id="a-2",
                    command="curl http://192.168.20.31/b",
                    target_container="target-b",
                    result="falha",
                    recorded_at=now + dt.timedelta(seconds=1),
                ),
            ]
        )
        db.commit()
        op_id = op.id
    finally:
        db.close()

    client = _make_client(session_factory)
    resp = client.get(f"/api/audit/{op_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation_id"] == op_id
    assert len(body["records"]) == 2
    # Oldest first: ids strictly ascending, one row per executed command.
    ids = [r["id"] for r in body["records"]]
    assert ids == sorted(ids)
    assert body["records"][0]["command"] == "curl http://192.168.20.30/a"
    assert body["records"][0]["target_container"] == "target-a"


def test_get_audit_unknown_operation_returns_empty_trail(
    session_factory: sessionmaker[Session],
) -> None:
    """An operation with no audit rows returns an empty (but 200) trail."""
    client = _make_client(session_factory)
    resp = client.get("/api/audit/999999")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation_id"] == 999999
    assert body["records"] == []


# ===========================================================================
# Emulation flow end-to-end: POST .../emulation/preview -> POST .../emulation
# ===========================================================================


def test_preview_then_external_destination_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulation: an external destination is flagged and refused."""
    client = _make_client(session_factory)

    with _external_case_loader():
        preview = client.post(f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation/preview")
        assert preview.status_code == 200, preview.text
        assert preview.json()["has_external"] is True

        run = client.post(
            f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation", json={"confirmed": True}
        )
    assert run.status_code == 409, run.text
    detail = run.json()["detail"]
    assert detail["motivo"] == "contencao_recusada"
    assert detail["violacoes"], "the offending Ability/command must be identified"


def test_preview_then_non_isolated_container_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulation (internal-only case): containment passes but a
    non-isolated container yields 409 identifying the container (Req. 6.4)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_not_isolated_verifier,
    )
    with _internal_case_loader():
        preview = client.post(f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation/preview")
        assert preview.status_code == 200, preview.text
        # Internal-only: nothing external in the preview.
        assert preview.json()["has_external"] is False

        run = client.post(
            f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation", json={"confirmed": True}
        )
    assert run.status_code == 409, run.text
    detail = run.json()["detail"]
    assert detail["motivo"] == "isolamento_falhou"
    assert detail["containers"], "the offending container must be identified"


def test_preview_then_caldera_timeout_returns_503(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulation (internal-only + isolated): a silent Caldera within
    10s yields 503; nothing started (Req. 4.6)."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_timeout_router),
        isolation_verifier=_isolated_verifier,
    )
    with _internal_case_loader():
        preview = client.post(f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation", json={"confirmed": True}
        )
    assert run.status_code == 503, run.text
    assert "indisponível" in run.json()["detail"].lower()


def test_preview_then_not_confirmed_runs_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulation with confirmed=false starts nothing (Req. 6.6).

    The preview is shown (the modal step) and then, without explicit
    confirmation, the execute call runs nothing: no operation id, state stays
    not-started, and no audit rows are written for it.
    """
    client = _make_client(session_factory)
    with _internal_case_loader():
        preview = client.post(f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation", json={"confirmed": False}
        )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["started"] is False
    assert body["result"] == "nao_confirmada"
    assert body["state"] == "nao_iniciada"
    assert body["operation_id"] is None

    # Nothing ran => no Operation was created for this case (and therefore no
    # ability results / audit rows tied to it). Scoped to the case so the
    # module-shared DB (which other tests seed) does not create false failures.
    db = session_factory()
    try:
        assert (
            db.query(Operation).filter(Operation.campaign_id == _REAL_SLUG).count() == 0
        )
    finally:
        db.close()


def test_preview_then_confirmed_happy_path_succeeds_with_aggregate(
    session_factory: sessionmaker[Session],
) -> None:
    """preview -> emulation (internal-only + isolated + healthy Caldera) with
    confirmed=true succeeds, returning the aggregate and per-Ability results."""
    client = _make_client(
        session_factory,
        transport=httpx.MockTransport(_healthy_router),
        isolation_verifier=_isolated_verifier,
    )
    with _internal_case_loader():
        preview = client.post(f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation/preview")
        assert preview.status_code == 200, preview.text

        run = client.post(
            f"/api/campaigns/{_REAL_SLUG}/stage/5/emulation", json={"confirmed": True}
        )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["started"] is True
    assert body["result"] == "concluida"
    assert body["state"] == "finalizada"
    assert body["total_success"] == 1
    assert body["total_failure"] == 0
    assert body["results"], "per-Ability results must be present"


# ===========================================================================
# Stage 3 — DAG Generation: GET /api/campaigns/{campaign}/dags and
# POST /api/campaigns/{campaign}/stage/3/choose-dag (Req. 4.8, 7.2, 7.3)
#
# In the six-Stage scheme DAG Generation is Stage 3
# (``STAGE_DAG_GENERATION == 3``; its Input is the attributes of the campaign
# selected in Stage 2). The listing (``GET .../dags``) performs an idempotent
# backfill of the curated DAGs, so a curated campaign always shows at least its
# curation DAG. That is why the choice tests use dedicated curated slugs: the
# 409 (NO_DAG) requires a campaign whose DAG has NOT yet been backfilled, so
# ``choose-dag`` is called BEFORE any ``.../dags`` for that slug.
#
# This section is at the end of the file on purpose: ``choose-dag`` completes
# Stage 3 and ``.../stage/3/complete`` persists the session-state (via
# SessionStateService), so, in the module's shared DB, these tests must run
# AFTER ``test_get_session_state_default_when_none_persisted`` (which requires
# no session-state persisted).
# ===========================================================================


def test_get_dags_lists_curated_backfill(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../dags`` lists the persisted DAGs; the curated backfill guarantees
    at least 1 ``curadoria_humana`` DAG for a curated campaign (Req. 4.8)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/dags")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _REAL_SLUG
    assert isinstance(body["dags"], list) and body["dags"], body
    # At least one curated DAG (curadoria_humana) appears after the backfill.
    assert any(d["origin"] == "curadoria_humana" for d in body["dags"]), body
    # Each summary carries the expected identity (id, origin, identifier).
    for dag in body["dags"]:
        assert isinstance(dag["id"], int)
        assert dag["origin"]
        assert dag["identifier"]
        # created_at present; graph content does NOT come in the summary.
        assert dag["created_at"]
        assert "content" not in dag


def test_get_dags_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug is a 404 on the DAG listing (campaigns.py convention)."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/nao_existe/dags")
    assert resp.status_code == 404, resp.text


def test_get_dag_por_id_returns_graph_and_mermaid(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../dags/{dag_id}`` returns the graph + Mermaid of a persisted DAG
    (Req. 4.4). The curated backfill guarantees a valid ``dag_id``; the payload
    carries the identity (``dag_id``/``origin``/``identifier``) and the graph
    shape (``nodes``/``edges``/``mermaid``) consistent with ``get_dag``."""
    client = _make_client(session_factory)

    # Backfill + listing to obtain a valid dag_id for the campaign.
    dags = client.get(f"/api/campaigns/{_REAL_SLUG}/dags").json()["dags"]
    assert dags, "the curated backfill must persist at least 1 DAG"
    resumo = dags[0]
    dag_id = resumo["id"]

    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/dags/{dag_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _REAL_SLUG
    assert body["dag_id"] == dag_id
    assert body["origin"] == resumo["origin"]
    assert body["identifier"] == resumo["identifier"]
    # Graph shape: nodes with ATT&CK identity, edges {de, para} and Mermaid.
    assert isinstance(body["nodes"], list) and body["nodes"], body
    assert all("node_id" in no for no in body["nodes"])
    assert isinstance(body["edges"], list)
    assert all({"source", "target"} <= set(a) for a in body["edges"])
    assert body["mermaid"].startswith("flowchart TD")
    # Each edge references node_ids present among the nodes (consistency).
    node_ids = {no["node_id"] for no in body["nodes"]}
    for edge in body["edges"]:
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids


def test_get_dag_por_id_unknown_dag_id_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """A non-existent ``dag_id`` or one not belonging to the campaign → 404 (Req.
    4.4). The backfill guarantees the campaign HAS DAGs, so only the requested
    ``dag_id`` is absent."""
    client = _make_client(session_factory)
    # Ensure the campaign has at least 1 persisted DAG (backfill).
    client.get(f"/api/campaigns/{_REAL_SLUG}/dags")

    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/dags/999999")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["motivo"] == "dag_nao_encontrado"


def test_get_dag_por_id_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown slug is a 404 on the DAG detail (campaigns.py convention)."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/nao_existe/dags/1")
    assert resp.status_code == 404, resp.text


def test_choose_dag_success_concludes_stage3_and_unblocks_stage4(
    session_factory: sessionmaker[Session],
) -> None:
    """``choose-dag`` with a valid ``dag_id`` for the campaign completes Stage 3
    (DAG Generation) and unblocks Stage 4 → 200 (Req. 7.6, 6.3).

    In the six-Stage pipeline DAG Generation is Stage 3; the choice writes
    ``chosen_dag_id`` on the ``stage=3`` ``StageRun`` and completes it. The
    return value is the per-Stage state (:class:`CaseStageStates`) with Stage 3
    ``completed`` and Stage 4 ``startable`` (not blocked)."""
    client = _make_client(session_factory)

    # Backfill + listing to obtain a valid dag_id for the campaign.
    dags = client.get(f"/api/campaigns/{_REAL_SLUG}/dags").json()["dags"]
    assert dags, "the curated backfill must persist at least 1 DAG"
    dag_id = dags[0]["id"]

    resp = client.post(
        f"/api/campaigns/{_REAL_SLUG}/stage/3/choose-dag", json={"dag_id": dag_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _REAL_SLUG
    stages = {e["stage"]: e for e in body["stages"]}
    assert stages[3]["state"] == StageState.COMPLETED.value
    assert stages[3]["progress"] == 100
    # Stage 4 (Operationalization) becomes available (not blocked) after 3.
    assert stages[4]["blocked"] is False
    assert stages[4]["startable"] is True


def test_choose_dag_unknown_dag_id_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """A non-existent ``dag_id`` or one not belonging to the campaign (which
    already has a DAG) → 404 (error). The backfill guarantees the campaign HAS a
    DAG, so the refusal is ``DAG_NOT_FOUND`` (404), not ``NO_DAG`` (409)."""
    client = _make_client(session_factory)
    # Ensure the campaign has at least 1 persisted DAG (backfill).
    client.get(f"/api/campaigns/{_REAL_SLUG}/dags")

    resp = client.post(
        f"/api/campaigns/{_REAL_SLUG}/stage/3/choose-dag",
        json={"dag_id": 999999},
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "dag_nao_encontrado"


def test_choose_dag_without_persisted_dag_returns_409(
    session_factory: sessionmaker[Session],
) -> None:
    """``choose-dag`` on a campaign WITHOUT any persisted DAG → 409 (NO_DAG,
    Req. 6.2).

    The backfill only runs on LISTING (``GET .../dags``); therefore, calling
    ``choose-dag`` for a dedicated curated slug BEFORE any listing hits the
    ``NO_DAG`` branch (no persisted DAG). Uses its own slug so as not to collide
    with the backfill triggered by other tests in the module's shared DB."""
    client = _make_client(session_factory)
    slug_sem_dag = "operation_midnighteclipse"

    resp = client.post(
        f"/api/campaigns/{slug_sem_dag}/stage/3/choose-dag",
        json={"dag_id": 1},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "no_dag"


# ===========================================================================
# Stage 2 — Campaign Selection (SIX-Stage scheme, task 3.2):
# GET  /api/campaigns/{campaign}/stage/2        — bundle campaigns + attributes
# POST /api/campaigns/{campaign}/stage/2/select — select → extract attributes
#
# In the SIX-Stage scheme Stage 2 is the "Campaign Selection": the GET lists the
# campaigns of the bundle selected in Stage 1 (marking curated/selectable) and
# returns the attributes in a read-only view; the POST selects a campaign and
# extracts its attributes, completing the Stage when the extraction succeeds.
# DAG Generation moved to Stage 3 (task 4.1). These tests use the real curated
# slug (``_REAL_SLUG``) and the real on-disk bundle (same convention as the
# other surface tests that read real curated artifacts).
# ===========================================================================


def test_get_stage2_lists_campaigns_and_readonly_attributes(
    session_factory: sessionmaker[Session],
) -> None:
    """``GET .../stage/2`` lists the bundle campaigns (with ``curated``/
    ``selectable``) and returns the read-only view of the attributes (Req. 6.2,
    6.4).

    The default version is the curated one (18.0); the 8 curated campaigns appear
    as selectable. The read-only view (``selection``) carries the ``campaign_id``
    and the identity, without running or completing the Stage when opening the
    screen."""
    from app.services.stix.stix_service import CURATED_VERSION

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_REAL_SLUG}/stage/2")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["campaign_id"] == _REAL_SLUG
    assert body["version"] == CURATED_VERSION

    # List of bundle campaigns: at least the 8 curated selectable ones.
    campaigns = body["campaigns"]
    assert isinstance(campaigns, list) and campaigns, body
    selecionaveis = [c for c in campaigns if c["selectable"]]
    assert len(selecionaveis) >= 8, selecionaveis
    # Every selectable campaign is curated and has a curated_slug.
    for c in selecionaveis:
        assert c["curated"] is True
        assert c["curated_slug"]

    # Read-only view of the current selection: identity present, never runs.
    selection = body["selection"]
    assert selection["campaign_id"] == _REAL_SLUG
    assert selection["identity"] is not None


def test_get_stage2_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown campaign slug is a 404 on Stage 2 (campaigns.py convention)."""
    client = _make_client(session_factory)
    resp = client.get("/api/campaigns/nao_existe/stage/2")
    assert resp.status_code == 404, resp.text


def test_post_stage2_select_extracts_attributes_and_concludes(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../stage/2/select`` extracts the curated campaign's attributes and
    completes the Stage when the extraction succeeds (Req. 6.3, 6.4, 6.6).

    For a real curated campaign (``_REAL_SLUG``) the structural extraction of the
    curated DAG produces the associated techniques; the typed result carries the
    identity, the techniques and ``completed == True`` with no error cause."""
    client = _make_client(session_factory)
    resp = client.post(
        f"/api/campaigns/{_REAL_SLUG}/stage/2/select",
        json={"slug": _REAL_SLUG},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["campaign_id"] == _REAL_SLUG
    assert body["completed"] is True
    assert body["identity"] is not None
    assert isinstance(body["techniques"], list) and body["techniques"], body
    assert body["error_message"] is None
    assert body["failed_step"] is None


def test_post_stage2_select_unknown_slug_returns_404(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown campaign slug in the route is a 404 (campaigns.py validation)."""
    client = _make_client(session_factory)
    resp = client.post(
        "/api/campaigns/nao_existe/stage/2/select",
        json={"slug": "nao_existe"},
    )
    assert resp.status_code == 404, resp.text


# ===========================================================================
# POST /api/campaigns/{campaign}/stage/3/complete — sequential block (old 2)
# ===========================================================================


def test_complete_stage3_respects_sequential_block(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../stage/3/complete`` respects the sequential block: 409 while
    Stage 2 is not completed; success after satisfying the prerequisite
    (Req. 1.4, 1.7).

    Uses a dedicated curated slug so as not to collide with the per-Stage state
    that other tests in the module write to the shared DB."""
    client = _make_client(session_factory)
    slug = "c0026"

    # Without Stage 2 completed, completing Stage 3 is 409 (block).
    blocked = client.post(f"/api/campaigns/{slug}/stage/3/complete")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["motivo"] == "bloqueio_sequencial"

    # Satisfy the gate prerequisite: the campaign's Stage 2 (Structural
    # Modeling) must be completed. Seed that completion by writing the Stage 2
    # StageRun row directly (the source of truth read by the endpoint's
    # sequential gate), without touching the session-state — so the gate is
    # exercised deterministically and in isolation from the module's other tests.
    db = session_factory()
    try:
        db.add(
            StageRun(
                campaign_id=slug,
                stage=2,
                state=StageState.COMPLETED,
                progress=100,
            )
        )
        db.commit()
    finally:
        db.close()

    # Now Stage 3 completes successfully and the return shows 3 completed.
    ok = client.post(f"/api/campaigns/{slug}/stage/3/complete")
    assert ok.status_code == 200, ok.text
    stages = {e["stage"]: e for e in ok.json()["stages"]}
    assert stages[3]["state"] == StageState.COMPLETED.value


# ===========================================================================
# POST /api/campaigns/{campaign}/stage/4/complete — Operationalization (six Stages)
#
# In the SIX-Stage scheme, Operationalization is Stage 4: completion requires
# Stage 3 (DAG Generation) completed (sequential block) AND the two artifacts
# produced (ordered Abilities + EnvironmentParameters, Req. 8.8).
# Since these tests PERSIST session-state (via persist_stage_completion), they
# stay at the end of the file — along with the other tests that write to the
# module's shared DB — so as not to interfere with the session-state tests that
# expect no prior persistence.
# ===========================================================================


def test_post_stage4_complete_requires_stage3(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../stage/4/complete`` is blocked while Stage 3 is not completed
    (sequential block — 409, Req. 3.x, 8.8).

    After a ``reset`` Stage 3 (DAG Generation) goes back to not completed; since
    ``complete_stage4`` reads the persisted state directly (without triggering
    the curated auto-completion), completing Stage 4 is refused with 409 and the
    reason ``bloqueio_sequencial``, persisting nothing."""
    slug = "costaricto"  # curated slug dedicated to this test in the module DB
    client = _make_client(session_factory)
    # Ensure Stage 3 NOT completed (reset clears the campaign's StageRun rows).
    assert client.post(f"/api/campaigns/{slug}/reset").status_code == 200
    resp = client.post(f"/api/campaigns/{slug}/stage/4/complete")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["motivo"] == "bloqueio_sequencial"


def test_post_stage4_complete_completes_after_stage3(
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../stage/4/complete`` completes Stage 4 (Operationalization) when
    Stage 3 is completed and the two artifacts exist (Req. 8.7, 8.8).

    First a DAG is chosen (completes Stage 3 and unblocks 4); then completing
    Stage 4 produces the Abilities + EnvironmentParameters, persists the
    completion and returns the per-Stage state with 4 ``completed`` and 5
    unblocked."""
    slug = "apt41_dust"  # curated slug dedicated to this test in the module DB
    client = _make_client(session_factory)

    # Complete Stage 3 by choosing the curated DAG (backfilled on listing).
    dags = client.get(f"/api/campaigns/{slug}/dags").json()["dags"]
    assert dags, "expected at least 1 backfilled curated DAG"
    dag_id = dags[0]["id"]
    choice = client.post(
        f"/api/campaigns/{slug}/stage/3/choose-dag", json={"dag_id": dag_id}
    )
    assert choice.status_code == 200, choice.text

    resp = client.post(f"/api/campaigns/{slug}/stage/4/complete")
    assert resp.status_code == 200, resp.text
    stages = {e["stage"]: e for e in resp.json()["stages"]}
    assert stages[4]["state"] == StageState.COMPLETED.value
    assert stages[4]["progress"] == 100
    # Stage 5 (Environment creation) becomes available after 4.
    assert stages[5]["blocked"] is False
    assert stages[5]["startable"] is True
