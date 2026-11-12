"""Property test for the persistence round-trip of a STIX package (task 2.5).

# Feature: stix-package-persistence-and-overview, Property 1: Persistence
# round-trip preserves type counts and campaigns.

Validates: Requirements 1.3, 3.2, 4.1

This is a *feature* property test (the ``stix-package-persistence-and-overview``
spec is a feature, not a bugfix): the property is expected to **hold** against
the current implementation of
:meth:`~app.services.stix.stix_persistence_service.StixPersistenceService.persist_bundle`.

Property (design.md — Property 1)
---------------------------------
*For every* valid in-memory STIX bundle, ``persist_bundle(version)``
followed by a read-back from the database reproduces, as a **reference model**,
the output of the current parser (:class:`StixService`):

1. The per-type counts persisted into ``stix_type_counts`` equal, type by type,
   the counts the parser derives in :meth:`StixService.dashboard` (same
   ``stix_type`` -> same ``count``). This is the projection Requirement 3.2
   serves from the database.
2. The set of campaign ``stix_id`` persisted into ``stix_campaigns`` equals the
   set the parser derives in :meth:`StixService.list_campaigns`. This is the
   content Requirement 4.1 serves from the database. The persistence is the
   single writer (Requirement 1.3).

The parser is the reference model on purpose: persistence must never invent nor
drop content relative to the parse it is a projection of.

Strategy / isolation
--------------------
Each example generates a **valid** STIX bundle (a ``dict`` with an ``"objects"``
list) via Hypothesis: campaign objects (``campaign--<uuid>`` ids, names, optional
``external_references`` / ``aliases``), plus ``attack-pattern``,
``x-mitre-tactic``, ``intrusion-set``, ``malware`` objects and ``relationship``
objects (``uses`` from a campaign to an attack-pattern and ``attributed-to``
from a campaign to an intrusion-set). The bundle is written to an isolated
``data_dir`` as ``enterprise-attack-{version}.json`` and a :class:`StixService`
is built pointing at that dir with a ``None`` ``http_client`` (no network — the
backfill path never downloads).

The persistence runs against an **ephemeral** SQLite ``Session`` (engine on a
temporary file with the full schema via ``Base.metadata.create_all``, the same
fixture pattern as ``test_backfill_idempotent_property.py`` /
``test_flow_models.py``). Nothing here touches the network nor an AI client.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StixCampaign, StixPackage, StixTypeCount
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import StixService


# ---------------------------------------------------------------------------
# Ephemeral DB fixture: a SQLite engine on a temporary file with the full schema
# via Base.metadata.create_all, so the persistence runs against a real,
# disposable database (same pattern as test_backfill_idempotent_property.py).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``.

    The file (not ``:memory:``) is used so ``create_all`` runs against a real
    file engine, mirroring production and matching the sibling fixtures.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stix_persistence_roundtrip_db_")
    db_path = Path(tmp_dir) / "roundtrip.db"
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
# Hypothesis strategy — a valid STIX bundle with campaigns, attack-patterns,
# tactics, intrusion-sets, malware and uses/attributed-to relationships.
# ---------------------------------------------------------------------------


def _stix_id(prefix: str) -> str:
    """A syntactically valid STIX id ``{prefix}--{uuid4}``."""
    return f"{prefix}--{uuid.uuid4()}"


@st.composite
def _valid_bundle(draw) -> dict:
    """Draw a valid STIX bundle (a ``dict`` with an ``"objects"`` list).

    Generates the six counted object types plus relationships so the dashboard
    counts and the campaign list are both non-trivial:

    * ``campaign`` objects with ``campaign--<uuid>`` ids, a ``name`` and optional
      ``external_references`` (an ``external_id`` such as ``"C0010"``) and
      ``aliases``;
    * ``attack-pattern``, ``x-mitre-tactic``, ``intrusion-set`` and ``malware``
      objects;
    * ``relationship`` objects: some ``uses`` from a campaign to an
      attack-pattern and some ``attributed-to`` from a campaign to an
      intrusion-set.

    The bundle is structurally valid (an object carrying an ``"objects"`` list),
    so :meth:`StixService.read_bundle` accepts it and the dashboard / campaign
    derivations run normally.
    """
    objects: list[dict] = []

    # --- campaigns (1..5), each with an id/name and optional refs/aliases ----
    n_campaigns = draw(st.integers(min_value=1, max_value=5))
    campaign_ids: list[str] = []
    for i in range(n_campaigns):
        cid = _stix_id("campaign")
        campaign_ids.append(cid)
        obj: dict = {"type": "campaign", "id": cid, "name": f"Campaign {i}"}
        if draw(st.booleans()):
            code = f"C{draw(st.integers(min_value=0, max_value=9999)):04d}"
            obj["external_references"] = [
                {"source_name": "mitre-attack", "external_id": code}
            ]
        if draw(st.booleans()):
            obj["aliases"] = draw(
                st.lists(
                    st.text(
                        alphabet=st.characters(
                            min_codepoint=65, max_codepoint=90
                        ),
                        min_size=1,
                        max_size=6,
                    ),
                    min_size=1,
                    max_size=3,
                )
            )
        objects.append(obj)

    # --- attack-patterns (0..5), each with an external_id and a tactic phase --
    n_patterns = draw(st.integers(min_value=0, max_value=5))
    pattern_ids: list[str] = []
    for i in range(n_patterns):
        pid = _stix_id("attack-pattern")
        pattern_ids.append(pid)
        objects.append(
            {
                "type": "attack-pattern",
                "id": pid,
                "name": f"Technique {i}",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": f"T{1000 + i}"}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": f"tactic-{i}"}
                ],
            }
        )

    # --- x-mitre-tactic (0..3) ----------------------------------------------
    for i in range(draw(st.integers(min_value=0, max_value=3))):
        objects.append(
            {"type": "x-mitre-tactic", "id": _stix_id("x-mitre-tactic"), "name": f"Tactic {i}"}
        )

    # --- intrusion-sets (0..4) ----------------------------------------------
    n_actors = draw(st.integers(min_value=0, max_value=4))
    actor_ids: list[str] = []
    for i in range(n_actors):
        aid = _stix_id("intrusion-set")
        actor_ids.append(aid)
        objects.append({"type": "intrusion-set", "id": aid, "name": f"Actor {i}"})

    # --- malware (0..3) ------------------------------------------------------
    for i in range(draw(st.integers(min_value=0, max_value=3))):
        objects.append(
            {"type": "malware", "id": _stix_id("malware"), "name": f"Malware {i}"}
        )

    # --- relationships: uses (campaign -> attack-pattern) --------------------
    if pattern_ids:
        for _ in range(draw(st.integers(min_value=0, max_value=6))):
            objects.append(
                {
                    "type": "relationship",
                    "id": _stix_id("relationship"),
                    "relationship_type": "uses",
                    "source_ref": draw(st.sampled_from(campaign_ids)),
                    "target_ref": draw(st.sampled_from(pattern_ids)),
                }
            )

    # --- relationships: attributed-to (campaign -> intrusion-set) ------------
    if actor_ids:
        for _ in range(draw(st.integers(min_value=0, max_value=6))):
            objects.append(
                {
                    "type": "relationship",
                    "id": _stix_id("relationship"),
                    "relationship_type": "attributed-to",
                    "source_ref": draw(st.sampled_from(campaign_ids)),
                    "target_ref": draw(st.sampled_from(actor_ids)),
                }
            )

    return {
        "type": "bundle",
        "id": _stix_id("bundle"),
        "spec_version": "2.1",
        "objects": objects,
    }


# ===========================================================================
# Property 1 — persistence round-trip preserves counts and campaigns.
# ===========================================================================


# Feature: stix-package-persistence-and-overview, Property 1: Persistence
# round-trip preserves type counts and campaigns.
@settings(
    max_examples=40,
    # Each example does disk I/O (ephemeral SQLite + a bundle file on disk),
    # whose cost varies with the environment; the default ``deadline`` (200ms)
    # is therefore a source of *timing* flakiness, not of correctness. The
    # property is still exercised on every example; we only disable the clock.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(bundle=_valid_bundle())
def test_property1_persistence_roundtrip_preserves_counts_and_campaigns(
    bundle: dict,
) -> None:
    """``persist_bundle`` + DB read reproduces the parser's counts/campaigns.

    For an arbitrary valid in-memory bundle, after ``persist_bundle(version)``:

    1. the per-type counts in ``stix_type_counts`` equal, type by type, the
       counts :meth:`StixService.dashboard` derives (Req. 3.2); and
    2. the set of campaign ``stix_id`` in ``stix_campaigns`` equals the set
       :meth:`StixService.list_campaigns` derives (Req. 4.1),

    with the persistence being the single writer (Req. 1.3). The parser is the
    reference model.

    Validates: Requirements 1.3, 3.2, 4.1
    """
    version = "18.0"
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        # The bundle lives in memory only; no network is involved.
        stix_service = StixService()
        persistence = StixPersistenceService(
            session=session, stix_service=stix_service
        )

        # --- reference model: the parser itself -----------------------------
        expected_counts = {
            type_count.stix_type: type_count.count
            for type_count in stix_service.dashboard(version, bundle).counts
        }
        expected_campaign_ids = {
            campaign.stix_id for campaign in stix_service.list_campaigns(bundle)
        }

        # --- act: persist the in-memory bundle (single writer, no download) ----
        result = persistence.persist_bundle(version, bundle)
        assert result.persisted is True

        # --- read back from the DB ------------------------------------------
        package = session.execute(
            select(StixPackage).where(StixPackage.version == version)
        ).scalar_one()

        persisted_counts = {
            stix_type: count
            for (stix_type, count) in session.execute(
                select(StixTypeCount.stix_type, StixTypeCount.count).where(
                    StixTypeCount.package_id == package.id
                )
            ).all()
        }
        persisted_campaign_ids = {
            stix_id
            for (stix_id,) in session.execute(
                select(StixCampaign.stix_id).where(
                    StixCampaign.package_id == package.id
                )
            ).all()
        }

        # 1) Per-type counts match the parser type by type (Req. 3.2).
        assert persisted_counts == expected_counts

        # 2) The set of campaign stix_ids matches the parser (Req. 4.1). A valid
        #    bundle always carries at least one campaign, so the set is non-empty.
        assert persisted_campaign_ids == expected_campaign_ids
    finally:
        session.close()
        factory.kw["bind"].dispose()
        # Clean up the temporary database directory.
        for base in (Path(db_tmp_dir),):
            for root, _dirs, files in os.walk(base, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                os.rmdir(root)
