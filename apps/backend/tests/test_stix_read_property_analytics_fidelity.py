"""Property test for the analytics fidelity of StixPackageReadService (task 10.3).

# Feature: stix-package-persistence-and-overview, Property 6: Analytics faithfully
# reflects the persisted tables.

Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.6

This is a *feature* property test (the ``stix-package-persistence-and-overview``
spec is a feature, not a bugfix): the property is expected to **hold** against
the current implementation of
:meth:`~app.services.stix.stix_package_read_service.StixPackageReadService.package_analytics`.

Property (design.md — Property 6)
---------------------------------
*For every* persisted package, each aggregation of ``Package_Analytics`` equals
the count computed directly over the rows of that version:

* ``curated_total`` / ``uncurated_total`` match ``stix_campaigns`` grouped by the
  ``curated`` flag (Req. 12.6);
* every ``ActorRank.campaigns_total`` equals the number of attributions of that
  actor over ``stix_actor_campaigns`` (Req. 12.2);
* every ``CampaignRank.techniques_total`` equals the number of techniques of
  that campaign over ``stix_campaign_techniques`` (Req. 12.3);
* the ``tactics`` distribution equals ``stix_campaign_techniques`` grouped by a
  non-null ``tactic`` (Req. 12.4);
* no entry crosses data from another version (Req. 12.6).

Oracle
------
The oracle is computed **directly from the persisted rows** of the version under
test (not from the generated bundle), scoped by ``package_id`` with plain
SQLAlchemy queries. ``package_analytics`` runs its own aggregate queries; the
test asserts that the service result equals the row-derived oracle for the
*same* version, and — to make cross-version leakage observable — that version
A's analytics never carries an actor/campaign/tactic that only exists in version
B's rows.

Ranking order / ties
---------------------
Rankings are ordered by their total descending with no defined tie-break, so the
test does not assert a specific order among equal totals. Instead it asserts:

* the per-entity ``{key: total}`` map of the service equals the oracle map
  (computed with a ``top_n`` large enough to capture every row, so truncation
  does not hide a mismatch);
* the returned list is sorted non-increasingly by total (a valid ranking);
* with a small ``top_n`` the service returns exactly ``top_n`` entries whose
  totals are the ``top_n`` largest totals of the oracle (a valid top-N cut).

Strategy / isolation
--------------------
Each example draws a *world* per version: campaigns, each owning techniques
(carrying a tactic) and attributed actors, with the ids prefixed per version so
leakage is detectable. A valid STIX bundle is assembled from the world
(campaign / attack-pattern / intrusion-set objects plus ``uses`` and
``attributed-to`` relationships), written to an isolated ``data_dir`` as
``enterprise-attack-{version}.json`` and persisted via
:class:`StixPersistenceService` fed by a :class:`StixService` with a ``None``
``http_client`` (no network). Both bundles are persisted into the SAME ephemeral
SQLite database (the same fixture pattern as
``test_stix_read_property_campaign_isolation.py``). Nothing here touches the
network nor an AI client.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
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
)
from app.services.stix.stix_package_read_service import StixPackageReadService
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import StixService


# ---------------------------------------------------------------------------
# Ephemeral DB fixture: a SQLite engine on a temporary file with the full schema
# via Base.metadata.create_all (same pattern as
# test_stix_read_property_campaign_isolation.py).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``."""
    tmp_dir = tempfile.mkdtemp(prefix="stix_read_analytics_db_")
    db_path = Path(tmp_dir) / "analytics.db"
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
# techniques (with a tactic) and attributed actors.
# ---------------------------------------------------------------------------

# Two distinct KNOWN_VERSIONS so both bundles are realistic versions persisted
# into the same database under two different packages.
VERSION_A = "18.0"
VERSION_B = "17.1"


def _stix_id(prefix: str) -> str:
    """A syntactically valid STIX id ``{prefix}--{uuid4}``."""
    return f"{prefix}--{uuid.uuid4()}"


