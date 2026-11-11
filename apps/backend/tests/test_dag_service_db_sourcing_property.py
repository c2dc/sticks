"""Property + example tests for DB-first DAG sourcing (task 1.2, Phase 2.2).

This file guards the **critical regression** of sub-step 1.1, which made
``DagService.get_dag`` database-first with a disk fallback as the secondary
path. The central guarantee is Property 5 of the design: for a curated case,
the content served **from the database** after the curated ingest is equivalent
to the content derived **from the disk files** for that same case — same DAG
(``structural_nodes`` + ``edges``), same abilities (executors) and same
adversary (``atomic_ordering``).

Everything here is offline and uses an ephemeral SQLite session plus a temporary
data directory laid out exactly like ``<data_dir>/{api,dag}/`` so
``CaseService(data_dir=...)`` resolves the generated fixtures. No network and no
AI client (there is none on this path).

Design's Correctness Property covered:

* **Property 5** (task 1.2) — curated content served from the database is
  equivalent to the one derived from disk. The property seeds the database via
  :class:`CuratedIngestService` from generated disk fixtures, then compares the
  DB-sourced DAG against the disk-sourced DAG and the persisted abilities /
  adversary against their disk-parsed counterparts.

Concrete examples in the same file (no separate example file):

* DB sourcing when the campaign IS persisted (Req. 3.1) — a seeded DB makes
  ``get_dag`` resolve from the ``dags`` table.
* Disk fallback when the campaign is ABSENT from the DB (Req. 3.2) — with no
  persisted row, ``get_dag`` falls through to the disk read.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Ability as AbilityModel, Adversary as AdversaryModel
from app.services.campaign_service import CURATED_CASES, CaseService
from app.services.curated_ingest_service import CuratedIngestService
from app.services.dag.dag_service import DagService


# A real curated slug: ``get_dag`` only resolves the DAG view for curated slugs,
# so the fixtures reuse a slug that is registered as curated.
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guard: keep the fixture slug curated


# ---------------------------------------------------------------------------
# Ephemeral SQLite session (same pattern as test_curated_ingest_service.py).
# ---------------------------------------------------------------------------


def _new_session() -> tuple[Session, Path, object]:
    """Build an isolated SQLite session; return (session, tmp_dir, engine)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="dag_db_sourcing_"))
    db_path = tmp_dir / "sourcing.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")
    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)
    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), tmp_dir, engine


@pytest.fixture()
def session() -> Iterator[Session]:
    db_session, tmp_dir, engine = _new_session()
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fixture builders — write a full curated case (ability + adversary + DAG) for
# ``slug`` under a tmp data dir, so both CaseService (disk) and the ingest can
# consume the exact same source material.
# ---------------------------------------------------------------------------


