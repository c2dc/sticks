"""Targeted example for the no-network / file-intact re-sync guarantee
(task 4.2 of the ``stix-package-overview-resync-and-english-labels`` spec).

Feature: stix-package-overview-resync-and-english-labels.

Validates: Requirements 1.5

A single focused example proving that re-syncing a version whose bundle is
already on disk performs NO network egress. ``StixPersistenceService.persist_from_disk`` never calls
``StixService.download_base``, so an injected ``http_client`` whose ``get`` would
raise if invoked must never be reached. The service is wired as
``StixPersistenceService(session, StixService(data_dir=..., http_client=...))``
against an ephemeral file-backed SQLite engine built with
``Base.metadata.create_all`` — the same session pattern the sibling backend
tests use.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import StixService


# ---------------------------------------------------------------------------
# A fake http client whose ``get`` fails loudly if ever invoked, while
# recording that it was called. Reaching it would mean a network egress fired.
# ---------------------------------------------------------------------------


class _FailingHttpClient:
    """An ``http_client`` double that raises on any ``get`` and records the call.

    ``persist_from_disk`` must never trigger a download when the bundle is on
    disk, so ``get`` must never be reached. If it is, ``called`` flips to ``True``
    and an ``AssertionError`` is raised to surface the unexpected egress.
    """

    def __init__(self) -> None:
        self.called = False

    def get(self, *args: object, **kwargs: object) -> object:
        self.called = True
        raise AssertionError("http_client.get must not be called during re-sync")


# ---------------------------------------------------------------------------
# Ephemeral DB fixture: a SQLite engine on a temporary file with the full
# schema via Base.metadata.create_all (sibling-test pattern).
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    """Yield an ephemeral SQLite ``Session`` with the full schema created."""
    tmp_dir = tempfile.mkdtemp(prefix="stix_no_network_unit_db_")
    db_path = Path(tmp_dir) / "no_network_unit.db"
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


def _build_bundle() -> dict:
    """A minimal valid STIX bundle with one object of each counted type."""
    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-0000-0000-000000000000",
        "objects": [
            {
                "type": "campaign",
                "id": "campaign--00000000-0000-0000-0000-000000000000",
                "name": "Example Campaign",
            },
            {
                "type": "attack-pattern",
                "id": "attack-pattern--00000000-0000-0000-0000-000000000000",
                "name": "Example Technique",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "T1059"}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
                ],
            },
            {
                "type": "x-mitre-tactic",
                "id": "x-mitre-tactic--00000000-0000-0000-0000-000000000000",
                "name": "Example Tactic",
            },
            {
                "type": "intrusion-set",
                "id": "intrusion-set--00000000-0000-0000-0000-000000000000",
                "name": "Example Actor",
            },
            {
                "type": "malware",
                "id": "malware--00000000-0000-0000-0000-000000000000",
                "name": "Example Malware",
            },
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000000",
                "relationship_type": "uses",
                "source_ref": "campaign--00000000-0000-0000-0000-000000000000",
                "target_ref": "attack-pattern--00000000-0000-0000-0000-000000000000",
            },
        ],
    }


def test_persist_from_disk_no_network_and_deletes_bundle(session: Session) -> None:
    """Re-sync from disk never calls the http client; the bundle is deleted on commit.

    Writes a bundle to an isolated ``data_dir``, injects an ``http_client`` that
    fails if invoked, runs ``persist_from_disk``, then asserts (1) the persist
    succeeded, (2) the http client was never called (no network egress), and
    (3) the on-disk bundle is deleted after the successful commit — the DB is now
    the single source of truth for the version (``stix-base-persisted-state-and-file-cleanup``,
    Req. 6.3). The read-only derivation itself performs no network egress.

    Validates: Requirements 1.5
    """
    version = "18.0"
    data_dir = Path(tempfile.mkdtemp(prefix="stix_no_network_data_"))
    try:
        bundle_path = data_dir / f"enterprise-attack-{version}.json"
        bundle_path.write_text(json.dumps(_build_bundle()), encoding="utf-8")

        http_client = _FailingHttpClient()
        stix_service = StixService(data_dir=data_dir, http_client=http_client)
        service = StixPersistenceService(session=session, stix_service=stix_service)

        result = service.persist_from_disk(version)

        # The re-sync committed from the on-disk bundle (no fetch).
        assert result.persisted is True
        assert result.downloaded is False

        # No network egress: the injected client was never reached.
        assert http_client.called is False

        # The successful commit deleted the on-disk bundle best-effort: the DB is
        # now the single source of truth for the version (cleanup spec, Req. 6.3).
        assert not bundle_path.exists()
    finally:
        for root, _dirs, files in os.walk(data_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)