@st.composite
def _world(draw, version_tag: str) -> dict:
    """Draw a per-version world: campaigns owning techniques and actors.

    Returns a dict with a single ``bundle`` key — a valid STIX bundle (``dict``
    with an ``"objects"`` list) carrying the campaigns, their
    attack-patterns/intrusion-sets and the ``uses`` / ``attributed-to``
    relationships. Technique external ids, tactic names and actor ids are
    prefixed with ``version_tag`` so any cross-version leakage is detectable.

    An actor may be attributed to more than one campaign of the same version
    (drawn from the already-created actors), so the ``campaigns_total`` ranking
    is exercised with values greater than one. At least one campaign owns at
    least one technique and one actor so every ranking/distribution is
    non-trivially exercised.
    """
    objects: list[dict] = []
    campaign_ids: list[str] = []
    actor_ids: list[str] = []

    n_campaigns = draw(st.integers(min_value=1, max_value=3))
    for c in range(n_campaigns):
        campaign_stix_id = _stix_id("campaign")
        campaign_ids.append(campaign_stix_id)
        objects.append(
            {
                "type": "campaign",
                "id": campaign_stix_id,
                "name": f"Campaign {version_tag}-{c}",
            }
        )

        # Techniques owned by THIS campaign, each carrying a per-version tactic
        # so the tactics distribution is detectable and cross-version-safe.
        n_techniques = draw(st.integers(min_value=0, max_value=3))
        for t in range(n_techniques):
            technique_id = f"T{version_tag}{c}{t}"
            tactic_name = f"tactic-{version_tag}-{draw(st.integers(0, 2))}"
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
                        {
                            "kill_chain_name": "mitre-attack",
                            "phase_name": tactic_name,
                        }
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

        # Actors attributed to THIS campaign. Some are new intrusion-sets; a new
        # attribution may also reuse an actor already created in this version so
        # one actor can accumulate several campaigns.
        n_actors = draw(st.integers(min_value=0, max_value=3))
        for a in range(n_actors):
            reuse_existing = bool(actor_ids) and draw(st.booleans())
            if reuse_existing:
                actor_stix_id = draw(st.sampled_from(actor_ids))
            else:
                actor_stix_id = (
                    f"intrusion-set--{version_tag}-{c}-{a}-{uuid.uuid4()}"
                )
                actor_ids.append(actor_stix_id)
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

    # Guarantee the first campaign owns at least one technique and one actor so
    # the rankings/distribution are exercised non-trivially on every example.
    first_campaign_id = campaign_ids[0]
    if not any(
        o.get("relationship_type") == "uses"
        and o.get("source_ref") == first_campaign_id
        for o in objects
    ):
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
                    {
                        "kill_chain_name": "mitre-attack",
                        "phase_name": f"tactic-{version_tag}-seed",
                    }
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
    if not any(
        o.get("relationship_type") == "attributed-to"
        and o.get("source_ref") == first_campaign_id
        for o in objects
    ):
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

    bundle = {
        "type": "bundle",
        "id": _stix_id("bundle"),
        "spec_version": "2.1",
        "objects": objects,
    }
    return {"bundle": bundle}


# ---------------------------------------------------------------------------
# Row-derived oracle: aggregations computed directly over the persisted rows of
# a package (scoped by package_id), independent of package_analytics.
# ---------------------------------------------------------------------------


def _resolve_package_id(session: Session, version: str) -> int:
    """Resolve the ``stix_packages`` id for ``version``."""
    return session.execute(
        select(StixPackage.id).where(StixPackage.version == version)
    ).scalar_one()


def _oracle_curated_totals(session: Session, package_id: int) -> tuple[int, int]:
    """(curated_total, uncurated_total) counted over ``stix_campaigns`` rows."""
    rows = session.execute(
        select(StixCampaign.curated, func.count())
        .where(StixCampaign.package_id == package_id)
        .group_by(StixCampaign.curated)
    ).all()
    curated_total = 0
    uncurated_total = 0
    for is_curated, total in rows:
        if is_curated:
            curated_total = total
        else:
            uncurated_total = total
    return curated_total, uncurated_total


def _oracle_actor_totals(session: Session, package_id: int) -> dict[str, int]:
    """{actor stix_id: number of attributions} over ``stix_actor_campaigns``.

    Only actors with at least one attribution appear — matching the INNER JOIN
    the service uses for the ranking.
    """
    rows = session.execute(
        select(StixActor.stix_id, func.count(StixActorCampaign.id))
        .join(StixActorCampaign, StixActorCampaign.actor_id == StixActor.id)
        .where(StixActor.package_id == package_id)
        .group_by(StixActor.id, StixActor.stix_id)
    ).all()
    return {stix_id: total for stix_id, total in rows}


