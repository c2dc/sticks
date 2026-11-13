"""Example-based unit tests for StixPackageReadService (task 4.4).

Feature: stix-package-persistence-and-overview.

Validates: Requirements 3.1, 4.1, 4.3, 8.3

These are focused *unit* tests of the read service only. Rather than going
through the persistence service, each test SEEDs rows directly via the ORM
models (:mod:`app.models.domain`: :class:`StixPackage`, :class:`StixTypeCount`,
:class:`StixCampaign`, :class:`StixCampaignTechnique`, :class:`StixActor`,
:class:`StixActorCampaign`) into an ephemeral SQLite session built with
``Base.metadata.create_all`` — the same session pattern as
``test_stix_persistence_property_roundtrip.py``. The read service is then
exercised against those seeded rows.

Covered behavior:

1. :meth:`StixPackageReadService.package_overview` assembles the overview from
   seeded rows (identity, per-type counts and the derived totals) — Req. 3.1.
2. :meth:`StixPackageReadService.list_campaigns` returns the summaries with the
   ``campaign_key`` resolved as ``curated_slug`` (curated) or ``stix_id``
   (plain) — Req. 4.1.
3. :meth:`StixPackageReadService.campaign_overview` happy path returns the
   identity, deserialized aliases, techniques and attributed actors of that
   campaign only, with no cross-campaign leakage — Req. 4.3.
4. An unknown ``campaign_key`` raises :class:`CampaignNotFoundError`.
5. An unknown version is not persisted (``is_persisted == False``) and
   :meth:`package_overview` raises :class:`StixPackageNotPersistedError`; a
   seeded version reports ``is_persisted == True`` — Req. 8.3.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    StixActor,
    StixActorCampaign,
    StixCampaign,
    StixCampaignTechnique,
    StixPackage,
    StixTypeCount,
)
from app.services.stix.stix_package_read_service import (
    CampaignNotFoundError,
    StixPackageNotPersistedError,
    StixPackageReadService,
)

# ---------------------------------------------------------------------------
# Ephemeral DB fixture: a SQLite engine on a temporary file with the full
# schema via Base.metadata.create_all (same pattern as
# test_stix_persistence_property_roundtrip.py).
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    """Yield an ephemeral SQLite ``Session`` with the full schema created."""
    tmp_dir = tempfile.mkdtemp(prefix="stix_read_service_unit_db_")
    db_path = Path(tmp_dir) / "read_service_unit.db"
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


# ---------------------------------------------------------------------------
# Test 1 — package_overview from seeded rows.
# ---------------------------------------------------------------------------


def test_package_overview_from_seeded_rows(session: Session) -> None:
    """``package_overview`` assembles identity, counts and totals from the DB.

    Seeds a package + six type counts (including ``attack-pattern`` /
    ``x-mitre-tactic`` / ``malware`` / ``relationship`` with known counts) + N
    campaigns + M actors, then asserts the overview reflects the seeded values:
    ``version`` / ``name`` / ``downloaded_at`` (ISO string), the counts list and
    the campaigns/actors/techniques/tactics/malware/relationships totals.

    Validates: Requirements 3.1
    """
    downloaded_at = dt.datetime(2024, 5, 1, 12, 30, 45)
    package = StixPackage(
        version="18.0",
        name="Enterprise ATT&CK v18.0",
        downloaded_at=downloaded_at,
    )
    session.add(package)
    session.flush()  # assign package.id

    # Six type counts with known counts for the type-derived totals.
    type_counts = [
        ("campaign", "campaigns", 3, "campaign"),
        ("attack-pattern", "techniques", 42, "attack-pattern"),
        ("x-mitre-tactic", "tactics", 14, "x-mitre-tactic"),
        ("intrusion-set", "actors", 2, "intrusion-set"),
        ("malware", "malware", 7, "malware"),
        ("relationship", "relationships", 99, "relationship"),
    ]
    for stix_type, label, count, description in type_counts:
        session.add(
            StixTypeCount(
                package_id=package.id,
                stix_type=stix_type,
                label=label,
                count=count,
                description=description,
            )
        )

    # N = 3 campaigns and M = 2 actors (counted directly from child rows).
    for i in range(3):
        session.add(
            StixCampaign(
                package_id=package.id,
                stix_id=f"campaign--{i}",
                name=f"Campaign {i}",
                curated=False,
            )
        )
    for i in range(2):
        session.add(
            StixActor(
                package_id=package.id,
                stix_id=f"intrusion-set--{i}",
                name=f"Actor {i}",
            )
        )
    session.commit()

    overview = StixPackageReadService(session).package_overview("18.0")

    assert overview.version == "18.0"
    assert overview.name == "Enterprise ATT&CK v18.0"
    # The naive-UTC timestamp is serialized with an explicit ``Z`` (Zulu/UTC)
    # designator so the contract is unambiguous for the frontend.
    assert overview.downloaded_at == downloaded_at.isoformat() + "Z"

    # The counts list mirrors every seeded type-count row.
    returned = {(c.stix_type, c.label, c.count, c.description) for c in overview.counts}
    assert returned == set(type_counts)

    # Totals: campaigns/actors from child rows; the rest from per-type counts.
    assert overview.campaigns_total == 3
    assert overview.actors_total == 2
    assert overview.techniques_total == 42
    assert overview.tactics_total == 14
    assert overview.malware_total == 7
    assert overview.relationships_total == 99


# ---------------------------------------------------------------------------
# Test 2 — list_campaigns resolves campaign_key per curated flag.
# ---------------------------------------------------------------------------


def test_list_campaigns_resolves_campaign_key(session: Session) -> None:
    """``list_campaigns`` returns summaries with the right ``campaign_key``.

    Seeds one curated campaign (with a ``curated_slug``) and one plain campaign,
    then asserts the ``campaign_key`` is the ``curated_slug`` for the curated one
    and the ``stix_id`` for the plain one, and the remaining summary fields match.

    Validates: Requirements 4.1
    """
    package = StixPackage(
        version="18.0",
        name="Enterprise ATT&CK v18.0",
        downloaded_at=dt.datetime(2024, 5, 1, 12, 0, 0),
    )
    session.add(package)
    session.flush()

    session.add(
        StixCampaign(
            package_id=package.id,
            stix_id="campaign--curated",
            name="ShadowRay",
            external_id="C0045",
            curated=True,
            curated_slug="shadowray",
        )
    )
    session.add(
        StixCampaign(
            package_id=package.id,
            stix_id="campaign--plain",
            name="Plain Campaign",
            external_id=None,
            curated=False,
            curated_slug=None,
        )
    )
    session.commit()

    summaries = StixPackageReadService(session).list_campaigns("18.0")
    by_stix_id = {s.stix_id: s for s in summaries}
    assert set(by_stix_id) == {"campaign--curated", "campaign--plain"}

    curated = by_stix_id["campaign--curated"]
    assert curated.campaign_key == "shadowray"  # curated_slug wins
    assert curated.name == "ShadowRay"
    assert curated.external_id == "C0045"
    assert curated.curated is True
    assert curated.curated_slug == "shadowray"

    plain = by_stix_id["campaign--plain"]
    assert plain.campaign_key == "campaign--plain"  # falls back to stix_id
    assert plain.name == "Plain Campaign"
    assert plain.external_id is None
    assert plain.curated is False
    assert plain.curated_slug is None


# ---------------------------------------------------------------------------
# Test 3 — campaign_overview happy path with no cross-campaign leakage.
# ---------------------------------------------------------------------------


def test_campaign_overview_happy_path_no_leakage(session: Session) -> None:
    """``campaign_overview`` returns only that campaign's data.

    Seeds a target campaign with techniques + attributed actors, plus a second
    campaign in the same package with its own techniques/actors. Asserts the
    overview returns the target's identity, deserialized aliases, techniques and
    actors — and none of the other campaign's techniques/actors (Req. 4.4).

    Validates: Requirements 4.3
    """
    package = StixPackage(
        version="18.0",
        name="Enterprise ATT&CK v18.0",
        downloaded_at=dt.datetime(2024, 5, 1, 12, 0, 0),
    )
    session.add(package)
    session.flush()

    target = StixCampaign(
        package_id=package.id,
        stix_id="campaign--target",
        name="Target Campaign",
        external_id="C0001",
        description="A curated target campaign.",
        aliases=["Alias A", "Alias B"],
        curated=True,
        curated_slug="target-campaign",
    )
    other = StixCampaign(
        package_id=package.id,
        stix_id="campaign--other",
        name="Other Campaign",
        external_id="C0002",
        description="Another campaign in the same package.",
        aliases=["Other Alias"],
        curated=False,
        curated_slug=None,
    )
    session.add_all([target, other])
    session.flush()

    # Techniques: two for the target, one for the other.
    session.add_all(
        [
            StixCampaignTechnique(
                campaign_id=target.id,
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
                description="Target technique one.",
            ),
            StixCampaignTechnique(
                campaign_id=target.id,
                technique_id="T1071",
                technique_name="Application Layer Protocol",
                tactic="command-and-control",
                description="Target technique two.",
            ),
            StixCampaignTechnique(
                campaign_id=other.id,
                technique_id="T1486",
                technique_name="Data Encrypted for Impact",
                tactic="impact",
                description="Other technique.",
            ),
        ]
    )

    # Actors: one attributed to the target, one to the other.
    target_actor = StixActor(package_id=package.id, stix_id="intrusion-set--t", name="Target Actor")
    other_actor = StixActor(package_id=package.id, stix_id="intrusion-set--o", name="Other Actor")
    session.add_all([target_actor, other_actor])
    session.flush()
    session.add_all(
        [
            StixActorCampaign(actor_id=target_actor.id, campaign_id=target.id),
            StixActorCampaign(actor_id=other_actor.id, campaign_id=other.id),
        ]
    )
    session.commit()

    overview = StixPackageReadService(session).campaign_overview("18.0", "target-campaign")

    # Identity + aliases of the target campaign.
    assert overview.campaign_key == "target-campaign"
    assert overview.stix_id == "campaign--target"
    assert overview.name == "Target Campaign"
    assert overview.external_id == "C0001"
    assert overview.description == "A curated target campaign."
    assert overview.aliases == ["Alias A", "Alias B"]
    assert overview.curated is True
    assert overview.curated_slug == "target-campaign"

    # Only the target's techniques (no leakage of the other campaign's T1486).
    assert {t.technique_id for t in overview.techniques} == {"T1059", "T1071"}

    # Only the target's actor (no leakage of the other campaign's actor).
    assert [(a.stix_id, a.name) for a in overview.actors] == [("intrusion-set--t", "Target Actor")]


# ---------------------------------------------------------------------------
# Test 4 — unknown campaign_key raises CampaignNotFoundError.
# ---------------------------------------------------------------------------


def test_campaign_overview_unknown_key_raises(session: Session) -> None:
    """An unknown ``campaign_key`` raises :class:`CampaignNotFoundError`.

    Validates: Requirements 4.3
    """
    package = StixPackage(
        version="18.0",
        name="Enterprise ATT&CK v18.0",
        downloaded_at=dt.datetime(2024, 5, 1, 12, 0, 0),
    )
    session.add(package)
    session.flush()
    session.add(
        StixCampaign(
            package_id=package.id,
            stix_id="campaign--known",
            name="Known Campaign",
            curated=False,
        )
    )
    session.commit()

    service = StixPackageReadService(session)
    with pytest.raises(CampaignNotFoundError) as excinfo:
        service.campaign_overview("18.0", "does-not-exist")
    assert excinfo.value.version == "18.0"
    assert excinfo.value.campaign_key == "does-not-exist"


# ---------------------------------------------------------------------------
# Test 5 — non-persisted version: is_persisted + package_overview error.
# ---------------------------------------------------------------------------


def test_non_persisted_version_behavior(session: Session) -> None:
    """Unknown version is not persisted and raises on overview; seeded is.

    ``is_persisted(unknown)`` is ``False`` and ``package_overview(unknown)``
    raises :class:`StixPackageNotPersistedError`; after seeding a version,
    ``is_persisted(seeded)`` is ``True``.

    Validates: Requirements 8.3
    """
    service = StixPackageReadService(session)

    # No rows yet: the version is not persisted.
    assert service.is_persisted("99.0") is False
    with pytest.raises(StixPackageNotPersistedError) as excinfo:
        service.package_overview("99.0")
    assert excinfo.value.version == "99.0"

    # Seed a package row: that version is now persisted.
    session.add(
        StixPackage(
            version="18.0",
            name="Enterprise ATT&CK v18.0",
            downloaded_at=dt.datetime(2024, 5, 1, 12, 0, 0),
        )
    )
    session.commit()
    assert service.is_persisted("18.0") is True
