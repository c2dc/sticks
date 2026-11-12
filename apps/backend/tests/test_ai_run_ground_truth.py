"""Integration tests for the Ground_Truth_Comparison endpoint (task 9.9).

Exercises ``GET /api/campaigns/{campaign}/ai/runs/{id}/ground-truth`` end-to-end
over the real FastAPI app with a :class:`~fastapi.testclient.TestClient`,
following the harness the sibling AI tests use (``test_ai_run_endpoints.py`` /
``test_ai_run_trace_read_and_fail.py``): a fresh temp-file SQLite engine + ORM
schema per module, the ``get_db`` dependency overridden onto that ephemeral
session. No real SEPINF / Postgres / pgvector is involved — the run's AI stage
outputs are seeded directly as ``agent_runs`` / ``agent_iterations`` /
``agent_stage_outputs`` rows.

Curated-ness is decided SERVER-SIDE by the endpoint from the curated set
(the ``campaigns.curated`` flag), so these two examples cover the curated-only
behaviour the requirement asks for:

* **Curated campaign (Req. 5.3).** For a curated campaign slug (``shadowray``)
  with a completed run carrying AI attack flow + operationalization stage outputs, the
  endpoint returns 200 with the per-stage AI-output-vs-human-curation comparison
  (``stage_3_flow`` technique ids and ``stage_4_operationalization`` ability ids,
  each with ``matched`` / ``missing`` / ``extra``). The curation baseline is read
  on demand from the database rows, so the technique/ability ids the
  run produced that also appear in the curation show up as ``matched``.
* **Non-curated campaign (Req. 5.2).** For a campaign slug that is not one of the
  eight Curated_Campaigns, the endpoint does NOT offer the comparison: it returns
  a 404 with ``reason="not_a_curated_campaign"``.

_Requirements: 5.2, 5.3_
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import (
    AgentIteration,
    AgentRun,
    AgentStageOutput,
)
from app.models.enums import AgentRunStatus, AgentStage, TranslationSource
from tests._curated_db import CURATED_NAMES, seed_case_rows

# A curated slug (``campaigns.curated`` row seeded below), and a slug that is deliberately NOT in the curated set.
_CURATED_CAMPAIGN = "shadowray"
_NON_CURATED_CAMPAIGN = "definitely-not-a-curated-campaign"

# A couple of ids seeded for the curated shadowray campaign, so the AI output
# we seed overlaps the curation baseline and yields non-empty ``matched`` sets.
_CURATED_TECHNIQUE_ID = "T1190"
_CURATED_ABILITY_ID = "0fa06c9c-fd66-52f1-b94a-83cb37bee900"
# An id the AI "produced" that the curation does not carry -> ``extra``.
_EXTRA_TECHNIQUE_ID = "T9999"


def _utcnow() -> dt.datetime:
    """Naive-UTC timestamp matching the AI bookkeeping convention."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Ephemeral session factory (fresh temp-file SQLite + ORM schema per module).
# Mirrors test_ai_run_trace_read_and_fail.py so the AI bookkeeping tables
# round-trip without a migration and no Campaign row is required under SQLite.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module (removed on exit)."""
    tmp_dir = tempfile.mkdtemp(prefix="ai_run_ground_truth_")
    db_path = Path(tmp_dir) / "ai_run_ground_truth.db"
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
    # The ground-truth baseline comes from the DB (the ``attack_flows`` +
    # ``abilities`` rows), so the curated campaign is persisted as ORM rows.
    seed_session = factory()
    try:
        seed_case_rows(
            seed_session,
            _CURATED_CAMPAIGN,
            abilities=[
                {
                    "ability_id": _CURATED_ABILITY_ID,
                    "name": "T1190 - exploit",
                    "technique_id": _CURATED_TECHNIQUE_ID,
                    "executors": [],
                }
            ],
            adversary={
                "id": f"{_CURATED_CAMPAIGN}-adv",
                "name": "adversary",
                "atomic_ordering": [_CURATED_ABILITY_ID],
            },
            flow={
                "campaign_name": CURATED_NAMES[_CURATED_CAMPAIGN],
                "structural_nodes": [
                    {"node_id": "n0", "technique_id": _CURATED_TECHNIQUE_ID},
                    {"node_id": "n1", "technique_id": "T1059"},
                ],
                "edges": [],
            },
        )
    finally:
        seed_session.close()
    try:
        yield factory
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


@pytest.fixture()
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A per-test session for directly seeding bookkeeping rows."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _make_client(session_factory: sessionmaker[Session]) -> TestClient:
    """Build a TestClient with the DB dependency overridden onto the temp SQLite."""
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


