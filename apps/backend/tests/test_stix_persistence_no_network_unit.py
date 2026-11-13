"""Targeted example: persisting an in-memory bundle performs NO network egress.

Validates: Requirements 1.5, 7.1

``StixPersistenceService.persist_bundle`` never calls ``StixService.fetch_bundle``,
so an injected ``http_client`` whose ``get`` would raise if invoked must never be
reached. The service runs against an ephemeral file-backed SQLite engine built
with ``Base.metadata.create_all``.
"""

from __future__ import annotations

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

    ``persist_bundle`` must never trigger a download, so ``get`` must never be reached. If it is, ``called`` flips to ``True``
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
                "external_references": [{"source_name": "mitre-attack", "external_id": "T1059"}],
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


def test_persist_bundle_makes_no_network_call(session: Session) -> None:
    """``persist_bundle`` commits the package without reaching the http client."""
    version = "18.0"
    http_client = _FailingHttpClient()
    service = StixPersistenceService(
        session=session, stix_service=StixService(http_client=http_client)
    )

    result = service.persist_bundle(version, _build_bundle())

    assert result.persisted is True
    assert http_client.called is False
