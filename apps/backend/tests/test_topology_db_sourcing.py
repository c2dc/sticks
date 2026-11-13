"""Example-based test: the topology endpoint reads the attack flow and name from the DB.

The ``GET /api/campaigns/{campaign}/topology`` endpoint resolves the flow plus
Abilities and the display name from the ``campaigns`` row via
``_resolve_campaign_name_from_db`` (Req. 3.5).

The curated slug ``shadowray`` is seeded entirely in the DB, but with a
**distinct display name** (``"DB-sourced ShadowRay"``) that differs from the
usual curated name (``"ShadowRay"``). The endpoint returning the
distinct DB name proves the name came from the ``campaigns`` row, not from
the usual curated name.

Everything is ephemeral: a temp-file SQLite session seeded directly with ORM rows
(no disk fixtures, no real network).

_Requirements: 3.5_
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
from app.models.domain import Ability, AttackFlowRow, Case
from app.models.enums import FlowOrigin, TranslationSource
from tests._curated_db import CURATED_NAMES as CURATED_CASES

# A curated slug used for topology: the DB-first flow resolution
# only engages for a curated slug. The seeded display
# name is deliberately different from the usual curated name so the
# endpoint returning it proves the name came from the DB.
_TOPOLOGY_SLUG = "shadowray"
assert _TOPOLOGY_SLUG in CURATED_CASES, "the topology slug must be curated"
_TOPOLOGY_DB_NAME = "DB-sourced ShadowRay"
assert CURATED_CASES[_TOPOLOGY_SLUG] != _TOPOLOGY_DB_NAME, (
    "the DB name must differ from the usual curated name to prove sourcing"
)


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _seed_topology_campaign(session: Session) -> None:
    """Seed the curated topology slug entirely in the DB (campaigns + ability + attack flow).

    The ``campaigns`` row carries the distinct ``_TOPOLOGY_DB_NAME`` and the
    ``attack_flows`` row a single-node graph so a
    non-empty attack flow resolves from the DB (``_resolve_campaign_name_from_db`` requires at
    least one Ability row and a persisted attack flow).
    """
    session.add(
        Case(
            id=_TOPOLOGY_SLUG,
            name=_TOPOLOGY_DB_NAME,
            translation_source=TranslationSource.HUMAN_CURATION,
            curated=True,
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
            executors=[{"name": "sh", "platform": "linux", "command": "echo topo"}],
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        AttackFlowRow(
            campaign_id=_TOPOLOGY_SLUG,
            origin=FlowOrigin.HUMAN_CURATION,
            identifier="human-curation-v1",
            label="DB-sourced attack flow",
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
    """Seed the topology campaign once for the module and return the factory."""
    session = session_factory()
    try:
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


# ---------------------------------------------------------------------------
# Topology endpoint resolves the attack flow + name from the DB (Req. 3.5)
# ---------------------------------------------------------------------------


def test_topology_resolves_name_from_db_not_case_service(
    seeded: sessionmaker[Session],
) -> None:
    """The topology endpoint resolves the campaign name from the DB (Req. 3.5).

    The curated slug ``shadowray`` is seeded in the DB with a display name that
    deliberately differs from the usual curated name. The endpoint
    returning the DB name — and requiring a persisted attack flow + Ability rows to do so
    — proves ``_resolve_campaign_name_from_db`` sourced the resolution from the
    database rather than from the usual curated name.
    """
    client = _make_client(seeded)
    resp = client.get(f"/api/campaigns/{_TOPOLOGY_SLUG}/topology")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["campaign_id"] == _TOPOLOGY_SLUG
    # The DB-sourced name (not the usual curated name "ShadowRay").
    assert body["name"] == _TOPOLOGY_DB_NAME
    assert body["name"] != CURATED_CASES[_TOPOLOGY_SLUG]
    # The contained topology itself is still rendered (presentation unchanged).
    assert body["isolated"] is True
    assert body["hosts"], "the static lab hosts must still be present"
