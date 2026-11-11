"""Property-based test: Stage 1 completeness reflects the database.

# Feature: stix-base-persisted-state-and-file-cleanup

Property 5: Completude do Estágio 1 reflete o banco
Validates: Requirements 3.4, 3.5

Hypothesis: for any subset of known versions marked persisted/not-persisted,
``Stage1Service.complete(version).completed`` is true if and only if the version
is persisted in the database (and the dashboard could be assembled from the DB),
regardless of whether a Bundle_File happens to exist on disk. A persisted
version seeds its structured content through ``StixPersistenceService``
(``persist_from_disk``) and reads the dashboard/campaigns straight from the
database via ``StixPackageReadService``; a non-persisted version stays not
completed even when a stray Bundle_File is left on disk.

Isolation (no real files / no real network)
-------------------------------------------
* Bundles live under an isolated per-example ``data_dir`` as
  ``enterprise-attack-{version}.json`` and the service is built with
  ``StixService(data_dir=..., http_client=<failing>)`` so the real
  ``sticks/data/stix`` is never touched and no network egress ever runs — the
  completion verdict is derived purely from the database.
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
from app.services.stix.stage1_service import Stage1Service
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


def _valid_bundle_objects(seed: int) -> list[dict]:
    """A small-but-valid STIX bundle exercising every persisted table.

    The shape is deterministic per ``seed`` so a persisted version always has a
    dashboard the read service can assemble from the database.
    """
    counter = 0
    objects: list[dict] = []

    attack_pattern_ids: list[str] = []
    for i in range(2):
        ap_id = _stix_id("attack-pattern", counter)
        counter += 1
        attack_pattern_ids.append(ap_id)
        objects.append(
            {
                "type": "attack-pattern",
                "id": ap_id,
                "name": f"Technique {seed}-{i}",
                "description": f"Technique description {seed}-{i}",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": f"T{1000 + i}"}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
                ],
            }
        )

    campaign_id = _stix_id("campaign", counter)
    counter += 1
    objects.append(
        {
            "type": "campaign",
            "id": campaign_id,
            "name": f"Campaign {seed}",
            "description": f"Campaign description {seed}",
            "external_references": [
                {"source_name": "mitre-attack", "external_id": "C9000"}
            ],
        }
    )

    intrusion_set_id = _stix_id("intrusion-set", counter)
    counter += 1
    objects.append(
        {
            "type": "intrusion-set",
            "id": intrusion_set_id,
            "name": f"Actor {seed}",
        }
    )

    objects.append(
        {
            "type": "relationship",
            "id": _stix_id("relationship", counter),
            "relationship_type": "uses",
            "source_ref": campaign_id,
            "target_ref": attack_pattern_ids[0],
        }
    )
    counter += 1
    objects.append(
        {
            "type": "relationship",
            "id": _stix_id("relationship", counter),
            "relationship_type": "attributed-to",
            "source_ref": campaign_id,
            "target_ref": intrusion_set_id,
        }
    )

    return objects


def _bundle_text(objects: list[dict]) -> str:
    """Serialize a valid STIX bundle (``{"objects": [...]}``) to text."""
    return json.dumps({"type": "bundle", "objects": objects})


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a valid STIX bundle to disk at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_bundle_text(objects), encoding="utf-8")


class _FailingHttpClient:
    """Fake ``http_client`` that fails if called.

    The completion verdict is derived purely from the database, so no network
    egress may happen; any ``get`` call is a test failure.
    """

    def get(self, url: str, timeout: int | None = None):  # noqa: ANN001, ANN201
        raise AssertionError("Stage 1 completion must not reach the network")


# A plan entry: a known version, whether it should be persisted, and whether a
# stray Bundle_File is left on disk (to prove disk presence does not matter).
_version_plan = st.lists(
    st.tuples(
        st.sampled_from(KNOWN_VERSIONS),
        st.booleans(),  # persist in the database?
        st.booleans(),  # leave a stray file on disk?
    ),
    min_size=1,
    max_size=len(KNOWN_VERSIONS),
    unique_by=lambda entry: entry[0],
)


# Feature: stix-base-persisted-state-and-file-cleanup, Property 5: Completude do Estágio 1 reflete o banco
@PROP_SETTINGS
@given(plan=_version_plan)
def test_property5_stage1_completeness_reflects_db(
    plan: list[tuple[str, bool, bool]],
) -> None:
    """``complete(version).completed`` is true iff the version is persisted.

    For an arbitrary set of versions marked persisted/not and with/without a
    stray Bundle_File on disk, Stage 1 completes exactly the persisted versions
    (reading the dashboard/campaigns from the database) and never completes a
    non-persisted version, regardless of a file on disk.

    Validates: Requirements 3.4, 3.5
    """
    data_dir = Path(tempfile.mkdtemp(prefix="stix_stage1_db_data_"))
    db_dir = Path(tempfile.mkdtemp(prefix="stix_stage1_db_db_"))
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
        session = session_factory()
        try:
            read_service = StixPackageReadService(session)

            # --- Seed the database: persist exactly the flagged versions -----
            # Persistence deletes the Bundle_File after the commit (the feature
            # under test), so we persist first and then (optionally) drop a stray
            # file on disk afterwards to prove disk presence is irrelevant.
            for seed, (version, persist, _leave_file) in enumerate(plan):
                if not persist:
                    continue
                bundle_path = data_dir / f"enterprise-attack-{version}.json"
                _write_bundle(bundle_path, _valid_bundle_objects(seed))
                persist_stix = StixService(
                    data_dir=data_dir, http_client=_FailingHttpClient()
                )
                persistence = StixPersistenceService(session, persist_stix)
                result = persistence.persist_from_disk(version)
                assert result.persisted is True

            # --- Leave stray Bundle_Files on disk where requested ------------
            # This covers both persisted versions (whose file was deleted) and
            # non-persisted ones: in every case the on-disk file must not change
            # the completion verdict.
            for seed, (version, _persist, leave_file) in enumerate(plan):
                if not leave_file:
                    continue
                bundle_path = data_dir / f"enterprise-attack-{version}.json"
                _write_bundle(bundle_path, _valid_bundle_objects(seed))

            # --- Assert: completed iff persisted, regardless of disk ---------
            service = Stage1Service(
                StixService(data_dir=data_dir, http_client=_FailingHttpClient()),
                read_service,
            )
            for version, persist, _leave_file in plan:
                outcome = service.complete(version)
                is_persisted = read_service.is_persisted(version)

                assert is_persisted is persist
                # The core biconditional: completed iff persisted in the DB.
                assert outcome.completed is persist
                # ``bundle_on_disk`` keeps its name but now carries is_persisted.
                assert outcome.bundle_on_disk is persist

                if persist:
                    # The dashboard/campaigns were assembled from the database.
                    assert outcome.dashboard is not None
                    assert outcome.error_message is None
                    counts = {c.stix_type: c.count for c in outcome.dashboard.counts}
                    assert counts.get("attack-pattern") == 2
                    assert counts.get("campaign") == 1
                    assert len(outcome.campaigns) == 1
                else:
                    assert outcome.dashboard is None
                    assert outcome.campaigns == []
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(db_dir, ignore_errors=True)
