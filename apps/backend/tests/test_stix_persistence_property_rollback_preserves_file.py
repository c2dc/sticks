"""Property-based test for rollback preserving the Bundle_File and keeping the
version non-persisted (task 1.3 of the ``stix-base-persisted-state-and-file-cleanup``
spec).

# Feature: stix-base-persisted-state-and-file-cleanup, Property 2: Rollback preserva o arquivo e mantém não persistida

Property 2: Rollback preserva o arquivo e mantém não persistida
Validates: Requirements 1.2, 7.2

Hypothesis: for any version whose write phase fails (an exception raised inside
``_write_derived_content``), after the rollback the Bundle_File for that version
remains on disk and ``is_persisted(version)`` is false.

The failure is injected by replacing ``StixPersistenceService._write_derived_content``
with a stub that raises, forcing ``_derive_and_write`` into its exception path
(``self._session.rollback()``). Because the best-effort deletion runs only on the
committed (success) path, the on-disk bundle must be preserved and no
``stix_packages`` row may exist.

Isolation (no real files / no real network):

* The bundle is written to disk under an isolated per-example ``data_dir`` as
  ``enterprise-attack-{version}.json`` and the service is built with
  ``StixService(data_dir=..., http_client=None)`` — the real ``sticks/data/stix``
  is never touched and ``persist_from_disk`` never reaches the network.
* Persistence lands in an ephemeral per-example temp-file SQLite whose schema is
  the six STIX tables built straight from the ORM
  (``Base.metadata.create_all`` scoped to those tables), following the backend
  test session pattern.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.domain import (
    StixActor,
    StixActorCampaign,
    StixCampaign,
    StixCampaignTechnique,
    StixPackage,
    StixTypeCount,
)
from app.services.stix.stix_package_read_service import StixPackageReadService
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import KNOWN_VERSIONS, StixService

# A single shared iteration budget so the property runs >= 100 examples.
PROP_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# The six STIX tables this spec persists; the schema is scoped to exactly these
# so the ephemeral SQLite never tries to create the pgvector-backed tables.
_STIX_TABLES = [
    StixPackage.__table__,
    StixTypeCount.__table__,
    StixCampaign.__table__,
    StixCampaignTechnique.__table__,
    StixActor.__table__,
    StixActorCampaign.__table__,
]


def _stix_id(prefix: str, n: int) -> str:
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


@st.composite
def _valid_bundle(draw: st.DrawFn) -> list[dict]:
    """Generate a valid STIX bundle with campaigns, attack-patterns,
    intrusion-sets and ``uses`` / ``attributed-to`` relationships.

    The derivation phase of ``_derive_and_write`` parses this bundle before the
    (injected-to-fail) write phase runs, so the generated objects must form a
    valid bundle that ``StixService`` can read without error.
    """
    counter = 0
    objects: list[dict] = []

    # --- Attack-patterns (techniques) -----------------------------------
    n_techniques = draw(st.integers(min_value=1, max_value=4))
    attack_pattern_ids: list[str] = []
    for i in range(n_techniques):
        ap_id = _stix_id("attack-pattern", counter)
        counter += 1
        attack_pattern_ids.append(ap_id)
        objects.append(
            {
                "type": "attack-pattern",
                "id": ap_id,
                "name": f"Technique {i}",
                "description": f"Technique description {i}",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": f"T{1000 + i}"}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
                ],
            }
        )

    # --- Campaigns ------------------------------------------------------
    n_campaigns = draw(st.integers(min_value=1, max_value=4))
    campaign_ids: list[str] = []
    for i in range(n_campaigns):
        campaign_id = _stix_id("campaign", counter)
        counter += 1
        campaign_ids.append(campaign_id)
        objects.append(
            {
                "type": "campaign",
                "id": campaign_id,
                "name": f"Campaign {i}",
                "description": f"Campaign description {i}",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": f"C{9000 + i}"}
                ],
            }
        )

    # --- Intrusion-sets (actors) ----------------------------------------
    n_actors = draw(st.integers(min_value=1, max_value=3))
    intrusion_set_ids: list[str] = []
    for i in range(n_actors):
        is_id = _stix_id("intrusion-set", counter)
        counter += 1
        intrusion_set_ids.append(is_id)
        objects.append({"type": "intrusion-set", "id": is_id, "name": f"Actor {i}"})

    # --- "uses" relationships: campaign -> attack-pattern ---------------
    uses_pairs = draw(
        st.lists(
            st.tuples(
                st.sampled_from(campaign_ids),
                st.sampled_from(attack_pattern_ids),
            ),
            max_size=6,
            unique=True,
        )
    )
    for source_ref, target_ref in uses_pairs:
        objects.append(
            {
                "type": "relationship",
                "id": _stix_id("relationship", counter),
                "relationship_type": "uses",
                "source_ref": source_ref,
                "target_ref": target_ref,
            }
        )
        counter += 1

    # --- "attributed-to" relationships: campaign -> intrusion-set -------
    attributed_pairs = draw(
        st.lists(
            st.tuples(
                st.sampled_from(campaign_ids),
                st.sampled_from(intrusion_set_ids),
            ),
            max_size=6,
            unique=True,
        )
    )
    for source_ref, target_ref in attributed_pairs:
        objects.append(
            {
                "type": "relationship",
                "id": _stix_id("relationship", counter),
                "relationship_type": "attributed-to",
                "source_ref": source_ref,
                "target_ref": target_ref,
            }
        )
        counter += 1

    # Shuffle so object ordering never influences the outcome.
    return list(draw(st.permutations(objects)))


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a valid STIX bundle (``{"objects": [...]}``) to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "bundle", "objects": objects}), encoding="utf-8"
    )


# Feature: stix-base-persisted-state-and-file-cleanup, Property 2: Rollback preserva o arquivo e mantém não persistida
@PROP_SETTINGS
@given(objects=_valid_bundle(), entry_point=st.sampled_from(["download", "disk"]))
def test_property2_rollback_preserves_file_and_keeps_unpersisted(
    objects: list[dict], entry_point: str
) -> None:
    """A write-phase failure rolls back, preserving the file and leaving the
    version non-persisted.

    When ``_write_derived_content`` raises, ``_derive_and_write`` rolls the
    session back and re-raises; the best-effort deletion (which runs only after a
    successful commit) never executes. Therefore the Bundle_File must still exist
    on disk and ``is_persisted(version)`` must be false.

    Both shared-core entry points are exercised: ``download_and_persist`` (the
    bundle already on disk, so no network egress) and ``persist_from_disk``.

    Validates: Requirements 1.2, 7.2
    """
    version = KNOWN_VERSIONS[0]

    data_dir = Path(tempfile.mkdtemp(prefix="stix_rollback_data_"))
    db_dir = Path(tempfile.mkdtemp(prefix="stix_rollback_db_"))
    db_path = db_dir / "stix.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    Base.metadata.create_all(bind=engine, tables=_STIX_TABLES)
    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )

    try:
        bundle_path = data_dir / f"enterprise-attack-{version}.json"
        _write_bundle(bundle_path, objects)
        stix_service = StixService(data_dir=data_dir, http_client=None)

        session = session_factory()
        try:
            service = StixPersistenceService(session, stix_service)

            # Inject a failure inside the write phase so the shared core takes its
            # exception path (rollback + re-raise). Nothing is committed, so the
            # best-effort deletion never runs.
            def _boom(*_args: object, **_kwargs: object) -> None:
                raise RuntimeError("simulated write-phase failure")

            service._write_derived_content = _boom  # type: ignore[method-assign]

            with pytest.raises(RuntimeError, match="simulated write-phase failure"):
                if entry_point == "download":
                    service.download_and_persist(version)
                else:
                    service.persist_from_disk(version)

            # Rollback path preserves the on-disk bundle (Req. 1.2, 7.2).
            assert bundle_path.exists(), (
                "the Bundle_File must be preserved when the write phase rolls back"
            )

            # The version is not persisted: no stix_packages row survives.
            session.rollback()
            read_service = StixPackageReadService(session)
            assert read_service.is_persisted(version) is False, (
                "the version must remain non-persisted after a write-phase rollback"
            )
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(db_dir, ignore_errors=True)
