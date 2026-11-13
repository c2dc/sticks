"""Property test for the per-type counts persisted by StixPersistenceService
(task 2.8 of the ``stix-package-persistence-and-overview`` spec).

# Feature: stix-package-persistence-and-overview, Property 4: per-type count
# matches the source bundle.

Validates: Requirements 3.2, 3.3

This is a *feature* spec, so the property is expected to HOLD against the
current implementation of
:meth:`~app.services.stix.stix_persistence_service.StixPersistenceService.persist_bundle`.

Property (design.md — Property 4)
---------------------------------
*For every* valid STIX bundle and *for every* counted STIX type, the value
persisted in ``stix_type_counts.count`` equals the number of objects of that
type in the source bundle. The six counted types are the ones
:class:`StixService.dashboard` projects (``ROTULO_TIPO`` order): ``campaign``,
``attack-pattern``, ``x-mitre-tactic``, ``intrusion-set``, ``malware`` and
``relationship`` (a count of ``0`` is persisted when a type is absent). Each
persisted ``StixTypeCount.description`` is also non-empty (Req. 3.3).

Strategy / isolation
--------------------
Each example draws an independent object count per counted type and emits that
many well-formed STIX objects of that type (globally unique ids so the
per-package ``UNIQUE`` constraints on campaigns/actors never collide), plus a
handful of objects of OTHER (uncounted) types to prove they do not perturb the
six counts. The bundle is written to an isolated ``data_dir`` as
``enterprise-attack-{version}.json`` and persisted via
``StixPersistenceService(session, StixService(data_dir=..., http_client=None))``
against an ephemeral file-backed SQLite engine built with
``Base.metadata.create_all`` (same session pattern the sibling backend property
tests use). Nothing here touches the network (``http_client=None`` and
``persist_bundle`` never downloads) nor an AI client.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StixTypeCount
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import ROTULO_TIPO, StixService

# The six counted STIX types, in the exact order StixService.dashboard emits
# them (ROTULO_TIPO insertion order). The persisted per-type count must equal
# the number of objects of each type in the source bundle.
_COUNTED_TYPES: tuple[str, ...] = tuple(ROTULO_TIPO.keys())

# A few uncounted STIX types sprinkled into every bundle: they must never affect
# the six counted values.
_UNCOUNTED_TYPES: tuple[str, ...] = (
    "course-of-action",
    "identity",
    "marking-definition",
    "x-mitre-matrix",
)


# ---------------------------------------------------------------------------
# Ephemeral DB factory (sibling-test pattern): a file-backed SQLite engine with
# the full schema via Base.metadata.create_all, so persistence runs against a
# real, disposable database.
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``.

    A temp file (not ``:memory:``) is used so ``create_all`` runs against a real
    file engine, matching the sibling backend property tests. Teardown (dispose
    + remove) is the caller's responsibility via the returned directory.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stix_type_counts_prop_")
    db_path = Path(tmp_dir) / "stix_type_counts.db"
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
    return factory, tmp_dir


# ---------------------------------------------------------------------------
# Bundle builder — emit exactly ``counts[stix_type]`` objects of each counted
# type with globally unique ids, plus some uncounted-type noise objects.
# ---------------------------------------------------------------------------


def _build_bundle(counts: dict[str, int], noise: int) -> dict:
    """Build a valid STIX bundle with the requested per-type object counts.

    Emits ``counts[stix_type]`` well-formed objects for each counted type (ids
    unique across the whole bundle so the per-package ``UNIQUE`` constraints on
    ``stix_campaigns``/``stix_actors`` never collide) and ``noise`` objects of
    uncounted types. Returns the ``{"type": "bundle", "objects": [...]}`` dict.
    """
    objects: list[dict] = []
    seq = 0

    for stix_type in _COUNTED_TYPES:
        for _ in range(counts[stix_type]):
            obj = {
                "type": stix_type,
                "id": f"{stix_type}--{seq:08d}-0000-0000-0000-000000000000",
                "name": f"{stix_type}-name-{seq}",
            }
            if stix_type == "attack-pattern":
                # A usable external_id + tactic so the technique derivation path
                # has valid data to walk (does not affect the counted total).
                obj["external_references"] = [
                    {"source_name": "mitre-attack", "external_id": f"T{1000 + seq}"}
                ]
                obj["kill_chain_phases"] = [
                    {"kill_chain_name": "mitre-attack", "phase_name": f"tactic-{seq}"}
                ]
            objects.append(obj)
            seq += 1

    for i in range(noise):
        uncounted_type = _UNCOUNTED_TYPES[i % len(_UNCOUNTED_TYPES)]
        objects.append(
            {
                "type": uncounted_type,
                "id": f"{uncounted_type}--{seq:08d}-0000-0000-0000-000000000000",
                "name": f"{uncounted_type}-name-{seq}",
            }
        )
        seq += 1

    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-0000-0000-000000000000",
        "objects": objects,
    }


# ---------------------------------------------------------------------------
# Hypothesis strategy — an independent object count per counted type plus noise.
# ---------------------------------------------------------------------------


@st.composite
def _bundle_plan(draw) -> tuple[dict[str, int], int]:
    """Draw ``(counts, noise)``: a per-counted-type object count and noise size."""
    counts = {
        stix_type: draw(st.integers(min_value=0, max_value=6)) for stix_type in _COUNTED_TYPES
    }
    noise = draw(st.integers(min_value=0, max_value=5))
    return counts, noise


# ===========================================================================
# Property 4 — persisted per-type count equals the bundle's object count.
# ===========================================================================


# Feature: stix-package-persistence-and-overview, Property 4: per-type count
# matches the source bundle.
@settings(
    max_examples=40,
    # Each example does disk I/O (ephemeral SQLite + a JSON bundle on disk),
    # whose cost varies with the environment; the default per-example deadline
    # is therefore a source of timing flakiness, not of correctness.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(plan=_bundle_plan())
def test_property4_type_counts_match_bundle(plan: tuple[dict[str, int], int]) -> None:
    """``stix_type_counts.count`` equals the number of bundle objects per type.

    For an arbitrary valid bundle, after ``persist_bundle(version)`` each of
    the six counted types has a persisted ``StixTypeCount`` whose ``count``
    equals the number of objects of that type the bundle carried (``0`` when
    absent), and whose ``description`` is non-empty. Uncounted-type noise objects
    do not perturb any of the six values.

    Validates: Requirements 3.2, 3.3
    """
    counts, noise = plan
    version = "18.0"

    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        bundle = _build_bundle(counts, noise)

        stix_service = StixService()
        service = StixPersistenceService(session=session, stix_service=stix_service)

        result = service.persist_bundle(version, bundle)
        assert result.persisted is True

        # Read the persisted per-type counts back from the DB (not the DTO) so we
        # assert the stored rows, keyed by stix_type.
        rows = session.execute(select(StixTypeCount)).scalars().all()
        persisted_by_type = {row.stix_type: row for row in rows}

        # Exactly the six counted types are persisted (one row each).
        assert set(persisted_by_type) == set(_COUNTED_TYPES)
        assert len(rows) == len(_COUNTED_TYPES)

        # Property 4: each persisted count equals the bundle's object count for
        # that type; description is non-empty (Req. 3.3).
        for stix_type in _COUNTED_TYPES:
            row = persisted_by_type[stix_type]
            assert row.count == counts[stix_type], (
                f"persisted count for {stix_type!r} is {row.count}, "
                f"expected {counts[stix_type]} (bundle object count)"
            )
            assert isinstance(row.description, str) and row.description.strip(), (
                f"persisted description for {stix_type!r} must be non-empty"
            )
    finally:
        session.close()
        factory.kw["bind"].dispose()
        for base in (Path(db_tmp_dir),):
            for root, _dirs, files in os.walk(base, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                os.rmdir(root)
