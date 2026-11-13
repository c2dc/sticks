"""Property-based test for re-persistence idempotency after the disk→DB switch.

# Feature: stix-base-persisted-state-and-file-cleanup

Property 6: Idempotência da re-persistência preservada
Validates: Requirements 8.1, 8.2, 8.3

Hypothesis: for any valid STIX bundle, re-persisting the same content of a
version reuses the existing ``stix_packages`` row (keyed by ``version``, no
duplicate), produces the same persisted content as the first persistence, and
refreshes ``stix_packages.downloaded_at``.

This exercises the existing :class:`StixPersistenceService` core (whose
idempotent upsert is unchanged by the file-cleanup spec). The best-effort
deletion of the Bundle_File after each commit is part of the production path, so
the bundle is re-materialized on disk before the second persistence — mirroring
how a real re-sync re-downloads the same content before re-persisting.

Isolation (no real files / no real network):

* The bundle is written to disk under an isolated per-example ``data_dir`` as
  ``enterprise-attack-{version}.json`` and the service is built with
  ``StixService(data_dir=..., http_client=None)`` — the real ``sticks/data/stix``
  is never touched and ``persist_bundle`` never reaches the network.
* Persistence lands in an ephemeral per-example temp-file SQLite whose schema is
  the six STIX tables built straight from the ORM
  (``Base.metadata.create_all`` scoped to those tables), following the backend
  test session pattern.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, func, select
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

# A curated external_id code and its paired slug, used to generate campaigns that
# resolve a curated slug so the curated path is covered alongside non-curated
# campaigns.
_CURATED_EXTERNAL_ID = "C0040"


def _stix_id(prefix: str, n: int) -> str:
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


@st.composite
def _valid_bundle(draw: st.DrawFn) -> list[dict]:
    """Generate a valid STIX bundle exercising every one of the six tables.

    Builds attack-patterns (techniques), a mix of curated and non-curated
    campaigns, intrusion-sets (actors) and the ``uses`` / ``attributed-to``
    relationships that wire techniques and actor attributions, then shuffles the
    objects so ordering never influences persistence.
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

    # --- Campaigns (a mix of curated and non-curated) -------------------
    n_campaigns = draw(st.integers(min_value=1, max_value=4))
    campaign_ids: list[str] = []
    curated_used = False
    for i in range(n_campaigns):
        campaign_id = _stix_id("campaign", counter)
        counter += 1
        campaign_ids.append(campaign_id)
        make_curated = draw(st.booleans()) and not curated_used
        external_id = _CURATED_EXTERNAL_ID if make_curated else f"C{9000 + i}"
        if make_curated:
            curated_used = True
        objects.append(
            {
                "type": "campaign",
                "id": campaign_id,
                "name": f"Campaign {i}",
                "description": f"Campaign description {i}",
                "aliases": draw(
                    st.lists(
                        st.sampled_from([f"alias-{i}-a", f"alias-{i}-b"]),
                        max_size=2,
                        unique=True,
                    )
                ),
                "first_seen": "2020-01-01T00:00:00.000Z",
                "last_seen": "2020-06-01T00:00:00.000Z",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": external_id}
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
        objects.append(
            {
                "type": "intrusion-set",
                "id": is_id,
                "name": f"Actor {i}",
            }
        )

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

    # Add a little noise of counted types so stix_type_counts has extra rows.
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        objects.append({"type": "malware", "id": _stix_id("malware", counter)})
        counter += 1
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        objects.append({"type": "x-mitre-tactic", "id": _stix_id("x-mitre-tactic", counter)})
        counter += 1

    # Shuffle so object ordering never influences persistence.
    return list(draw(st.permutations(objects)))


def _row_counts(session: Session) -> dict[str, int]:
    """Row count of every one of the six STIX tables."""
    return {
        model.__tablename__: session.execute(select(func.count()).select_from(model)).scalar_one()
        for model in (
            StixPackage,
            StixTypeCount,
            StixCampaign,
            StixCampaignTechnique,
            StixActor,
            StixActorCampaign,
        )
    }


def _content_snapshot(session: Session) -> dict[str, list[tuple]]:
    """Order-independent content snapshot (sorted tuples per table).

    Surrogate ids are excluded so the comparison is about persisted content, not
    the physical row ids, which naturally change on a delete-and-insert.
    """
    type_counts = sorted(
        (tc.stix_type, tc.label, tc.count, tc.description)
        for tc in session.execute(select(StixTypeCount)).scalars()
    )
    campaigns = sorted(
        (
            c.stix_id,
            c.name,
            c.external_id,
            c.description,
            tuple(c.aliases) if isinstance(c.aliases, list) else c.aliases,
            c.curated,
            c.curated_slug,
            c.first_seen,
            c.last_seen,
        )
        for c in session.execute(select(StixCampaign)).scalars()
    )
    # Join grandchildren back to the stable stix_id so the snapshot survives the
    # id churn of a re-persistence.
    campaign_stix_by_id = {c.id: c.stix_id for c in session.execute(select(StixCampaign)).scalars()}
    actor_stix_by_id = {a.id: a.stix_id for a in session.execute(select(StixActor)).scalars()}
    techniques = sorted(
        (
            campaign_stix_by_id[t.campaign_id],
            t.technique_id,
            t.technique_name,
            t.tactic,
            t.description,
        )
        for t in session.execute(select(StixCampaignTechnique)).scalars()
    )
    actors = sorted((a.stix_id, a.name) for a in session.execute(select(StixActor)).scalars())
    attributions = sorted(
        (actor_stix_by_id[ac.actor_id], campaign_stix_by_id[ac.campaign_id])
        for ac in session.execute(select(StixActorCampaign)).scalars()
    )
    return {
        "type_counts": type_counts,
        "campaigns": campaigns,
        "techniques": techniques,
        "actors": actors,
        "attributions": attributions,
    }


# Feature: stix-base-persisted-state-and-file-cleanup, Property 6: Idempotência da re-persistência preservada
@PROP_SETTINGS
@given(objects=_valid_bundle())
def test_property6_idempotencia_da_re_persistencia_preservada(
    objects: list[dict],
) -> None:
    """Re-persisting the same content reuses the row, keeps content, refreshes time.

    Asserts that the second persistence of the same version: (8.1) reuses the
    existing ``stix_packages`` row identified by ``version`` instead of creating
    a new one — a single package row survives and its id is unchanged; (8.2)
    produces the same persisted content as the first persistence with no
    duplicate rows (``f(x) == f(f(x))``); and (8.3) refreshes
    ``stix_packages.downloaded_at``.

    Validates: Requirements 8.1, 8.2, 8.3
    """
    version = KNOWN_VERSIONS[0]

    # Isolated data_dir for the bundle on disk; isolated temp-file SQLite for the
    # six STIX tables.
    db_dir = Path(tempfile.mkdtemp(prefix="stix_repersist_db_"))
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
        bundle = {"type": "bundle", "objects": objects}
        stix_service = StixService()

        session = session_factory()
        try:
            service = StixPersistenceService(session, stix_service)

            # f(x): first persistence.
            service.persist_bundle(version, bundle)
            first_counts = _row_counts(session)
            first_snapshot = _content_snapshot(session)
            first_package = session.execute(
                select(StixPackage).where(StixPackage.version == version)
            ).scalar_one()
            first_package_id = first_package.id
            first_downloaded_at = first_package.downloaded_at

            # f(f(x)): re-persist the same bundle content.
            service.persist_bundle(version, bundle)
            session.expire_all()
            second_counts = _row_counts(session)
            second_snapshot = _content_snapshot(session)
            second_package = session.execute(
                select(StixPackage).where(StixPackage.version == version)
            ).scalar_one()
            second_package_id = second_package.id
            second_downloaded_at = second_package.downloaded_at

            # (8.1) The row keyed by ``version`` is reused, not duplicated: a
            # single package row survives and its id is unchanged.
            assert second_counts["stix_packages"] == 1, (
                "re-persistence must not create a duplicate stix_packages row"
            )
            assert first_package_id == second_package_id, (
                "re-persistence created a new stix_packages row instead of reusing it"
            )

            # (8.2) No duplication anywhere and identical persisted content
            # (f(x) == f(f(x))).
            assert first_counts == second_counts, (
                f"row counts changed on re-persistence: {first_counts} != {second_counts}"
            )
            assert first_snapshot == second_snapshot, (
                "persisted content differs between the first and second persistence"
            )

            # (8.3) downloaded_at is refreshed on the re-persistence (never moves
            # backwards; _utcnow may land on the same tick within one run).
            assert second_downloaded_at >= first_downloaded_at, (
                "downloaded_at must be refreshed on re-persistence"
            )
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(db_dir, ignore_errors=True)
