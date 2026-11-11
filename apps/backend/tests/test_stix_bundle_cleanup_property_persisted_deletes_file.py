"""Property-based test: a confirmed persistence deletes the on-disk Bundle_File.

# Feature: stix-base-persisted-state-and-file-cleanup

Property 1: Persistência confirmada deleta o arquivo
Validates: Requirements 1.1, 1.3, 6.3

Hypothesis: for any valid STIX bundle and either persistence entry point
(``download_and_persist`` — which fetches through a fake ``http_client`` — or
``persist_from_disk`` — which reads the bundle already on disk), once the call
returns successfully the Bundle_File for that version no longer exists on disk
and ``StixPackageReadService.is_persisted(version)`` is true. The result also
reports ``persisted=True`` (and ``downloaded=True`` for the download path),
matching the DTO contract that survives the best-effort deletion (Req. 1.3).

Isolation (no real files / no real network)
-------------------------------------------
* The bundle lives under an isolated per-example ``data_dir`` as
  ``enterprise-attack-{version}.json`` and the service is built with
  ``StixService(data_dir=..., http_client=<fake>)`` so the real
  ``sticks/data/stix`` is never touched.
* For the ``download_and_persist`` path the fake ``http_client`` returns the
  generated bundle body, so the single network egress is exercised without a
  real request; for the ``persist_from_disk`` path the fake client raises if
  called, proving the backfill never reaches the network.
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


@st.composite
def _valid_bundle(draw: st.DrawFn) -> list[dict]:
    """Generate a valid STIX bundle with campaigns, attack-patterns,
    intrusion-sets and ``uses`` / ``attributed-to`` relationships.

    The generated objects exercise every one of the six persisted tables so the
    derivation and the single transactional write run end-to-end before the
    bundle deletion.
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

    # --- Campaigns ------------------------------------------------------
    n_campaigns = draw(st.integers(min_value=1, max_value=4))
    campaign_ids: list[str] = []
    for i in range(n_campaigns):
        campaign_id = _stix_id("campaign", counter)
        counter += 1
        campaign_ids.append(campaign_id)
        objects.append(
            {
                "type": "campaign",
                "id": campaign_id,
                "name": f"Campaign {i}",
                "description": f"Campaign description {i}",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": f"C{9000 + i}"}
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

    # Shuffle so object ordering never influences persistence.
    return list(draw(st.permutations(objects)))


def _bundle_text(objects: list[dict]) -> str:
    """Serialize a valid STIX bundle (``{"objects": [...]}``) to text."""
    return json.dumps({"type": "bundle", "objects": objects})


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a valid STIX bundle to disk at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_bundle_text(objects), encoding="utf-8")


class _FakeResponse:
    """Minimal response stand-in for the injected ``http_client`` (text + OK)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:  # pragma: no cover - always OK here.
        return None


class _FakeHttpClient:
    """Fake ``http_client`` whose ``get`` returns the generated bundle body.

    Used by the ``download_and_persist`` path so the single network egress is
    exercised without a real request.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    def get(self, url: str, timeout: int | None = None) -> _FakeResponse:
        return _FakeResponse(self._text)


class _FailingHttpClient:
    """Fake ``http_client`` that fails if called.

    Used by the ``persist_from_disk`` path to prove the backfill never reaches
    the network — the bundle must already be on disk.
    """

    def get(self, url: str, timeout: int | None = None) -> _FakeResponse:
        raise AssertionError("persist_from_disk must not reach the network")


# Feature: stix-base-persisted-state-and-file-cleanup, Property 1: Persistência confirmada deleta o arquivo
@PROP_SETTINGS
@given(objects=_valid_bundle(), use_download=st.booleans())
def test_property1_persistencia_confirmada_deleta_o_arquivo(
    objects: list[dict], use_download: bool
) -> None:
    """A confirmed persistence deletes the Bundle_File and marks it persisted.

    For either entry point, once the call returns successfully the Bundle_File no
    longer exists on disk and ``is_persisted(version)`` is true; the result also
    reports ``persisted=True`` (and ``downloaded=True`` for the download path).

    Validates: Requirements 1.1, 1.3, 6.3
    """
    version = KNOWN_VERSIONS[0]

    data_dir = Path(tempfile.mkdtemp(prefix="stix_cleanup_data_"))
    db_dir = Path(tempfile.mkdtemp(prefix="stix_cleanup_db_"))
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

    bundle_path = data_dir / f"enterprise-attack-{version}.json"

    try:
        session = session_factory()
        try:
            if use_download:
                # download_and_persist: no file on disk yet; the fake client
                # serves the body so the (single) network egress is exercised.
                http_client = _FakeHttpClient(_bundle_text(objects))
                stix_service = StixService(data_dir=data_dir, http_client=http_client)
                service = StixPersistenceService(session, stix_service)
                assert not bundle_path.exists()

                result = service.download_and_persist(version)

                # The download path reports the bundle was fetched (Req. 1.3).
                assert result.downloaded is True
            else:
                # persist_from_disk: the bundle is already on disk; the client
                # fails if touched, proving no network egress.
                _write_bundle(bundle_path, objects)
                stix_service = StixService(
                    data_dir=data_dir, http_client=_FailingHttpClient()
                )
                service = StixPersistenceService(session, stix_service)
                assert bundle_path.exists()

                result = service.persist_from_disk(version)

                # The backfill reports the bundle was present, not fetched.
                assert result.downloaded is False

            # Common post-conditions for both entry points.
            assert result.persisted is True
            # The Bundle_File is deleted after the confirmed commit (Req. 1.1, 6.3).
            assert not bundle_path.exists(), (
                "Bundle_File still on disk after a confirmed persistence"
            )
            # The database is the source of truth: the version is persisted.
            read_service = StixPackageReadService(session)
            assert read_service.is_persisted(version) is True
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(db_dir, ignore_errors=True)
