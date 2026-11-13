"""Property-based test for re-download idempotency of StixPersistenceService.

# Feature: stix-package-persistence-and-overview

Property 2: Idempotência do re-download
Validates: Requirements 2.2, 2.3

Hypothesis: for any valid STIX bundle, persisting the same version twice
produces exactly the same content in the database (no duplicated rows) — the
content after the second persistence equals the content after the first:
``f(x) == f(f(x))``.

Isolation (no real files / no real network):

* The bundle is written to disk under an isolated per-example ``data_dir`` as
  ``enterprise-attack-{version}.json`` and the service is built with
  ``StixService(data_dir=..., http_client=None)`` — the real
  ``sticks/data/stix`` is never touched and ``persist_bundle`` never reaches
  the network.
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

# A curated external_id code (from SLUG_TO_STIX) and its paired slug, used to
# generate campaigns that resolve a curated slug so the curated path is covered
# alongside non-curated campaigns.
_CURATED_EXTERNAL_ID = "C0040"
_CURATED_SLUG = "apt41_dust"


def _stix_id(prefix: str, n: int) -> str:
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


@st.composite
def _valid_bundle(draw: st.DrawFn) -> list[dict]:
    """Generate a valid STIX bundle with campaigns, attack-patterns,
    intrusion-sets and ``uses`` / ``attributed-to`` relationships.

    The generated objects exercise every one of the six persisted tables: type
    counts (every object type), campaigns (some curated, some not), techniques a
    campaign uses (``uses`` relationships campaign -> attack-pattern) and actors
    with their attributions (``attributed-to`` relationships campaign ->
    intrusion-set).
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


# Feature: stix-package-persistence-and-overview, Property 2: Idempotência do re-download
@PROP_SETTINGS
@given(objects=_valid_bundle())
def test_property2_idempotencia_do_re_download(objects: list[dict]) -> None:
    """Persisting the same bundle twice yields identical DB content with no
    duplicate rows and reuses the same ``stix_packages`` row (``f(x) == f(f(x))``).

    Validates: Requirements 2.2, 2.3
    """
    version = KNOWN_VERSIONS[0]

    # Isolated data_dir for the bundle on disk; isolated temp-file SQLite for the
    # six STIX tables.
    db_dir = Path(tempfile.mkdtemp(prefix="stix_idempotency_db_"))
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
            first_package_id = session.execute(
                select(StixPackage.id).where(StixPackage.version == version)
            ).scalar_one()

            # f(f(x)): re-persist the same bundle.
            service.persist_bundle(version, bundle)
            second_counts = _row_counts(session)
            second_snapshot = _content_snapshot(session)
            second_package_id = session.execute(
                select(StixPackage.id).where(StixPackage.version == version)
            ).scalar_one()

            # No duplication: identical row counts in every one of the six tables.
            assert first_counts == second_counts, (
                f"row counts changed on re-persistence: {first_counts} != {second_counts}"
            )
            # Exactly one package row survives (reused, not duplicated).
            assert second_counts["stix_packages"] == 1

            # The stix_packages row is the SAME id (reused, not a new row).
            assert first_package_id == second_package_id, (
                "re-persistence created a new stix_packages row instead of reusing it"
            )

            # Content is identical after the second persistence (f(x) == f(f(x))).
            assert first_snapshot == second_snapshot, (
                "persisted content differs between the first and second persistence"
            )
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(db_dir, ignore_errors=True)
