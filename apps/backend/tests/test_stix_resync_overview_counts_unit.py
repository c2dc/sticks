"""Targeted example for the partial -> re-sync overview counts (task 4.1).

Feature: stix-package-overview-resync-and-english-labels.

Validates: Requirements 1.3, 1.4

This single focused example proves the re-sync behaviour described in the
design: a package that was persisted from a *partial* bundle (one object of each
counted type) is re-persisted from the *complete* bundle through the existing
idempotent delete-and-insert
(:meth:`~app.services.stix.stix_persistence_service.StixPersistenceService.persist_from_disk`),
after which the overview totals equal the full bundle's object counts and no
duplicate per-type / campaign / actor rows survive.

Isolation mirrors ``test_stix_persistence_property_type_counts.py``: a
``StixService`` bound to an isolated ``data_dir`` (``http_client=None`` so no
network is ever reachable) and an ephemeral file-backed SQLite session built via
``Base.metadata.create_all``. The re-sync overwrites the same on-disk file with
the full bundle before the second ``persist_from_disk`` call.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StixActor, StixCampaign, StixTypeCount
from app.services.stix.stix_package_read_service import StixPackageReadService
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import StixService


_VERSION = "18.0"

# Known per-type object counts for the *complete* fixture bundle. These are the
# totals the overview must report after the re-sync.
_FULL_COUNTS: dict[str, int] = {
    "campaign": 4,
    "attack-pattern": 5,
    "x-mitre-tactic": 3,
    "intrusion-set": 2,
    "malware": 6,
    "relationship": 7,
}


@pytest.fixture()
def session() -> Iterator[Session]:
    """Yield an ephemeral file-backed SQLite ``Session`` with the full schema."""
    tmp_dir = tempfile.mkdtemp(prefix="stix_resync_overview_unit_db_")
    db_path = Path(tmp_dir) / "resync_overview_unit.db"
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
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()
        for root, _dirs, files in os.walk(tmp_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)


def _build_bundle(counts: dict[str, int]) -> dict:
    """Build a valid STIX bundle carrying ``counts[stix_type]`` objects per type.

    Ids are globally unique so the per-package ``UNIQUE`` constraints on
    ``stix_campaigns`` / ``stix_actors`` never collide. ``attack-pattern`` objects
    carry a usable ``external_id`` and a kill-chain phase, and each ``relationship``
    is a ``uses`` edge from the first campaign to one attack-pattern so the
    technique-derivation path has real data to walk (it does not change the
    counted totals).
    """
    objects: list[dict] = []
    seq = 0
    attack_pattern_ids: list[str] = []
    first_campaign_id: str | None = None

    for stix_type, amount in counts.items():
        for _ in range(amount):
            stix_id = f"{stix_type}--{seq:08d}-0000-0000-0000-000000000000"
            obj: dict = {
                "type": stix_type,
                "id": stix_id,
                "name": f"{stix_type}-name-{seq}",
            }
            if stix_type == "campaign" and first_campaign_id is None:
                first_campaign_id = stix_id
            if stix_type == "attack-pattern":
                obj["external_references"] = [
                    {"source_name": "mitre-attack", "external_id": f"T{1000 + seq}"}
                ]
                obj["kill_chain_phases"] = [
                    {"kill_chain_name": "mitre-attack", "phase_name": f"tactic-{seq}"}
                ]
                attack_pattern_ids.append(stix_id)
            if stix_type == "relationship":
                # A "uses" edge campaign -> attack-pattern when both endpoints
                # exist; otherwise a benign self-referencing edge that is still a
                # valid relationship object for counting purposes.
                index = len(
                    [o for o in objects if o.get("type") == "relationship"]
                )
                target = (
                    attack_pattern_ids[index % len(attack_pattern_ids)]
                    if attack_pattern_ids
                    else stix_id
                )
                obj["relationship_type"] = "uses"
                obj["source_ref"] = first_campaign_id or stix_id
                obj["target_ref"] = target
            objects.append(obj)
            seq += 1

    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-0000-0000-000000000000",
        "objects": objects,
    }


def test_partial_then_resync_overview_counts_match_full_bundle(
    session: Session,
) -> None:
    """Re-syncing a partial package yields full-bundle totals with no dupes.

    Seeds a *partial* package (one object of each counted type) via
    ``persist_from_disk``, then overwrites the on-disk bundle with the *complete*
    bundle and re-syncs through the same idempotent path. The overview totals
    must then equal the complete bundle's per-type object counts, and exactly one
    row per counted type / campaign object / intrusion-set object must survive
    (delete-and-insert, no accumulation).

    Validates: Requirements 1.3, 1.4
    """
    data_dir = Path(tempfile.mkdtemp(prefix="stix_resync_overview_data_"))
    bundle_path = data_dir / f"enterprise-attack-{_VERSION}.json"
    try:
        stix_service = StixService(data_dir=data_dir, http_client=None)
        persistence = StixPersistenceService(
            session=session, stix_service=stix_service
        )

        # Partial persistence: one object of each counted type.
        partial_counts = {stix_type: 1 for stix_type in _FULL_COUNTS}
        bundle_path.write_text(
            json.dumps(_build_bundle(partial_counts)), encoding="utf-8"
        )
        persistence.persist_from_disk(_VERSION)

        # Re-sync against the complete bundle through the same idempotent path.
        bundle_path.write_text(
            json.dumps(_build_bundle(_FULL_COUNTS)), encoding="utf-8"
        )
        persistence.persist_from_disk(_VERSION)

        overview = StixPackageReadService(session).package_overview(_VERSION)

        type_count_rows = session.execute(select(StixTypeCount)).scalars().all()
        campaign_row_count = session.execute(
            select(func.count()).select_from(StixCampaign)
        ).scalar_one()
        actor_row_count = session.execute(
            select(func.count()).select_from(StixActor)
        ).scalar_one()

        assert (
            overview.campaigns_total == _FULL_COUNTS["campaign"]
            and overview.actors_total == _FULL_COUNTS["intrusion-set"]
            and overview.techniques_total == _FULL_COUNTS["attack-pattern"]
            and overview.tactics_total == _FULL_COUNTS["x-mitre-tactic"]
            and overview.malware_total == _FULL_COUNTS["malware"]
            and overview.relationships_total == _FULL_COUNTS["relationship"]
            # No duplicate rows: exactly one per-type row per counted type, and one
            # campaign / actor row per bundle object of that type.
            and len(type_count_rows) == len(_FULL_COUNTS)
            and {row.stix_type for row in type_count_rows} == set(_FULL_COUNTS)
            and campaign_row_count == _FULL_COUNTS["campaign"]
            and actor_row_count == _FULL_COUNTS["intrusion-set"]
        )
    finally:
        for root, _dirs, files in os.walk(data_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)
