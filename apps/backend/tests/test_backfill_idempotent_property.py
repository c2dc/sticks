"""Property test for the idempotent backfill of curated DAGs (task 5.4).

# Feature: dag-stage-multi-origin, Property 11: Curated-DAG backfill is
# idempotent.

Validates: Requirements 4.5, 5.1

This is a *feature* property test (the ``dag-stage-multi-origin`` spec is a
feature, not a bugfix): the property is expected to **hold** against the current
implementation of
:meth:`~app.services.dag.dag_service.DagService.sync_curated_dags`.

Property (design.md — Property 11)
----------------------------------
*For every* curated campaign with a derivable DAG and *for every* number of runs
``N ≥ 1``, applying ``sync_curated_dags(campaign)`` repeatedly produces the
**same persisted set** of DAGs — exactly one ``Dag`` with
``origin = curadoria_humana`` per campaign, reconciled by the key
``(campaign_id, origin, identifier)``, **without duplication** on subsequent
runs. The first call inserts; the following ones are no-ops.

Concretely, for an arbitrary curated DAG written to a temporary data dir and an
arbitrary ``N`` (``st.integers(1, 6)``):

1. The persisted set after ``N`` calls is **equal** to the set after 1 call:
   same count (always 1), same ``(campaign_id, origin, identifier)`` tuples, no
   duplicates.
2. The ``id`` of the ``Dag`` rows are **stable** across calls (the first
   inserts, the subsequent ones do not touch the row) — a stronger "no-op"
   proof.
3. ``list_dags(campaign)`` returns a **stable** listing across calls.

Strategy / isolation
--------------------
Each example points a :class:`CaseService` at a **temporary** data directory
with the ``<data_dir>/{api,dag}/`` layout (same as the real one), into which we
write a generated ``{slug}_dag.json``, and builds a ``DagService`` with an
**ephemeral** SQLite ``Session`` (engine on a temporary file with the full
schema via ``Base.metadata.create_all``, same as the ``test_dag_models.py``
fixtures). The generated DAGs are acyclic by construction (edges only from an
earlier node to a later one), mirroring how the real files are parsed
(``structural_nodes[*].child_nodes``). Nothing here touches the network nor an
AI client (there is none on this path).
"""

from __future__ import annotations

import json
import os
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
from app.models.domain import Dag as DagModel
from app.models.enums import DagOrigin
from app.services.campaign_service import CURATED_CASES, CaseService
from app.services.dag.dag_service import DagService
from app.services.dag.generator import CurationDagGenerator


# A real curated campaign slug: curation generation only produces a DAG for
# curated slugs (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guard: keep the curated-fixture slug