def _oracle_campaign_totals(session: Session, package_id: int) -> dict[str, int]:
    """{campaign_key: number of techniques} over ``stix_campaign_techniques``.

    ``campaign_key`` is ``curated_slug`` when present, else ``stix_id`` (the same
    key the service exposes — Req. 12.5). Only campaigns with at least one
    technique appear — matching the service's INNER JOIN.
    """
    rows = session.execute(
        select(
            StixCampaign.stix_id,
            StixCampaign.curated_slug,
            func.count(StixCampaignTechnique.id),
        )
        .join(
            StixCampaignTechnique,
            StixCampaignTechnique.campaign_id == StixCampaign.id,
        )
        .where(StixCampaign.package_id == package_id)
        .group_by(StixCampaign.id, StixCampaign.stix_id, StixCampaign.curated_slug)
    ).all()
    return {
        (curated_slug or stix_id): total
        for stix_id, curated_slug, total in rows
    }


def _oracle_tactic_totals(session: Session, package_id: int) -> dict[str, int]:
    """{tactic: number of techniques} over non-null ``tactic`` rows (Req. 12.4)."""
    rows = session.execute(
        select(StixCampaignTechnique.tactic, func.count(StixCampaignTechnique.id))
        .join(StixCampaign, StixCampaign.id == StixCampaignTechnique.campaign_id)
        .where(
            StixCampaign.package_id == package_id,
            StixCampaignTechnique.tactic.is_not(None),
        )
        .group_by(StixCampaignTechnique.tactic)
    ).all()
    return {tactic: total for tactic, total in rows}


def _oracle_technique_totals(session: Session, package_id: int) -> dict[str, int]:
    """{technique_id: number of DISTINCT campaigns using it} (inverse of campaigns).

    Counts distinct ``campaign_id`` per ``technique_id`` over
    ``stix_campaign_techniques`` scoped to the package — the oracle for the
    ``top_techniques`` ranking (campaigns per technique).
    """
    rows = session.execute(
        select(
            StixCampaignTechnique.technique_id,
            func.count(func.distinct(StixCampaignTechnique.campaign_id)),
        )
        .join(StixCampaign, StixCampaign.id == StixCampaignTechnique.campaign_id)
        .where(StixCampaign.package_id == package_id)
        .group_by(StixCampaignTechnique.technique_id)
    ).all()
    return {technique_id: total for technique_id, total in rows}


def _assert_non_increasing(totals: list[int]) -> None:
    """Assert a ranking's totals are sorted non-increasingly (a valid order)."""
    assert all(
        totals[i] >= totals[i + 1] for i in range(len(totals) - 1)
    ), f"ranking is not sorted by total descending: {totals}"


def _assert_valid_top_n(
    service_totals: list[int], oracle_map: dict[str, int], top_n: int
) -> None:
    """Assert the service returned a valid top-N cut of the oracle totals.

    ``oracle_map`` is the ``{key: total}`` map of every ranked entity. The
    service must return exactly ``min(top_n, number of oracle entries)`` entries,
    and the multiset of returned totals must equal the ``top_n`` largest totals
    of the oracle (ties resolved by value, since the ranking has no defined
    tie-break).
    """
    expected_len = min(top_n, len(oracle_map))
    assert len(service_totals) == expected_len
    all_oracle_totals = sorted(oracle_map.values(), reverse=True)
    expected_top = all_oracle_totals[:top_n]
    assert sorted(service_totals, reverse=True) == expected_top


# ===========================================================================
# Property 6 — analytics faithfully reflects the persisted tables.
# ===========================================================================