def _seed_completed_run_with_ai_outputs(session: Session, campaign_id: str) -> AgentRun:
    """Seed a COMPLETED run with one iteration carrying AI attack flow + op stage outputs.

    * The attack flow stage output carries a serialized ``ProducedFlow`` whose
      ``content.structural_nodes`` include one ``technique_id`` that the
      shadowray curation also carries (-> ``matched``) and one it does not
      (-> ``extra``).
    * The operationalization stage output carries a serialized
      ``ProducedOperationalization`` whose ``abilities.abilities`` include one
      ``ability_id`` the curation also carries (-> ``matched``).
    """
    now = _utcnow()
    run = AgentRun(
        campaign_id=campaign_id,
        status=AgentRunStatus.COMPLETED,
        max_iterations=3,
        iterations_used=1,
        thread_id=f"thread-ground-truth-{campaign_id}",
        final_verdict_success=True,
        error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    session.flush()

    iteration = AgentIteration(
        run_id=run.id,
        index=0,
        refined_stage=None,
        notes="first pass",
        created_at=now,
    )
    session.add(iteration)
    session.flush()

    # Attack flow stage output — content mirrors the curation format; one technique id
    # overlaps the curation (matched) and one does not (extra).
    session.add(
        AgentStageOutput(
            iteration_id=iteration.id,
            stage=AgentStage.FLOW,
            origin=TranslationSource.AI_GENERATED,
            payload={
                "content": {
                    "structural_nodes": [
                        {"technique_id": _CURATED_TECHNIQUE_ID},
                        {"technique_id": _EXTRA_TECHNIQUE_ID},
                    ],
                    "arestas": [],
                }
            },
            created_at=now,
        )
    )

    # Operationalization stage output — abilities carry one ability id that the
    # curation also carries (matched).
    session.add(
        AgentStageOutput(
            iteration_id=iteration.id,
            stage=AgentStage.OPERATIONALIZATION,
            origin=TranslationSource.AI_GENERATED,
            payload={
                "abilities": {
                    "abilities": [
                        {"ability_id": _CURATED_ABILITY_ID},
                    ]
                }
            },
            created_at=now + dt.timedelta(seconds=1),
        )
    )

    session.flush()
    session.commit()
    return run


def test_ground_truth_comparison_for_curated_campaign(
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    """A curated campaign with a completed run returns the per-stage comparison.

    The endpoint decides curated-ness server-side, reads the curation baseline
    from the database on demand, and returns 200 with a per-stage AI-output-vs-curation
    comparison (technique ids for the attack flow stage, ability ids for the
    operationalization stage) carrying matched/missing/extra (Req. 5.3).
    """
    # Sanity: the slug under test really is one of the curated campaigns, so the
    # endpoint's server-side curated check will admit it.
    assert _CURATED_CAMPAIGN in CURATED_NAMES

    run = _seed_completed_run_with_ai_outputs(db_session, _CURATED_CAMPAIGN)

    client = _make_client(session_factory)
    resp = client.get(
        f"/api/campaigns/{_CURATED_CAMPAIGN}/ai/runs/{run.id}/ground-truth"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["campaign_id"] == _CURATED_CAMPAIGN
    assert body["run_id"] == run.id

    # Two per-stage comparisons: the attack flow stage and the operationalization stage.
    stages = {s["stage"]: s for s in body["stages"]}
    assert set(stages) == {
        AgentStage.FLOW.value,
        AgentStage.OPERATIONALIZATION.value,
    }

    flow = stages[AgentStage.FLOW.value]
    # The AI produced both ids; the curated set is read from the database on demand.
    assert set(flow["ai_items"]) == {_CURATED_TECHNIQUE_ID, _EXTRA_TECHNIQUE_ID}
    assert _CURATED_TECHNIQUE_ID in flow["curated_items"]
    # The overlapping id is matched, the AI-only id is extra, and a curated id
    # the AI did not produce shows up as missing.
    assert _CURATED_TECHNIQUE_ID in flow["matched"]
    assert flow["extra"] == [_EXTRA_TECHNIQUE_ID]
    assert _EXTRA_TECHNIQUE_ID not in flow["matched"]
    assert _CURATED_TECHNIQUE_ID not in flow["missing"]
    assert len(flow["missing"]) > 0  # the curation carries more than the one match.

    op = stages[AgentStage.OPERATIONALIZATION.value]
    assert op["ai_items"] == [_CURATED_ABILITY_ID]
    assert _CURATED_ABILITY_ID in op["curated_items"]
    assert op["matched"] == [_CURATED_ABILITY_ID]
    assert op["extra"] == []


def test_ground_truth_comparison_rejects_non_curated_campaign(
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    """A non-curated campaign gets the "not a curated campaign" error (Req. 5.2).

    The comparison is offered ONLY on the eight Curated_Campaigns; for any other
    campaign the endpoint returns a 404 whose detail reason is
    ``not_a_curated_campaign`` instead of computing a comparison.
    """
    # Sanity: the slug under test is deliberately not one of the curated set.
    assert _NON_CURATED_CAMPAIGN not in CURATED_NAMES

    run = _seed_completed_run_with_ai_outputs(db_session, _NON_CURATED_CAMPAIGN)

    client = _make_client(session_factory)
    resp = client.get(
        f"/api/campaigns/{_NON_CURATED_CAMPAIGN}/ai/runs/{run.id}/ground-truth"
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "not_a_curated_campaign"
    assert detail["campaign"] == _NON_CURATED_CAMPAIGN
