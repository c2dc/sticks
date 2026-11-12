"""Example-based unit tests for :class:`StixPersistenceService`.

Concrete, hand-written examples that complement the Hypothesis-driven property
tests in ``test_stix_persistence_property_*.py``. They pin the behavior against
ONE readable in-memory bundle so the expected row counts and spot-checked field
values are explicit.

Scenarios
---------
1. **Persist creates rows** — after :meth:`persist_bundle`, every one of the six
   STIX tables has the expected number of rows and the spot-checked values
   (curated flag + slug, external id, technique id, actor attribution) are
   correct (Req. 1.4).
2. **Re-persist does not duplicate** — persisting twice keeps the row counts,
   reuses the same ``stix_packages`` id and refreshes ``downloaded_at``
   (Req. 2.1, 2.2).
3. **Simulated failure leaves no rows** — when the commit fails, the service
   rolls back and no ``stix_packages`` row survives (Req. 9.2).

Isolation: the bundle lives in memory only (no file, no network); the write runs
against an ephemeral SQLite engine with the full schema created via
``Base.metadata.create_all``.

_Requirements: 1.4, 2.1, 2.2, 9.2_
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
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
    StixTypeCount,
)
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import KNOWN_VERSIONS, StixService


# A known version, as in production. ``persist_bundle`` never downloads, so
# there is no network egress.
_VERSION = KNOWN_VERSIONS[0]

# Stable STIX ids for the concrete bundle, so the assertions can reference them
# directly.
_CAMPAIGN_CURATED_ID = "campaign--00000000-0000-0000-0000-000000000001"
_CAMPAIGN_PLAIN_ID = "campaign--00000000-0000-0000-0000-000000000002"
_PATTERN_A_ID = "attack-pattern--00000000-0000-0000-0000-00000000000a"
_PATTERN_B_ID = "attack-pattern--00000000-0000-0000-0000-00000000000b"
_ACTOR_ID = "intrusion-set--00000000-0000-0000-0000-0000000000f1"


def _example_bundle() -> dict:
    """Build ONE concrete, readable STIX bundle exercising every table.

    The bundle carries, by construction:

    * **Two campaigns** — a curated one (``external_id`` ``"C0045"`` resolves the
      ``shadowray`` slug) and a plain one (a non-curated ``external_id``), so both
      the curated and the non-curated branch of ``stix_campaigns.curated`` are
      populated.
    * **Two attack-patterns** (techniques ``T1059`` and ``T1021``), each with a
      tactic phase, so ``stix_campaign_techniques`` gets rows.
    * **One intrusion-set** (the actor), so ``stix_actors`` gets a row.
    * **Relationships** — two ``uses`` edges from the curated campaign to the two
      techniques, and one ``attributed-to`` edge from the curated campaign to the
      intrusion-set, so ``stix_campaign_techniques`` and ``stix_actor_campaigns``
      both get rows.

    This makes the expected row counts deterministic and the spot-checked field
    values explicit.
    """
    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-0000-0000-000000000000",
        "spec_version": "2.1",
        "objects": [
            # --- curated campaign (C0045 -> shadowray) ----------------------
            {
                "type": "campaign",
                "id": _CAMPAIGN_CURATED_ID,
                "name": "ShadowRay",
                "description": "A curated campaign for the unit test.",
                "aliases": ["Shadow", "Ray"],
                "first_seen": "2023-01-01T00:00:00.000Z",
                "last_seen": "2023-06-01T00:00:00.000Z",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "C0045"}
                ],
            },
            # --- plain campaign (non-curated external id) -------------------
            {
                "type": "campaign",
                "id": _CAMPAIGN_PLAIN_ID,
                "name": "Plain Campaign",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "C9001"}
                ],
            },
            # --- techniques (attack-patterns) -------------------------------
            {
                "type": "attack-pattern",
                "id": _PATTERN_A_ID,
                "name": "Command and Scripting Interpreter",
                "description": "Technique A description.",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "T1059"}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
                ],
            },
            {
                "type": "attack-pattern",
                "id": _PATTERN_B_ID,
                "name": "Remote Services",
                "description": "Technique B description.",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "T1021"}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": "lateral-movement"}
                ],
            },
            # --- one tactic and one malware (so those type counts are > 0) --
            {
                "type": "x-mitre-tactic",
                "id": "x-mitre-tactic--00000000-0000-0000-0000-0000000000c1",
                "name": "Execution",
            },
            {
                "type": "malware",
                "id": "malware--00000000-0000-0000-0000-0000000000d1",
                "name": "ExampleMalware",
            },
            # --- actor (intrusion-set) --------------------------------------
            {
                "type": "intrusion-set",
                "id": _ACTOR_ID,
                "name": "APT-Example",
            },
            # --- relationships: curated campaign uses both techniques -------
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000011",
                "relationship_type": "uses",
                "source_ref": _CAMPAIGN_CURATED_ID,
                "target_ref": _PATTERN_A_ID,
            },
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000012",
                "relationship_type": "uses",
                "source_ref": _CAMPAIGN_CURATED_ID,
                "target_ref": _PATTERN_B_ID,
            },
            # --- relationship: curated campaign attributed to the actor -----
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000013",
                "relationship_type": "attributed-to",
                "source_ref": _CAMPAIGN_CURATED_ID,
                "target_ref": _ACTOR_ID,
            },
        ],
    }


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``.

    A real temporary-file engine (not ``:memory:``) is used so ``create_all``
    runs against a file engine, mirroring production and matching the sibling
    persistence tests.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stix_persistence_unit_db_")
    db_path = Path(tmp_dir) / "unit.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
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


def _cleanup(*bases: Path) -> None:
    """Remove the temporary directories created for a test."""
    for base in bases:
        if not base.exists():
            continue
        for root, _dirs, files in os.walk(base, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)


def _count(session: Session, model) -> int:
    """Return the total number of rows of ``model`` in the database."""
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_persist_bundle_creates_rows_in_every_table() -> None:
    """``persist_bundle`` populates all six tables with the expected rows.

    Spot-checks the curated flag/slug, the external id, a persisted technique id
    and the actor attribution against the concrete bundle. Satisfies the single
    transactional write that populates every table (Req. 1.4).
    """
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        bundle = _example_bundle()
        stix_service = StixService()
        service = StixPersistenceService(session=session, stix_service=stix_service)

        result = service.persist_bundle(_VERSION, bundle)
        assert result.persisted is True
        assert result.campaigns_total == 2
        assert result.actors_total == 1

        # --- expected row counts per table ---------------------------------
        # One package row for the version.
        assert _count(session, StixPackage) == 1
        # dashboard always emits the six required type counts in a stable order.
        assert _count(session, StixTypeCount) == 6
        # Two campaigns (curated + plain).
        assert _count(session, StixCampaign) == 2
        # The curated campaign uses both techniques (T1059, T1021); the plain one
        # uses none.
        assert _count(session, StixCampaignTechnique) == 2
        # One intrusion-set (the synthetic "no actor" group is not persisted).
        assert _count(session, StixActor) == 1
        # One actor->campaign attribution (the curated campaign).
        assert _count(session, StixActorCampaign) == 1

        # --- spot-check field values ---------------------------------------
        curated = session.execute(
            select(StixCampaign).where(StixCampaign.stix_id == _CAMPAIGN_CURATED_ID)
        ).scalar_one()
        assert curated.curated is True
        assert curated.curated_slug == "shadowray"
        assert curated.external_id == "C0045"
        assert curated.aliases == ["Shadow", "Ray"]

        plain = session.execute(
            select(StixCampaign).where(StixCampaign.stix_id == _CAMPAIGN_PLAIN_ID)
        ).scalar_one()
        assert plain.curated is False
        assert plain.curated_slug is None
        assert plain.external_id == "C9001"

        # The persisted techniques belong to the curated campaign and carry the
        # expected technique ids.
        technique_ids = {
            technique_id
            for (technique_id,) in session.execute(
                select(StixCampaignTechnique.technique_id).where(
                    StixCampaignTechnique.campaign_id == curated.id
                )
            ).all()
        }
        assert technique_ids == {"T1059", "T1021"}

        # The actor attribution links the actor to the curated campaign.
        actor = session.execute(
            select(StixActor).where(StixActor.stix_id == _ACTOR_ID)
        ).scalar_one()
        assert actor.name == "APT-Example"
        attribution = session.execute(
            select(StixActorCampaign).where(StixActorCampaign.actor_id == actor.id)
        ).scalar_one()
        assert attribution.campaign_id == curated.id
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _cleanup(Path(db_tmp_dir))


def test_persist_bundle_is_idempotent_no_duplicate_rows() -> None:
    """Re-persisting the same version keeps counts, reuses the id, refreshes time.

    Calling ``persist_bundle`` twice must not duplicate any rows, must reuse
    the same ``stix_packages`` id (upsert by version) and must refresh
    ``downloaded_at`` (Req. 2.1, 2.2).
    """
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        bundle = _example_bundle()
        stix_service = StixService()
        service = StixPersistenceService(session=session, stix_service=stix_service)

        # First persistence.
        service.persist_bundle(_VERSION, bundle)
        first = session.execute(
            select(StixPackage).where(StixPackage.version == _VERSION)
        ).scalar_one()
        first_id = first.id
        first_downloaded_at = first.downloaded_at
        counts_after_first = {
            model: _count(session, model)
            for model in (
                StixPackage,
                StixTypeCount,
                StixCampaign,
                StixCampaignTechnique,
                StixActor,
                StixActorCampaign,
            )
        }

        # Second persistence of the same bundle/version.
        service.persist_bundle(_VERSION, bundle)
        session.expire_all()
        second = session.execute(
            select(StixPackage).where(StixPackage.version == _VERSION)
        ).scalar_one()
        counts_after_second = {
            model: _count(session, model)
            for model in (
                StixPackage,
                StixTypeCount,
                StixCampaign,
                StixCampaignTechnique,
                StixActor,
                StixActorCampaign,
            )
        }

        # No duplication: row counts are unchanged across the two persistences.
        assert counts_after_second == counts_after_first
        # The stix_packages row is reused (same id), not recreated.
        assert second.id == first_id
        # downloaded_at is refreshed (not earlier than the first persistence).
        assert second.downloaded_at >= first_downloaded_at
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _cleanup(Path(db_tmp_dir))


def test_persist_bundle_rolls_back_on_commit_failure_leaving_no_rows() -> None:
    """A commit failure rolls back, leaving no package row.

    When the DB write fails after the bundle is available, the service rolls the
    transaction back — no ``stix_packages`` row survives.
    (Req. 9.2). A fresh database is used for this case.
    """
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        bundle = _example_bundle()
        stix_service = StixService()
        service = StixPersistenceService(session=session, stix_service=stix_service)

        # Force the single commit to fail so the write cannot be persisted. The
        # service wraps the write phase and must roll back on any failure.
        def _boom() -> None:
            raise RuntimeError("simulated commit failure")

        session.commit = _boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="simulated commit failure"):
            service.persist_bundle(_VERSION, bundle)

        # Restore a working commit so the verification queries can run cleanly.
        del session.commit  # type: ignore[attr-defined]
        session.rollback()

        # No partial package survives the failed write (Req. 9.2).
        assert _count(session, StixPackage) == 0
        assert _count(session, StixTypeCount) == 0
        assert _count(session, StixCampaign) == 0
        assert _count(session, StixCampaignTechnique) == 0
        assert _count(session, StixActor) == 0
        assert _count(session, StixActorCampaign) == 0
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _cleanup(Path(db_tmp_dir))