# Feature: stix-package-persistence-and-overview, Property 6: Analytics faithfully
# reflects the persisted tables.
@settings(
    max_examples=40,
    # Each example does disk I/O (ephemeral SQLite + two bundle files on disk),
    # whose cost varies with the environment; the default ``deadline`` (200ms)
    # is therefore a source of *timing* flakiness, not of correctness.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    world_a=_world(VERSION_A),
    world_b=_world(VERSION_B),
)
def test_property6_analytics_matches_row_oracle_without_cross_version(
    world_a: dict,
    world_b: dict,
) -> None:
    """Analytics equals the row-derived oracle and never crosses versions.

    Two distinct versions, each with its own campaigns/techniques/actors, are
    persisted into the SAME database. For version A, ``package_analytics`` must
    reproduce the aggregations computed directly over version A's rows — totals,
    actor ranking, campaign ranking and tactic distribution — and must carry
    none of version B's actors/campaigns/tactics (ids are prefixed per version,
    so any overlap would be a leak).

    Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.6
    """
    data_dir = Path(tempfile.mkdtemp(prefix="stix_read_analytics_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        stix_service = StixService(data_dir=data_dir, http_client=None)
        persistence = StixPersistenceService(
            session=session, stix_service=stix_service
        )

        # Persist BOTH versions into the SAME database.
        for version, world in ((VERSION_A, world_a), (VERSION_B, world_b)):
            bundle_path = data_dir / f"enterprise-attack-{version}.json"
            bundle_path.write_text(json.dumps(world["bundle"]), encoding="utf-8")
            result = persistence.persist_from_disk(version)
            assert result.persisted is True

        read_service = StixPackageReadService(session)

        package_id_a = _resolve_package_id(session, VERSION_A)
        package_id_b = _resolve_package_id(session, VERSION_B)

        # Row-derived oracle for version A (independent of package_analytics).
        oracle_curated, oracle_uncurated = _oracle_curated_totals(
            session, package_id_a
        )
        oracle_actor_totals = _oracle_actor_totals(session, package_id_a)
        oracle_campaign_totals = _oracle_campaign_totals(session, package_id_a)
        oracle_tactic_totals = _oracle_tactic_totals(session, package_id_a)
        oracle_technique_totals = _oracle_technique_totals(session, package_id_a)

        # Version B's keys, to assert NO cross-version leakage in A's analytics.
        b_actor_keys = set(_oracle_actor_totals(session, package_id_b))
        b_campaign_keys = set(_oracle_campaign_totals(session, package_id_b))
        b_tactic_keys = set(_oracle_tactic_totals(session, package_id_b))
        b_technique_keys = set(_oracle_technique_totals(session, package_id_b))

        # --- Analytics with a top_n large enough to capture every ranked row so
        # truncation cannot hide a mismatch against the oracle. ---
        big_top_n = 1000
        analytics = read_service.package_analytics(VERSION_A, top_n=big_top_n)

        assert analytics.version == VERSION_A

        # curated_total / uncurated_total match the grouped campaign counts.
        assert analytics.curated_total == oracle_curated
        assert analytics.uncurated_total == oracle_uncurated

        # top_actors: per-actor campaigns_total map equals the oracle, the order
        # is non-increasing, and no version-B actor appears.
        service_actor_map = {
            rank.stix_id: rank.campaigns_total for rank in analytics.top_actors
        }
        assert service_actor_map == oracle_actor_totals
        _assert_non_increasing([rank.campaigns_total for rank in analytics.top_actors])
        assert set(service_actor_map).isdisjoint(b_actor_keys)

        # top_campaigns: per-campaign techniques_total map equals the oracle, the
        # order is non-increasing, and no version-B campaign appears.
        service_campaign_map = {
            rank.campaign_key: rank.techniques_total
            for rank in analytics.top_campaigns
        }
        assert service_campaign_map == oracle_campaign_totals
        _assert_non_increasing(
            [rank.techniques_total for rank in analytics.top_campaigns]
        )
        assert set(service_campaign_map).isdisjoint(b_campaign_keys)

        # tactics: per-tactic techniques_total map equals the oracle, the order
        # is non-increasing, and no version-B tactic appears.
        service_tactic_map = {
            bucket.tactic: bucket.techniques_total for bucket in analytics.tactics
        }
        assert service_tactic_map == oracle_tactic_totals
        _assert_non_increasing(
            [bucket.techniques_total for bucket in analytics.tactics]
        )
        assert set(service_tactic_map).isdisjoint(b_tactic_keys)

        # top_techniques: per-technique campaigns_total map equals the oracle
        # (distinct campaigns per technique), the order is non-increasing, and no
        # version-B technique appears.
        service_technique_map = {
            rank.technique_id: rank.campaigns_total
            for rank in analytics.top_techniques
        }
        assert service_technique_map == oracle_technique_totals
        _assert_non_increasing(
            [rank.campaigns_total for rank in analytics.top_techniques]
        )
        assert set(service_technique_map).isdisjoint(b_technique_keys)

        # --- top_n truncation: with a small cut the service must return a valid
        # top-N of the oracle totals (exact count + the N largest totals). ---
        small_top_n = 2
        cut = read_service.package_analytics(VERSION_A, top_n=small_top_n)
        _assert_valid_top_n(
            [rank.campaigns_total for rank in cut.top_actors],
            oracle_actor_totals,
            small_top_n,
        )
        _assert_valid_top_n(
            [rank.techniques_total for rank in cut.top_campaigns],
            oracle_campaign_totals,
            small_top_n,
        )
        _assert_valid_top_n(
            [rank.campaigns_total for rank in cut.top_techniques],
            oracle_technique_totals,
            small_top_n,
        )
    finally:
        session.close()
        factory.kw["bind"].dispose()
        for base in (data_dir, Path(db_tmp_dir)):
            for root, _dirs, files in os.walk(base, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                os.rmdir(root)