def _write_full_case_fixture(
    data_dir: Path,
    slug: str,
    *,
    structural_nodes: list[dict],
    abilities: list[dict],
    atomic_ordering: list[str],
) -> None:
    """Write the ability + adversary + DAG fixtures for ``slug`` under ``data_dir``.

    The layout mirrors the real one: ``<data_dir>/api/{slug}_dag-ability.json``,
    ``<data_dir>/api/{slug}_dag-adversary.json`` and
    ``<data_dir>/dag/{slug}_dag.json``.
    """
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    (api_dir / f"{slug}_dag-ability.json").write_text(
        json.dumps(abilities), encoding="utf-8"
    )
    (api_dir / f"{slug}_dag-adversary.json").write_text(
        json.dumps(
            {
                "id": f"adv-{slug}",
                "name": f"Adversary {slug}",
                "description": "generated",
                "atomic_ordering": atomic_ordering,
            }
        ),
        encoding="utf-8",
    )
    dag_payload = {
        "campaign_name": slug,
        "metadata": {"validation": {"is_dag": True}},
        "structural_nodes": structural_nodes,
    }
    (dag_dir / f"{slug}_dag.json").write_text(
        json.dumps(dag_payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Hypothesis strategy — a curated case: an acyclic DAG (child_nodes with
# earlier->later edges only), abilities whose ids compose the atomic_ordering,
# and the matching adversary ordering.
# ---------------------------------------------------------------------------


@st.composite
def _curated_case(draw) -> dict:
    """Draw a curated case as ``{structural_nodes, abilities, atomic_ordering}``.

    Nodes get unique ids; edges go only from an earlier node to a later node
    (``i -> j`` with ``i < j``), which guarantees acyclicity and makes the
    expected edge set exact. Each node contributes one ability whose
    ``ability_id`` composes the adversary ``atomic_ordering`` in node order.
    """
    n = draw(st.integers(min_value=1, max_value=6))
    node_ids = [f"node-{i:02d}" for i in range(n)]

    children: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.booleans()):
                children[node_ids[i]].append(node_ids[j])

    structural_nodes = [
        {
            "node_id": node_ids[i],
            "node_index": i,
            "technique_id": f"T{1000 + i}",
            "technique_name": f"Technique {i}",
            "tactic": f"tactic-{i}",
            "child_nodes": children[node_ids[i]],
            "parent_nodes": [],
        }
        for i in range(n)
    ]

    ability_ids = [f"ability-{i:02d}" for i in range(n)]
    abilities = [
        {
            "ability_id": ability_ids[i],
            "name": f"T{1000 + i} - Technique {i}",
            "tactic": f"tactic-{i}",
            "technique_name": f"Technique {i}",
            "technique_id": f"T{1000 + i}",
            "description": f"ability {i}",
            "executors": [
                {
                    "name": "sh",
                    "platform": "linux",
                    "command": f"echo step {i}",
                }
            ],
        }
        for i in range(n)
    ]

    return {
        "structural_nodes": structural_nodes,
        "abilities": abilities,
        "atomic_ordering": list(ability_ids),
    }


def _dag_tuples(view) -> tuple[list[tuple], set[tuple[str, str]]]:
    """Normalize a :class:`DagView` into comparable (nodes, edges) structures."""
    nodes = [
        (n.node_id, n.technique_id, n.technique_name, n.tactic) for n in view.nodes
    ]
    edges = {(e.source, e.target) for e in view.edges}
    return nodes, edges


# ===========================================================================
# Property 5 (task 1.2) — DB-served curated content equals the disk one.
# ===========================================================================


# Feature: stix-db-single-source-ai-pipeline, Property 5: Conteúdo curado servido do banco é equivalente ao de disco
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=_curated_case())
def test_property5_db_served_curated_content_equals_disk(case: dict) -> None:
    """DB-sourced DAG / abilities / adversary equal the disk-derived ones.

    Validates: Requirements 1.4, 3.6
    """
    db_session, tmp_dir, engine = _new_session()
    try:
        data_dir = tmp_dir / "data"
        _write_full_case_fixture(
            data_dir,
            _CURATED_SLUG,
            structural_nodes=case["structural_nodes"],
            abilities=case["abilities"],
            atomic_ordering=case["atomic_ordering"],
        )
        case_service = CaseService(data_dir=data_dir)

        # Disk-sourced reference, BEFORE seeding the DB: the one-shot ingest path
        # still parses the curated files from disk (``CaseService.load_case``),
        # and the pure ``_build_dag_view`` assembly turns the parsed DAG into the
        # reference view. The runtime ``get_dag`` no longer reads disk (sub-step
        # 2.4), so the reference is built from the parsed ``CaseData`` directly.
        disk_case = case_service.load_case(_CURATED_SLUG)
        assert disk_case.dag is not None
        disk_dag = DagService._build_dag_view(
            _CURATED_SLUG, disk_case.name, disk_case.dag
        )

        # Seed the database from the SAME disk files.
        CuratedIngestService(db_session, case_service=case_service).ingest_all()

        # DB-sourced: a DagService WITH a session resolves the DAG from the
        # ``dags`` table (database-first), never from disk.
        db_dag = DagService(
            session=db_session, case_service=case_service
        ).get_dag(_CURATED_SLUG)

        # --- DAG equivalence (structural_nodes + edges) -------------------
        assert db_dag.curated is True and db_dag.can_advance is True
        assert _dag_tuples(db_dag) == _dag_tuples(disk_dag)

        # --- Abilities equivalence (executors) ---------------------------
        db_abilities = {
            row.ability_id: row.executors
            for row in db_session.execute(
                select(AbilityModel).where(
                    AbilityModel.campaign_id == _CURATED_SLUG
                )
            )
            .scalars()
            .all()
        }
        disk_abilities = {
            ability.ability_id: [e.model_dump() for e in ability.executors]
            for ability in disk_case.abilities
        }
        assert db_abilities == disk_abilities

        # --- Adversary equivalence (atomic_ordering) ---------------------
        db_adversary = (
            db_session.execute(
                select(AdversaryModel).where(
                    AdversaryModel.campaign_id == _CURATED_SLUG
                )
            )
            .scalars()
            .first()
        )
        assert db_adversary is not None
        assert list(db_adversary.atomic_ordering) == disk_case.atomic_ordering
    finally:
        db_session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Concrete example — DB sourcing when the campaign is persisted (Req. 3.1).
# ===========================================================================


def test_example_db_sourcing_when_persisted(session: Session, tmp_path: Path) -> None:
    """A seeded DB makes ``get_dag`` resolve from the ``dags`` table (Req. 3.1).

    Validates: Requirements 3.1
    """
    data_dir = tmp_path / "data"
    structural_nodes = [
        {
            "node_id": "n0",
            "node_index": 0,
            "technique_id": "T1000",
            "technique_name": "Technique 0",
            "tactic": "tactic-0",
            "child_nodes": ["n1"],
            "parent_nodes": [],
        },
        {
            "node_id": "n1",
            "node_index": 1,
            "technique_id": "T1001",
            "technique_name": "Technique 1",
            "tactic": "tactic-1",
            "child_nodes": [],
            "parent_nodes": ["n0"],
        },
    ]
    abilities = [
        {
            "ability_id": "ability-00",
            "technique_id": "T1000",
            "executors": [{"name": "sh", "platform": "linux", "command": "echo 0"}],
        }
    ]
    _write_full_case_fixture(
        data_dir,
        _CURATED_SLUG,
        structural_nodes=structural_nodes,
        abilities=abilities,
        atomic_ordering=["ability-00"],
    )
    case_service = CaseService(data_dir=data_dir)
    CuratedIngestService(session, case_service=case_service).ingest_all()

    # With the DB seeded, a session-backed DagService resolves from the DB even
    # if the disk files were removed: delete them to PROVE the DB is the source.
    shutil.rmtree(data_dir)

    view = DagService(session=session, case_service=case_service).get_dag(
        _CURATED_SLUG
    )

    assert view.curated is True
    assert view.can_advance is True
    assert [n.node_id for n in view.nodes] == ["n0", "n1"]
    assert {(e.source, e.target) for e in view.edges} == {("n0", "n1")}


# ===========================================================================
# Concrete example — DB-only: no disk fallback when the campaign is absent.
# (Sub-step 2.4 removed the request-time disk read — Req. 1.1, 4.1, 4.2.)
# ===========================================================================


def test_example_no_disk_fallback_when_absent_from_db(
    session: Session, tmp_path: Path
) -> None:
    """With no persisted DAG, ``get_dag`` surfaces the typed missing-DAG error.

    After sub-step 2.4 the runtime no longer reads ``data/dag/{slug}_dag.json``:
    even though the curated file exists on disk, a session-backed ``get_dag``
    with an empty database returns ``curated=True``/``can_advance=False`` with a
    missing-DAG error instead of falling back to disk — proving the runtime never
    touches ``packages/cli/data`` (Req. 1.1, 4.1, 4.2).

    Validates: Requirements 1.1, 4.1, 4.2
    """
    data_dir = tmp_path / "data"
    structural_nodes = [
        {
            "node_id": "n0",
            "node_index": 0,
            "technique_id": "T1000",
            "technique_name": "Technique 0",
            "tactic": "tactic-0",
            "child_nodes": ["n1"],
            "parent_nodes": [],
        },
        {
            "node_id": "n1",
            "node_index": 1,
            "technique_id": "T1001",
            "technique_name": "Technique 1",
            "tactic": "tactic-1",
            "child_nodes": [],
            "parent_nodes": ["n0"],
        },
    ]
    _write_full_case_fixture(
        data_dir,
        _CURATED_SLUG,
        structural_nodes=structural_nodes,
        abilities=[],
        atomic_ordering=[],
    )
    case_service = CaseService(data_dir=data_dir)

    # The DB is EMPTY (no ingest): the session-backed DagService finds no
    # persisted DAG and does NOT fall back to disk (sub-step 2.4).
    view = DagService(session=session, case_service=case_service).get_dag(
        _CURATED_SLUG
    )

    assert view.curated is True
    assert view.can_advance is False
    assert view.error is not None
    assert view.nodes == []
    assert view.edges == []