# ---------------------------------------------------------------------------
# Ephemeral DB fixture (same as test_dag_models.py): a SQLite engine on a
# temporary file with the full schema via Base.metadata.create_all, so the
# DagService persistence runs against a real, disposable database.
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], Path, str]:
    """Create an ephemeral SQLite engine and return ``(factory, db_path, tmp_dir)``.

    The file (not ``:memory:``) is used so ``create_all`` runs against a real
    file engine and the portable ``CHECK`` (``native_enum=False``) on ``origin``
    mirrors production, same as ``test_dag_models.py``.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_backfill_prop_")
    db_path = Path(tmp_dir) / "backfill.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
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
    # The engine is kept alive by the factory (bind); teardown is the caller's
    # responsibility via the returned temporary directory.
    return factory, db_path, tmp_dir


# ---------------------------------------------------------------------------
# Fixture builder — writes a generated {slug}_dag.json into a temporary data dir
# with the <data_dir>/{api,dag}/ layout, same as the real one. No network, no AI.
# ---------------------------------------------------------------------------


def _write_case_fixture(
    data_dir: Path,
    slug: str,
    structural_nodes: list[dict],
) -> None:
    """Write the ability + DAG fixtures for ``slug`` under ``data_dir``.

    An empty (valid) ability list plus the DAG, so the generator reaches the
    valid-DAG branch purely from the structural graph.
    """
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

    dag_payload = {
        "campaign_name": slug,
        "metadata": {"validation": {"is_dag": True}},
        "structural_nodes": structural_nodes,
    }
    (dag_dir / f"{slug}_dag.json").write_text(
        json.dumps(dag_payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Hypothesis strategy — an acyclic DAG expressed via child_nodes (only
# earlier->later, hence acyclic by topological order), same as the strategy in
# test_curation_generator_property.py.
# ---------------------------------------------------------------------------


@st.composite
def _acyclic_dag(draw) -> list[dict]:
    """Draw the ``structural_nodes`` of an acyclic DAG."""
    n = draw(st.integers(min_value=1, max_value=8))
    node_ids = [f"node-{i:02d}" for i in range(n)]

    children: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.booleans()):
                children[node_ids[i]].append(node_ids[j])

    return [
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


def _persisted_tuples(session: Session, campaign: str) -> list[tuple]:
    """Persisted set as ``(id, campaign_id, origin, identifier)`` tuples.

    Ordered by ``id`` for a stable comparison. Reads directly from the ``dags``
    table (ORM) to observe the persisted state without depending on the
    ``list_dags`` projection.
    """
    lines = (
        session.execute(
            select(DagModel).where(DagModel.campaign_id == campaign).order_by(DagModel.id.asc())
        )
        .scalars()
        .all()
    )
    return [
        (line.id, line.campaign_id, line.origin, line.identifier)
        for line in lines
    ]


# ===========================================================================
# Property 11 — idempotent backfill: N calls == 1 call.
# ===========================================================================


# Feature: dag-stage-multi-origin, Property 11: Curated-DAG backfill is
# idempotent
@settings(
    max_examples=100,
    # Each example does disk I/O (ephemeral SQLite + file fixtures), whose cost
    # varies with the environment; the default ``deadline`` (200ms) is therefore
    # a source of *timing* flakiness — not of correctness. The property is still
    # exercised on every example; we only disable the per-example clock.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(structural_nodes=_acyclic_dag(), n=st.integers(min_value=1, max_value=6))
def test_property11_backfill_is_idempotent(
    structural_nodes: list[dict], n: int
) -> None:
    """``sync_curated_dags`` N times == 1 time: same set, no duplication.

    For an arbitrary curated DAG and arbitrary ``N`` (1..6), the persisted set
    after ``N`` calls is identical to the set after the 1st call: exactly 1
    ``Dag`` ``curadoria_humana`` per campaign, same ``(campaign_id, origin,
    identifier)`` tuples, same ``id`` (1st inserts, the rest are no-ops), and
    ``list_dags`` stable.

    Validates: Requirements 4.5, 5.1
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_backfill_data_"))
    factory, db_path, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)

        case_service = CaseService(data_dir=data_dir)
        service = DagService(
            session=session,
            case_service=case_service,
            generator=CurationDagGenerator(case_service),
        )

        # 1st call: inserts exactly one Dag curadoria_humana (Req. 4.5, 5.1).
        listagem_1 = service.sync_curated_dags(_CURATED_SLUG)
        tuples_1 = _persisted_tuples(session, _CURATED_SLUG)

        # Exactly one curadoria_humana DAG after the 1st call.
        assert len(tuples_1) == 1
        _, campaign_id_1, origin_1, identifier_1 = tuples_1[0]
        assert origin_1 == DagOrigin.HUMAN_CURATION
        assert campaign_id_1 == _CURATED_SLUG
        assert identifier_1 == _CURATED_SLUG  # curation uses identifier=slug

        # Calls 2..N are no-ops reconciled by (campaign_id, origin, identifier).
        listagem_n = listagem_1
        for _ in range(n - 1):
            listagem_n = service.sync_curated_dags(_CURATED_SLUG)

        tuplas_n = _persisted_tuples(session, _CURATED_SLUG)

        # Persisted set after N calls == after 1 call: same count, same tuples,
        # SAME ids (proof of no-op, not re-insertion). No duplication.
        assert tuplas_n == tuples_1
        assert len(tuplas_n) == 1

        # The (campaign_id, origin, identifier) key stays unique — no duplicates.
        chaves = [(c, o, ident) for (_id, c, o, ident) in tuplas_n]
        assert len(chaves) == len(set(chaves))

        # list_dags stable between the 1st and the N-th call.
        resumos_1 = [
            (r.id, r.campaign_id, r.origin, r.identifier) for r in listagem_1
        ]
        resumos_n = [
            (r.id, r.campaign_id, r.origin, r.identifier) for r in listagem_n
        ]
        assert resumos_n == resumos_1
        # The listing matches the persisted state observed via ORM.
        assert resumos_n == [
            (_id, c, o, ident) for (_id, c, o, ident) in tuplas_n
        ]
    finally:
        session.close()
        factory.kw["bind"].dispose()
        # Clean up the data dir and the temporary database.
        for base in (data_dir, Path(db_tmp_dir)):
            for root, _dirs, files in os.walk(base, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                os.rmdir(root)
