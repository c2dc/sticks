"""Example-based tests: Stage 4, emulation and topology read from the DB (task 3.3).

Sub-step 2.3b migrated the three remaining read points off the curated
``CaseService`` disk read and onto the database (``Ability``/``Adversary`` rows
and the ``dags`` table keyed by ``campaign_id``):

* **Stage 4 — operationalization** (``StageService.assemble_stage2``): resolves
  the Abilities via ``_load_ability_views(campaign_id, adversary)`` ordered by
  the Adversary's ``atomic_ordering`` and the Adversary via
  ``_load_adversary_row(campaign_id)`` (Req. 3.3).
* **Emulation endpoint** (``POST /api/campaigns/{campaign}/stage/5/emulation/preview``):
  resolves Abilities + Adversary via ``_resolve_case_from_db`` (Req. 3.4).
* **Topology endpoint** (``GET /api/campaigns/{campaign}/topology``): resolves
  the DAG (``dags`` table via ``DagService``) + Abilities and the display name
  from the ``campaigns`` row via ``_resolve_campaign_name_from_db`` (Req. 3.5).

The proof that the DB — and **not** the ``CaseService`` — is the source:

* For Stage 4 and emulation, the seeded campaign (``db-only-campaign``) is a
  **non-curated** slug that is absent from ``CURATED_CASES`` and has no file on
  disk. The ``CaseService`` cannot produce it, so any resolved Ability/Adversary
  can only have come from the database rows this test seeds.
* For topology, the DB-first path in ``DagService.get_dag`` only engages for a
  curated slug, so the case uses the curated slug ``shadowray`` seeded entirely
  in the DB — but with a **distinct display name** (``"DB-sourced ShadowRay"``)
  that differs from the curated ``CaseService`` name (``"ShadowRay"``). The
  endpoint returning the distinct DB name proves the name came from the
  ``campaigns`` row, not from ``CaseService.list_cases()``.

Everything is in-memory/ephemeral: a temp-file SQLite session seeded directly
with ORM rows (no disk fixtures, no real network). The emulation/topology
endpoints run through a ``TestClient`` with ``get_db`` overridden onto the same
session factory.

_Requirements: 3.3, 3.4, 3.5_
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
from app.models.domain import Ability, Adversary, Case, Dag
from app.models.enums import DagOrigin, TranslationSource
from app.services.campaign_service import CURATED_CASES
from app.services.stage.stage_service import (
    Stage1Input,
    Stage1Output,
    Stage1Result,
    StageService,
    TechniqueElement,
)
from app.models.enums import StageState


# ---------------------------------------------------------------------------
# A non-curated campaign used for Stage 4 + emulation. Its absence from the
# curated registry is what makes DB sourcing provable: CaseService has no such
# case, so a resolved Ability/Adversary can only come from the DB rows below.
# ---------------------------------------------------------------------------

_DB_ONLY_SLUG = "db-only-campaign"
assert _DB_ONLY_SLUG not in CURATED_CASES, "the DB-only slug must NOT be curated"

# Two abilities seeded out of execution order on purpose: the Adversary's
# atomic_ordering must drive the resolved order (ab-2 before ab-1).
_ABILITY_1_ID = f"{_DB_ONLY_SLUG}-ab-1"
_ABILITY_2_ID = f"{_DB_ONLY_SLUG}-ab-2"
_ABILITY_1_COMMAND = "curl http://192.168.20.30/first"
_ABILITY_2_COMMAND = "curl http://192.168.20.30/second"
_ADVERSARY_ID = f"{_DB_ONLY_SLUG}-adv"

# A curated slug used for topology: the DB-first DAG resolution in
# ``DagService.get_dag`` only engages for a curated slug. The seeded display
# name is deliberately different from the curated CaseService name so the
# endpoint returning it proves the name came from the DB.
_TOPOLOGY_SLUG = "shadowray"
assert _TOPOLOGY_SLUG in CURATED_CASES, "the topology slug must be curated"
_TOPOLOGY_DB_NAME = "DB-sourced ShadowRay"
assert _TOPOLOGY_DB_NAME != CURATED_CASES[_TOPOLOGY_SLUG], (
    "the DB name must differ from the curated CaseService name to prove sourcing"
)


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _seed_db_only_campaign(session: Session) -> None:
    """Seed a non-curated campaign with Abilities + Adversary rows in the DB.

    Mirrors what ``CuratedIngestService`` persists for a campaign — a
    ``campaigns`` row plus the ``Ability``/``Adversary`` rows keyed by
    ``campaign_id`` — but for a slug the ``CaseService`` does not know about, so
    only the DB can resolve it.
    """
    session.add(
        Case(
            id=_DB_ONLY_SLUG,
            name="DB Only Campaign",
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        Ability(
            ability_id=_ABILITY_1_ID,
            campaign_id=_DB_ONLY_SLUG,
            name="first ability",
            technique_id="T1190",
            technique_name="Exploit Public-Facing Application",
            tactic="initial-access",
            executors=[
                {"name": "sh", "platform": "linux", "command": _ABILITY_1_COMMAND}
            ],
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        Ability(
            ability_id=_ABILITY_2_ID,
            campaign_id=_DB_ONLY_SLUG,
            name="second ability",
            technique_id="T1059",
            technique_name="Command and Scripting Interpreter",
            tactic="execution",
            executors=[
                {"name": "sh", "platform": "linux", "command": _ABILITY_2_COMMAND}
            ],
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        Adversary(
            id=_ADVERSARY_ID,
            campaign_id=_DB_ONLY_SLUG,
            name="DB Only Adversary",
            description="seeded in the DB only",
            # Reversed vs. seed order: ordering must follow atomic_ordering.
            atomic_ordering=[_ABILITY_2_ID, _ABILITY_1_ID],
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.commit()


def _seed_topology_campaign(session: Session) -> None:
    """Seed the curated topology slug entirely in the DB (campaigns + ability + DAG).

    The ``campaigns`` row carries the distinct ``_TOPOLOGY_DB_NAME`` and the
    ``dags`` row a single-node graph so ``DagService.get_dag`` resolves a
    non-empty DAG from the DB (``_resolve_campaign_name_from_db`` requires at
    least one Ability row and a persisted DAG).
    """
    session.add(
        Case(
            id=_TOPOLOGY_SLUG,
            name=_TOPOLOGY_DB_NAME,
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        Ability(
            ability_id=f"{_TOPOLOGY_SLUG}-ab-1",
            campaign_id=_TOPOLOGY_SLUG,
            name="topology ability",
            technique_id="T1190",
            technique_name="Exploit Public-Facing Application",
            tactic="initial-access",
            executors=[
                {"name": "sh", "platform": "linux", "command": "echo topo"}
            ],
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        Dag(
            campaign_id=_TOPOLOGY_SLUG,
            origin=DagOrigin.HUMAN_CURATION,
            identifier="human-curation-v1",
            label="DB-sourced DAG",
            content={
                "structural_nodes": [
                    {
                        "node_id": "n0",
                        "technique_id": "T1190",
                        "technique_name": "Exploit Public-Facing Application",
                        "tactic": "initial-access",
                        "parent_nodes": [],
                        "child_nodes": [],
                    }
                ],
                "edges": [],
            },
            created_at=_utcnow(),
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Ephemeral module-scoped SQLite + app fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_db_sourcing_")
    db_path = Path(tmp_dir) / "db_sourcing.db"
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
    """Seed both campaigns once for the module and return the factory."""
    session = session_factory()
    try:
        _seed_db_only_campaign(session)
        _seed_topology_campaign(session)
    finally:
        session.close()
    return session_factory


def _make_client(session_factory: sessionmaker[Session]) -> TestClient:
    """Build a TestClient with ``get_db`` wired onto the seeded session factory."""
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


def _precomputed_stage1(slug: str) -> Stage1Result:
    """A completed Stage-1 result carrying techniques, to feed ``assemble_stage2``.

    Passing this in avoids ``assemble_stage2`` re-running Stage 1 (which would
    read the DAG from disk via ``CaseService`` and, for a non-curated slug, fail
    to find it). With a non-empty technique list the Stage-2 input is present, so
    the method proceeds to resolve the Abilities/Adversary — which is exactly the
    DB-sourced behaviour under test (Req. 3.3).
    """
    output = Stage1Output(
        techniques=[
            TechniqueElement(
                technique_id="T1190",
                technique_name="Exploit Public-Facing Application",
                tactic="initial-access",
                node_id="n0",
            ),
            TechniqueElement(
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
                node_id="n1",
            ),
        ]
    )
    return Stage1Result(
        campaign_id=slug,
        state=StageState.COMPLETED,
        input=Stage1Input(campaign_id=slug, name=slug),
        output=output,
    )


# ---------------------------------------------------------------------------
# Stage 4 — operationalization resolves Abilities + Adversary from the DB (Req. 3.3)
# ---------------------------------------------------------------------------


def test_stage4_operationalization_reads_abilities_and_adversary_from_db(
    seeded: sessionmaker[Session],
) -> None:
    """``assemble_stage2`` resolves Abilities/Adversary from the DB, not CaseService.

    The campaign is non-curated (absent from ``CURATED_CASES`` and from disk), so
    the Abilities and Adversary in the Stage-2 Output can only have come from the
    seeded DB rows. The ordering follows the Adversary's ``atomic_ordering``
    (ab-2 before ab-1), confirming ``_load_adversary_row`` +
    ``_load_ability_views`` are the source. (Req. 3.3)
    """
    session = seeded()
    try:
        service = StageService(session=session)
        result = service.assemble_stage2(
            _DB_ONLY_SLUG, stage1=_precomputed_stage1(_DB_ONLY_SLUG)
        )
    finally:
        session.close()

    assert result.output is not None, "a non-suppressed Stage-2 output is expected"
    output = result.output

    # Abilities came from the DB rows (non-curated slug ⇒ not from CaseService).
    assert output.has_abilities is True
    resolved_ids = [a.ability_id for a in output.abilities]
    assert resolved_ids == [_ABILITY_2_ID, _ABILITY_1_ID], (
        "ordering must follow the Adversary's atomic_ordering (DB-sourced)"
    )
    # Concrete commands are the ones seeded in the DB abilities.
    commands = {
        a.ability_id: [e.command for e in a.executors] for a in output.abilities
    }
    assert commands[_ABILITY_1_ID] == [_ABILITY_1_COMMAND]
    assert commands[_ABILITY_2_ID] == [_ABILITY_2_COMMAND]

    # Adversary came from the DB row.
    assert output.has_adversary is True
    assert output.adversary is not None
    assert output.adversary.id == _ADVERSARY_ID
    assert output.adversary.atomic_ordering == [_ABILITY_2_ID, _ABILITY_1_ID]
    assert output.ordering == [_ABILITY_2_ID, _ABILITY_1_ID]


# ---------------------------------------------------------------------------
# Emulation endpoint resolves Abilities + Adversary from the DB (Req. 3.4)
# ---------------------------------------------------------------------------


def test_emulation_preview_reads_case_from_db(
    seeded: sessionmaker[Session],
) -> None:
    """The emulation preview resolves the case from the DB, not CaseService (Req. 3.4).

    The preview for the non-curated ``db-only-campaign`` lists exactly the two
    DB-seeded abilities with their concrete commands. Because ``CaseService`` has
    no such case on disk, a non-empty preview proves ``_resolve_case_from_db`` is
    the source.
    """
    client = _make_client(seeded)
    resp = client.post(
        f"/api/campaigns/{_DB_ONLY_SLUG}/stage/5/emulation/preview"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["campaign_id"] == _DB_ONLY_SLUG
    # Both DB abilities surface, ordered by the Adversary's atomic_ordering.
    ability_ids = [a["ability_id"] for a in body["abilities"]]
    assert ability_ids == [_ABILITY_2_ID, _ABILITY_1_ID]

    # The concrete commands are the ones seeded in the DB.
    previewed_commands = {
        a["ability_id"]: [c["command"] for c in a["commands"]]
        for a in body["abilities"]
    }
    assert previewed_commands[_ABILITY_1_ID] == [_ABILITY_1_COMMAND]
    assert previewed_commands[_ABILITY_2_ID] == [_ABILITY_2_COMMAND]


# ---------------------------------------------------------------------------
# Topology endpoint resolves the DAG + name from the DB (Req. 3.5)
# ---------------------------------------------------------------------------


def test_topology_resolves_name_from_db_not_case_service(
    seeded: sessionmaker[Session],
) -> None:
    """The topology endpoint resolves the campaign name from the DB (Req. 3.5).

    The curated slug ``shadowray`` is seeded in the DB with a display name that
    deliberately differs from the curated ``CaseService`` name. The endpoint
    returning the DB name — and requiring a persisted DAG + Ability rows to do so
    — proves ``_resolve_campaign_name_from_db`` sourced the resolution from the
    database rather than from ``CaseService.list_cases()``.
    """
    client = _make_client(seeded)
    resp = client.get(f"/api/campaigns/{_TOPOLOGY_SLUG}/topology")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["campaign_id"] == _TOPOLOGY_SLUG
    # The DB-sourced name (not the curated CaseService name "ShadowRay").
    assert body["name"] == _TOPOLOGY_DB_NAME
    assert body["name"] != CURATED_CASES[_TOPOLOGY_SLUG]
    # The contained topology itself is still rendered (presentation unchanged).
    assert body["isolated"] is True
    assert body["hosts"], "the static lab hosts must still be present"
