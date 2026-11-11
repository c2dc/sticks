"""Example-based regression guard for the curated flow after Phase 2 (task 6.2).

Phase 2 migrated the curated path off disk and onto the database: the DAG, the
Abilities and the Adversary are now resolved from the ``dags`` / ``abilities`` /
``adversaries`` tables, the ``_known_campaign_or_404`` guard validates against
the persisted ``stix_campaigns`` rows, and Stage 2 extracts identity/techniques
from the persisted STIX via ``StixPackageReadService.campaign_overview`` — no
request-time disk read anywhere. This single focused example confirms that a
real curated campaign (``shadowray``) stays **selectable and executable
end-to-end with no regression** (Req. 9.3).

What the one concrete case drives through the ``TestClient`` (doubled agents /
runners where an external boundary would otherwise be touched):

1. **Selectable in the listing / guard** — ``GET /api/campaigns`` lists the
   curated campaign with its per-stage state, and the ``_known_campaign_or_404``
   guard accepts the curated slug (proven transitively: every guarded stage
   endpoint below returns 200, never 404, for the slug).
2. **Stage 2 selection completes** — ``POST /api/campaigns/{slug}/stage/2/select``
   returns ``completed=True`` with a found identity for the curated campaign.
3. **DAG resolves** — ``GET /api/campaigns/{slug}/dags`` lists the persisted
   curated DAG and ``GET /api/campaigns/{slug}/stage/3`` resolves a non-empty,
   advanceable curated ``DagView`` from the database.
4. **Emulation preview works** — ``POST /api/campaigns/{slug}/stage/5/emulation/preview``
   returns the concrete commands + targets for the curated Abilities without
   executing anything (a stubbed isolation verifier and a mocked Caldera
   transport stand in for the external boundaries so no real Docker / network is
   touched).

Seeding mirrors production (and the sibling DB-sourcing tests):

* ``CuratedIngestService.ingest_all()`` persists the curated
  ``campaigns``/``dags``/``abilities``/``adversaries`` rows from disk one-shot
  (sub-step 2.1) — the source the DAG/emulation reads resolve from.
* ``ensure_stix_campaigns`` (shared ``_stix_seed`` helper) persists the matching
  ``stix_campaigns`` row for the curated slug so the DB-backed guard accepts it,
  and one ``stix_campaign_techniques`` row so Stage 2 selection carries a
  technique — matching the production invariant that all 52 STIX campaigns are
  persisted.

Everything is in-memory / ephemeral: a temp-file SQLite session wired into the
app via the ``get_db`` override; Caldera is a real ``CalderaClient`` over an
``httpx.MockTransport``; isolation is a fake verifier. No real network, no real
Docker daemon, no request-time disk read.

_Requirements: 9.3_
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from _stix_seed import ensure_stix_campaigns

from app.api.emulation import get_caldera_client, get_runner_factory
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import StixCampaign, StixCampaignTechnique
from app.services.audit import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    ADVERSARIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.campaign_service import CURATED_CASES
from app.services.containment.isolation import (
    ContainerIsolationVerdict,
    IsolationResult,
)
from app.services.curated_ingest_service import CuratedIngestService
from app.services.operation.runner import OperationRunner
from app.services.stix.stix_service import CURATED_VERSION


# ---------------------------------------------------------------------------
# A real curated campaign: its disk material (DAG + ability + adversary) is
# ingested into the DB, so the DB-sourced DAG/emulation reads resolve it. The
# slug must be curated for the DB-first DAG path and the curated ingest to apply.
# ---------------------------------------------------------------------------

_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES, "the regression slug must be a curated case"

# One technique seeded for the curated STIX campaign so Stage 2 selection carries
# a concrete technique (a richer end-to-end check than an empty extraction).
_SEEDED_TECHNIQUE_ID = "T1190"
_SEEDED_TECHNIQUE_NAME = "Exploit Public-Facing Application"
_SEEDED_TACTIC = "initial-access"


# ---------------------------------------------------------------------------
# Doubled external boundaries: a healthy Caldera transport and an isolation
# verifier that always passes. The preview endpoint does not reach Caldera, but
# the runner factory is wired the same way the sibling emulation suite wires it
# so the doubles stand in for any boundary the flow could otherwise touch.
# ---------------------------------------------------------------------------


def _healthy_router(request: httpx.Request) -> httpx.Response:
    """A ``MockTransport`` handler that serves a healthy Caldera chain."""
    method = request.method.upper()
    path = request.url.path
    if method == "GET" and path == HEALTH_PATH:
        return httpx.Response(200, json={"status": "ok"})
    if method == "POST" and path in (ABILITIES_PATH, ADVERSARIES_PATH):
        import json

        return httpx.Response(200, json=json.loads(request.content.decode("utf-8")))
    if method == "POST" and path == OPERATIONS_PATH:
        return httpx.Response(
            200, json={"id": "op-test", "name": "run", "state": "running"}
        )
    if method == "GET" and path == f"{OPERATIONS_PATH}/op-test":
        return httpx.Response(200, json={"id": "op-test", "state": "finished", "chain": []})
    raise AssertionError(f"unexpected request: {method} {path}")


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """Fake isolation verifier: every requested container is isolated."""
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Ephemeral module-scoped SQLite + seeded app fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_curated_regression_")
    db_path = Path(tmp_dir) / "curated_regression.db"
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


@pytest.fixture(scope="module")
def seeded(session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """Seed the curated campaign exactly as production does, then return the factory.

    Two complementary seeds, both required for the end-to-end curated flow:

    * ``CuratedIngestService.ingest_all()`` persists the curated
      ``campaigns``/``dags``/``abilities``/``adversaries`` rows (sub-step 2.1) —
      the source the DAG listing/view and the emulation preview resolve from.
    * ``ensure_stix_campaigns`` + one ``stix_campaign_techniques`` row persist the
      matching STIX campaign so the DB-backed guard accepts the slug and Stage 2
      selection reads a found identity with a technique from the database.
    """
    # Curated disk -> DB one-shot ingest (DAG + abilities + adversary).
    ingest_session = session_factory()
    try:
        CuratedIngestService(ingest_session).ingest_all()
    finally:
        ingest_session.close()

    # Persisted STIX campaign (guard) + one technique (Stage 2 selection).
    ensure_stix_campaigns(session_factory, [_CURATED_SLUG], version=CURATED_VERSION)
    tech_session = session_factory()
    try:
        campaign_id = tech_session.execute(
            select(StixCampaign.id).where(
                StixCampaign.curated_slug == _CURATED_SLUG
            )
        ).scalar_one()
        tech_session.add(
            StixCampaignTechnique(
                campaign_id=campaign_id,
                technique_id=_SEEDED_TECHNIQUE_ID,
                technique_name=_SEEDED_TECHNIQUE_NAME,
                tactic=_SEEDED_TACTIC,
            )
        )
        tech_session.commit()
    finally:
        tech_session.close()

    return session_factory


def _make_client(session_factory: sessionmaker[Session]) -> TestClient:
    """Build a ``TestClient`` with DB, Caldera and isolation doubles wired in."""
    app = create_app()
    transport = httpx.MockTransport(_healthy_router)

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


# ---------------------------------------------------------------------------
# The single regression case: the curated flow stays selectable + executable.
# ---------------------------------------------------------------------------


def test_curated_flow_stays_selectable_and_executable(
    seeded: sessionmaker[Session],
) -> None:
    """A curated campaign remains selectable and executable end-to-end (Req. 9.3).

    One concrete pass over the curated path through the ``TestClient``, proving no
    Phase-2 regression:

    * the curated campaign is listed with its per-stage state and the guard
      accepts its slug on every stage endpoint (never a 404);
    * Stage 2 selection completes with a found identity and the seeded technique;
    * the persisted curated DAG is listed and resolves as a non-empty, advanceable
      ``DagView`` from the database;
    * the emulation preview returns concrete commands + targets for the curated
      Abilities without executing anything.
    """
    client = _make_client(seeded)

    # (1) Selectable in the listing: the curated campaign is present with state.
    listing = client.get("/api/campaigns")
    assert listing.status_code == 200, listing.text
    listed_ids = {item["id"] for item in listing.json()["campaigns"]}
    assert _CURATED_SLUG in listed_ids, "the curated campaign must be listed"

    # (2) Stage 2 selection completes with a found identity + the seeded technique.
    select_resp = client.post(
        f"/api/campaigns/{_CURATED_SLUG}/stage/2/select",
        json={"slug": _CURATED_SLUG, "version": CURATED_VERSION},
    )
    assert select_resp.status_code == 200, select_resp.text
    selection = select_resp.json()
    assert selection["completed"] is True, "curated Stage 2 selection must complete"
    assert selection["error_message"] is None
    assert selection["identity"]["found"] is True
    assert selection["identity"]["name"] == CURATED_CASES[_CURATED_SLUG]
    technique_ids = {t["technique_id"] for t in selection["techniques"]}
    assert _SEEDED_TECHNIQUE_ID in technique_ids

    # (3a) The persisted curated DAG is listed (guard accepted the slug: 200).
    dags_resp = client.get(f"/api/campaigns/{_CURATED_SLUG}/dags")
    assert dags_resp.status_code == 200, dags_resp.text
    dags_body = dags_resp.json()
    assert dags_body["campaign_id"] == _CURATED_SLUG
    assert dags_body["dags"], "the curated campaign must expose a persisted DAG"

    # (3b) The DAG resolves as a non-empty, advanceable curated view from the DB.
    dag_resp = client.get(f"/api/campaigns/{_CURATED_SLUG}/stage/3")
    assert dag_resp.status_code == 200, dag_resp.text
    dag_view = dag_resp.json()
    assert dag_view["curated"] is True
    assert dag_view["can_advance"] is True
    assert dag_view["nodes"], "the resolved curated DAG must carry nodes"

    # (4) The emulation preview yields concrete commands + targets, no execution.
    preview_resp = client.post(
        f"/api/campaigns/{_CURATED_SLUG}/stage/5/emulation/preview"
    )
    assert preview_resp.status_code == 200, preview_resp.text
    preview = preview_resp.json()
    assert preview["campaign_id"] == _CURATED_SLUG
    assert preview["abilities"], "the curated Abilities must surface in the preview"
    total_commands = 0
    for ability in preview["abilities"]:
        for command in ability["commands"]:
            total_commands += 1
            assert command["command"].strip(), "each command must carry concrete text"
            assert command["targets"], "each command must present a target"
    assert total_commands > 0, "the preview must list at least one concrete command"
