"""Property test for the campaign-overview isolation of StixPackageReadService
(task 4.3).

# Feature: stix-package-persistence-and-overview, Property 5: Campaign overview
# returns data of the selected package only.

Validates: Requirements 4.4

This is a *feature* property test (the ``stix-package-persistence-and-overview``
spec is a feature, not a bugfix): the property is expected to **hold** against
the current implementation of
:meth:`~app.services.stix.stix_package_read_service.StixPackageReadService.campaign_overview`.

Property (design.md — Property 5)
---------------------------------
*For every* ``campaign_key`` that exists in a persisted package, the campaign
overview returns **only** the techniques and actors of that campaign in that
package — no cross-version nor cross-campaign leakage (Req. 4.4).

To make leakage observable, two **distinct** versions (two different entries of
``KNOWN_VERSIONS``) are generated, each with its OWN bundle carrying DIFFERENT
campaigns, each campaign with its own techniques (``uses`` relationships) and
attributed actors (``attributed-to`` relationships). Both bundles are persisted
into the SAME database. The technique ids and actor ids are made distinguishable
per version (prefixed with the version), so any content of version B surfacing
in a version-A campaign overview would be detectable.

For any campaign of version A, ``campaign_overview(version_A, campaign_key)``
must return exactly the technique_ids and actor stix_ids that belong to that
campaign in version A — built as an oracle from the generated data — and NONE
from version B nor from any sibling campaign.

Strategy / isolation
--------------------
Each example draws a *world* per version: a set of campaigns, each owning its
techniques and actors, with the exact expected (technique_ids, actor stix_ids)
recorded as the oracle. A valid STIX bundle is assembled from the world (campaign
/ attack-pattern / intrusion-set objects plus ``uses`` and ``attributed-to``
relationships), held in memory as
``enterprise-attack-{version}.json`` and persisted via
:class:`StixPersistenceService` fed by a :class:`StixService` with a ``None``
``http_client`` (no network — the backfill path never downloads).

The persistence runs against an **ephemeral** SQLite ``Session`` (engine on a
temporary file with the full schema via ``Base.metadata.create_all``, the same
fixture pattern as ``test_stix_persistence_property_roundtrip.py``, using
``create_db_engine(Settings(database_url=...))`` + ``import_models()``). Nothing
here touches the network nor an AI client.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.services.stix.stix_package_read_service import StixPackageReadService
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import StixService

# ---------------------------------------------------------------------------
# Ephemeral DB fixture: a SQLite engine on a temporary file with the full schema
# via Base.metadata.create_all (same pattern as
# test_stix_persistence_property_roundtrip.py).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``.

    A real file engine (not ``:memory:``) is used so ``create_all`` runs against
    a disposable file database, mirroring the sibling property fixture.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stix_read_isolation_db_")
    db_path = Path(tmp_dir) / "isolation.db"
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
# Hypothesis strategy — a per-version "world" of campaigns, each owning its own
# techniques and actors, plus the oracle of expected ids per campaign.
# ---------------------------------------------------------------------------

# Two distinct KNOWN_VERSIONS so both bundles are realistic versions and so the
# content is persisted into the same database under two different packages.
VERSION_A = "18.0"
VERSION_B = "17.1"


def _stix_id(prefix: str) -> str:
    """A syntactically valid STIX id ``{prefix}--{uuid4}``."""
    return f"{prefix}--{uuid.uuid4()}"


@st.composite
def _world(draw, version_tag: str) -> dict:
    """Draw a per-version world and the oracle of expected ids per campaign.

    Returns a dict with:

    * ``bundle`` — a valid STIX bundle (``dict`` with an ``"objects"`` list)
      carrying the campaigns, their attack-patterns/intrusion-sets and the
      ``uses`` / ``attributed-to`` relationships;
    * ``expected`` — the oracle: ``{campaign_stix_id: {"technique_ids": set,
      "actor_stix_ids": set}}`` recording exactly which technique ids and actor
      stix ids belong to each campaign in this version.

    Technique ids and actor ids are prefixed with ``version_tag`` so any leakage
    from the other version would be detectable. At least one campaign owns at
    least one technique and one actor (so the property is non-trivially
    exercised); every campaign is non-curated, so its ``campaign_key`` is its
    ``stix_id`` (``curated_slug`` is ``None`` for generated campaigns).
    """
    objects: list[dict] = []
    expected: dict[str, dict] = {}

    n_campaigns = draw(st.integers(min_value=1, max_value=3))
    for c in range(n_campaigns):
        campaign_stix_id = _stix_id("campaign")
        objects.append(
            {
                "type": "campaign",
                "id": campaign_stix_id,
                "name": f"Campaign {version_tag}-{c}",
            }
        )
        expected[campaign_stix_id] = {
            "technique_ids": set(),
            "actor_stix_ids": set(),
        }

        # Techniques owned by THIS campaign. The technique external_id is
        # prefixed with the version tag so cross-version leakage is detectable.
        n_techniques = draw(st.integers(min_value=0, max_value=3))
        for t in range(n_techniques):
            technique_id = f"T{version_tag}{c}{t}"
            attack_pattern_stix_id = _stix_id("attack-pattern")
            objects.append(
                {
                    "type": "attack-pattern",
                    "id": attack_pattern_stix_id,
                    "name": f"Technique {technique_id}",
                    "external_references": [
                        {"source_name": "mitre-attack", "external_id": technique_id}
                    ],
                    "kill_chain_phases": [
                        {"kill_chain_name": "mitre-attack", "phase_name": f"tactic-{t}"}
                    ],
                }
            )
            objects.append(
                {
                    "type": "relationship",
                    "id": _stix_id("relationship"),
                    "relationship_type": "uses",
                    "source_ref": campaign_stix_id,
                    "target_ref": attack_pattern_stix_id,
                }
            )
            expected[campaign_stix_id]["technique_ids"].add(technique_id)

        # Actors attributed to THIS campaign. The actor stix_id already carries
        # the version tag (via the prefix below) so leakage is detectable.
        n_actors = draw(st.integers(min_value=0, max_value=3))
        for a in range(n_actors):
            actor_stix_id = f"intrusion-set--{version_tag}-{c}-{a}-{uuid.uuid4()}"
            objects.append(
                {
                    "type": "intrusion-set",
                    "id": actor_stix_id,
                    "name": f"Actor {version_tag}-{c}-{a}",
                }
            )
            objects.append(
                {
                    "type": "relationship",
                    "id": _stix_id("relationship"),
                    "relationship_type": "attributed-to",
                    "source_ref": campaign_stix_id,
                    "target_ref": actor_stix_id,
                }
            )
            expected[campaign_stix_id]["actor_stix_ids"].add(actor_stix_id)

    # Guarantee at least one campaign owns at least one technique and one actor
    # so the isolation property is exercised non-trivially on every example.
    first_campaign_id = next(iter(expected))
    if not expected[first_campaign_id]["technique_ids"]:
        technique_id = f"T{version_tag}seed"
        attack_pattern_stix_id = _stix_id("attack-pattern")
        objects.append(
            {
                "type": "attack-pattern",
                "id": attack_pattern_stix_id,
                "name": f"Technique {technique_id}",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": technique_id}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": "tactic-seed"}
                ],
            }
        )
        objects.append(
            {
                "type": "relationship",
                "id": _stix_id("relationship"),
                "relationship_type": "uses",
                "source_ref": first_campaign_id,
                "target_ref": attack_pattern_stix_id,
            }
        )
        expected[first_campaign_id]["technique_ids"].add(technique_id)
    if not expected[first_campaign_id]["actor_stix_ids"]:
        actor_stix_id = f"intrusion-set--{version_tag}-seed-{uuid.uuid4()}"
        objects.append(
            {
                "type": "intrusion-set",
                "id": actor_stix_id,
                "name": f"Actor {version_tag}-seed",
            }
        )
        objects.append(
            {
                "type": "relationship",
                "id": _stix_id("relationship"),
                "relationship_type": "attributed-to",
                "source_ref": first_campaign_id,
                "target_ref": actor_stix_id,
            }
        )
        expected[first_campaign_id]["actor_stix_ids"].add(actor_stix_id)

    bundle = {
        "type": "bundle",
        "id": _stix_id("bundle"),
        "spec_version": "2.1",
        "objects": objects,
    }
    return {"bundle": bundle, "expected": expected}


# ===========================================================================
# Property 5 — campaign overview returns data of the selected package only.
# ===========================================================================


# Feature: stix-package-persistence-and-overview, Property 5: Campaign overview
# returns data of the selected package only.
@settings(
    max_examples=40,
    # Each example does disk I/O (ephemeral SQLite),
    # whose cost varies with the environment; the default ``deadline`` (200ms)
    # is therefore a source of *timing* flakiness, not of correctness.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    world_a=_world(VERSION_A),
    world_b=_world(VERSION_B),
)
def test_property5_campaign_overview_isolated_to_selected_package(
    world_a: dict,
    world_b: dict,
) -> None:
    """Campaign overview returns only the selected package's campaign data.

    Two distinct versions, each with its own campaigns/techniques/actors, are
    persisted into the SAME database. For every campaign of version A,
    ``campaign_overview(VERSION_A, campaign_key)`` must return exactly the
    technique_ids and actor stix_ids the oracle records for that campaign in
    version A — and none belonging to version B nor to a sibling campaign
    (Req. 4.4).

    Validates: Requirements 4.4
    """
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        stix_service = StixService()
        persistence = StixPersistenceService(session=session, stix_service=stix_service)

        # Persist BOTH versions into the SAME database.
        for version, world in ((VERSION_A, world_a), (VERSION_B, world_b)):
            result = persistence.persist_bundle(version, world["bundle"])
            assert result.persisted is True

        read_service = StixPackageReadService(session)

        # The full set of version-B ids, used to assert NO cross-version leakage.
        all_b_technique_ids: set[str] = set()
        all_b_actor_ids: set[str] = set()
        for entry in world_b["expected"].values():
            all_b_technique_ids |= entry["technique_ids"]
            all_b_actor_ids |= entry["actor_stix_ids"]

        # For every campaign of version A, the overview must match its oracle
        # exactly — with no data from version B nor from a sibling campaign.
        for campaign_stix_id, oracle in world_a["expected"].items():
            # Generated campaigns are non-curated, so the campaign_key is the
            # stix_id (curated_slug is None).
            overview = read_service.campaign_overview(VERSION_A, campaign_stix_id)

            got_technique_ids = {t.technique_id for t in overview.techniques}
            got_actor_ids = {a.stix_id for a in overview.actors}

            # Exact match against the oracle for this campaign in version A.
            assert got_technique_ids == oracle["technique_ids"]
            assert got_actor_ids == oracle["actor_stix_ids"]

            # Explicit no-leakage assertions: none of version B's ids appear
            # (ids are prefixed per version, so any overlap would be a leak).
            assert got_technique_ids.isdisjoint(all_b_technique_ids)
            assert got_actor_ids.isdisjoint(all_b_actor_ids)
    finally:
        session.close()
        factory.kw["bind"].dispose()
        for base in (Path(db_tmp_dir),):
            for root, _dirs, files in os.walk(base, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                os.rmdir(root)
