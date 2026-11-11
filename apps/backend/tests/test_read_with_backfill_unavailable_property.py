"""Property-based test: unavailable without a file and without persistence.

# Feature: stix-base-persisted-state-and-file-cleanup

Property 7: Indisponível sem arquivo e sem persistência
Validates: Requirements 6.1, 5.3

Hypothesis: for any known version that is neither persisted in the database nor
backed by an on-disk Bundle_File, a read routed through
``app.api.stix._read_with_backfill`` resolves to the ``bundle_indisponivel``
422 contract — and does so without ever reaching the network. This proves the
design's "backfill from disk is no longer the default path" semantics
(Req. 6.1): with no persisted row and no file to backfill from, the read
surfaces unavailable, and the download endpoint (not an on-demand disk
backfill) is the path that would make the version available (Req. 5.3).

Isolation (no real files / no real network)
-------------------------------------------
* The module-level ``StixService`` symbol used by ``_read_with_backfill`` is
  patched to a factory that pins ``data_dir`` to an empty per-example temp dir
  and injects a fake ``http_client`` that fails if called. Because
  ``_read_with_backfill`` for the "no row + no file" case never triggers a
  backfill nor any download, the fake client is never invoked — and if the code
  ever regressed into a network egress, the fake would turn it into an immediate
  failure.
* The read closure is the real
  ``StixPackageReadService(session).package_overview(version)`` against an
  ephemeral per-example temp-file SQLite whose schema is the six STIX tables
  built straight from the ORM (``Base.metadata.create_all`` scoped to those
  tables), following the backend test session pattern. On a fresh DB no version
  has a ``stix_packages`` row, so the read raises the typed
  ``StixPackageNotPersistedError`` that drives ``_read_with_backfill`` into the
  unavailable branch.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.api.stix as stix_api
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


class _FailingHttpClient:
    """Fake ``http_client`` that fails if called.

    The "no row + no file" read path must not reach the network: it neither
    backfills nor downloads. Any ``get`` call would therefore be a regression,
    which this client turns into an immediate assertion failure.
    """

    def get(self, url: str, timeout: int | None = None):  # pragma: no cover
        raise AssertionError(
            "_read_with_backfill must not reach the network for an "
            "unavailable version"
        )


# Feature: stix-base-persisted-state-and-file-cleanup, Property 7: Indisponível sem arquivo e sem persistência
@PROP_SETTINGS
@given(version=st.sampled_from(KNOWN_VERSIONS))
def test_property7_indisponivel_sem_arquivo_e_sem_persistencia(version: str) -> None:
    """A non-persisted version with no Bundle_File maps to ``bundle_indisponivel`` 422.

    For an arbitrary known version that has neither a ``stix_packages`` row nor a
    Bundle_File on disk, ``_read_with_backfill`` raises an ``HTTPException`` with
    status 422 and the ``bundle_indisponivel`` contract, and the injected fake
    ``http_client`` is never called (no network egress).

    Validates: Requirements 6.1, 5.3
    """
    data_dir = Path(tempfile.mkdtemp(prefix="stix_unavailable_data_"))
    db_dir = Path(tempfile.mkdtemp(prefix="stix_unavailable_db_"))
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

    def _factory() -> StixService:
        # Pin an empty data_dir (no Bundle_File on disk) and a client that fails
        # loudly if the code ever tried a network egress.
        return StixService(data_dir=data_dir, http_client=_FailingHttpClient())

    try:
        # Precondition: no Bundle_File on disk for this version.
        assert not (data_dir / f"enterprise-attack-{version}.json").exists()

        session = session_factory()
        try:
            # Precondition: nothing persisted on a fresh DB.
            read_service = StixPackageReadService(session)
            assert read_service.is_persisted(version) is False

            with mock.patch.object(stix_api, "StixService", _factory):
                with pytest.raises(HTTPException) as exc_info:
                    stix_api._read_with_backfill(
                        session,
                        version,
                        lambda: StixPackageReadService(session).package_overview(
                            version
                        ),
                    )

            exc = exc_info.value
            # The ``bundle_indisponivel`` 422 contract (Req. 6.1): neither a row
            # nor a file, so the version is unavailable until a fresh download is
            # triggered (Req. 5.3).
            assert exc.status_code == 422, (
                f"v{version}: expected 422 but got {exc.status_code}"
            )
            assert isinstance(exc.detail, dict)
            assert exc.detail["motivo"] == "bundle_indisponivel", (
                f"v{version}: unexpected motivo {exc.detail.get('motivo')!r}"
            )
            assert exc.detail["mensagem"]
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(db_dir, ignore_errors=True)
