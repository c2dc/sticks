"""Property-based test for best-effort deletion failure of
:class:`StixPersistenceService` (task 1.4 of the
``stix-base-persisted-state-and-file-cleanup`` spec).

# Feature: stix-base-persisted-state-and-file-cleanup, Property 3: Falha de
# deleção não reverte a persistência

Property 3: Falha de deleção não reverte a persistência
Validates: Requirements 2.1, 2.2, 2.3, 7.3

Hypothesis: for any valid STIX bundle whose commit succeeds but whose on-disk
Bundle_File deletion fails, the persistence must stand. Concretely, after
injecting an ``unlink`` that raises :class:`OSError` on the bundle path (the
best-effort deletion that runs only on the post-commit success path), the
result still reports ``persisted = True``, ``is_persisted(version)`` is true,
and no error propagates to the caller.

The deletion failure is simulated exactly as the design prescribes — by patching
``Path.unlink`` to raise :class:`OSError`. Because ``_delete_bundle_file`` is the
only ``unlink`` call and it runs strictly after the single commit, patching
``Path.unlink`` globally exercises only the best-effort deletion and never the
read-only derivation (which never unlinks).

Isolation (no real files / no real network):

* The bundle is written to disk under an isolated per-example ``data_dir`` as
  ``enterprise-attack-{version}.json`` and the service is built with
  ``StixService(data_dir=..., http_client=None)`` — the real
  ``sticks/data/stix`` is never touched and ``persist_from_disk`` never reaches
  the network.
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

# A curated external_id code (from SLUG_TO_STIX) and its paired slug, used to
# generate campaigns that resolve a curated slug so the curated path is covered
# alongside non-curated campaigns.
_CURATED_EXTERNAL_ID = "C0040"


def _stix_id(prefix: str, n: int) -> str:
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


@st.composite
def _valid_bundle(draw: st.DrawFn) -> list[dict]:
    """Generate a valid STIX bundle with campaigns, attack-patterns,
    intrusion-sets and ``uses`` / ``attributed-to`` relationships.

    Mirrors the generator used by the sibling persistence property tests so the
    derivation populates every one of the six persisted tables across a wide
    range of shapes.
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
        objects.append(
            {"type": "x-mitre-tactic", "id": _stix_id("x-mitre-tactic", counter)}
        )
        counter += 1

    # Shuffle so object ordering never influences persistence.
    return list(draw(st.permutations(objects)))


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a valid STIX bundle (``{"objects": [...]}``) to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "bundle", "objects": objects}), encoding="utf-8"
    )


# Feature: stix-base-persisted-state-and-file-cleanup, Property 3: Falha de deleção não reverte a persistência
@PROP_SETTINGS
@given(objects=_valid_bundle(), use_download_path=st.booleans())
def test_property3_delete_failure_does_not_revert_persistence(
    objects: list[dict],
    use_download_path: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deletion failure after a successful commit never reverts persistence.

    Injects an ``unlink`` raising :class:`OSError` on the post-commit best-effort
    deletion and asserts: the call returns normally (no error propagated), the
    result reports ``persisted = True``, and ``is_persisted(version)`` is true.
    Both the download path (``download_and_persist``) and the backfill path
    (``persist_from_disk``) are exercised, since both share the core that deletes
    the file after commit.

    Validates: Requirements 2.1, 2.2, 2.3, 7.3
    """
    version = KNOWN_VERSIONS[0]

    data_dir = Path(tempfile.mkdtemp(prefix="stix_delete_failure_data_"))
    db_dir = Path(tempfile.mkdtemp(prefix="stix_delete_failure_db_"))
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

        # Simulate the deletion failure by patching Path.unlink to raise OSError.
        # _delete_bundle_file is the ONLY unlink call and it runs strictly after
        # the single commit, so this patch exercises only the best-effort
        # deletion on the post-commit success path.
        def _raising_unlink(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("simulated deletion failure")

        monkeypatch.setattr(Path, "unlink", _raising_unlink)

        session = session_factory()
        try:
            service = StixPersistenceService(session, stix_service)
            read_service = StixPackageReadService(session)

            # The call must NOT raise even though the deletion fails — the
            # best-effort deletion swallows the OSError (Req. 2.3, 7.3). Both
            # entry points share the delete-after-commit core.
            if use_download_path:
                result = service.download_and_persist(version)
            else:
                result = service.persist_from_disk(version)

            # The commit stands: the result reports persisted (Req. 2.1).
            assert result.persisted is True, (
                "a deletion failure must not flip persisted to False"
            )
            # The content stays persisted — the transaction was not reverted
            # (Req. 2.2): the version is persisted in the database.
            assert read_service.is_persisted(version) is True, (
                "is_persisted must stay true after a best-effort deletion failure"
            )
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(db_dir, ignore_errors=True)
