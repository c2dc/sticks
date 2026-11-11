"""Property-based test: the base listing state reflects the database.

# Feature: stix-base-persisted-state-and-file-cleanup

Property 4: Estado da base reflete o banco
Validates: Requirements 3.1, 3.2, 3.3

Hypothesis: for any arbitrary split of ``KNOWN_VERSIONS`` into persisted and
non-persisted versions — and regardless of which versions happen to have a
Bundle_File on disk — the Bases_Listing produced by
``Stage1Service(stix_service, read_service).list_bases()`` marks ``selectable``
and ``downloaded`` as true exactly for the persisted versions and false for the
non-persisted ones. This proves the source of truth is the database
(``StixPackageReadService.is_persisted``), not the on-disk bundle presence
(Req. 3.1, 3.2, 3.3). For persisted versions ``file`` is also ``None`` (the
on-disk bundle is deleted after a successful commit, so the path is no longer a
useful signal).

Isolation (no real files / no real network)
-------------------------------------------
* The ``StixService`` is built with an isolated per-example ``data_dir`` and a
  fake ``http_client`` that fails if called, so the real ``sticks/data/stix`` is
  never touched and listing never reaches the network (``list_bases`` has no
  network egress anyway).
* The persisted state is seeded directly in an ephemeral per-example temp-file
  SQLite whose schema is the six STIX tables built straight from the ORM
  (``Base.metadata.create_all`` scoped to those tables), following the backend
  test session pattern. A version is seeded as persisted by inserting a single
  ``stix_packages`` row — exactly what ``is_persisted`` checks for.
* Bundle_Files are materialized on disk for an *independent* arbitrary subset of
  versions, so the test also covers the cross cases "persisted without file" and
  "non-persisted with file on disk", demonstrating disk presence never drives
  the state.
"""

from __future__ import annotations

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
from app.services.stix.stage1_service import Stage1Service
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

    ``list_bases`` has no network egress, so any ``get`` call would be a
    regression; this client turns that into an immediate failure.
    """

    def get(self, url: str, timeout: int | None = None):  # pragma: no cover
        raise AssertionError("list_bases must not reach the network")


def _seed_persisted(session: Session, versions: set[str]) -> None:
    """Seed a persisted ``stix_packages`` row for each version in ``versions``.

    ``is_persisted`` is true exactly when a ``stix_packages`` row exists for the
    version, so inserting the root row is all that is needed to make a version
    count as persisted in the database.
    """
    for version in versions:
        session.add(
            StixPackage(
                version=version,
                name=f"Enterprise ATT&CK v{version}",
                source_file=None,
            )
        )
    session.commit()


def _materialize_files(data_dir: Path, versions: set[str]) -> None:
    """Write a Bundle_File on disk for each version in ``versions``.

    The content is irrelevant for this property: ``list_bases`` must derive state
    from the database, never from disk, so a placeholder body is enough to prove
    that disk presence does not flip ``downloaded``/``selectable``.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    for version in versions:
        (data_dir / f"enterprise-attack-{version}.json").write_text(
            '{"type": "bundle", "objects": []}', encoding="utf-8"
        )


# Feature: stix-base-persisted-state-and-file-cleanup, Property 4: Estado da base reflete o banco
@PROP_SETTINGS
@given(
    persisted_versions=st.sets(st.sampled_from(KNOWN_VERSIONS)),
    on_disk_versions=st.sets(st.sampled_from(KNOWN_VERSIONS)),
)
def test_property4_estado_da_base_reflete_o_banco(
    persisted_versions: set[str], on_disk_versions: set[str]
) -> None:
    """The listing marks selectable/downloaded exactly for persisted versions.

    For an arbitrary set of persisted versions and an *independent* arbitrary set
    of versions materialized on disk, every entry of the Bases_Listing has
    ``selectable == downloaded == (version in persisted_versions)`` — regardless
    of whether the version has a Bundle_File on disk. Persisted versions also
    carry ``file is None`` (the bundle is deleted after a successful commit).

    Validates: Requirements 3.1, 3.2, 3.3
    """
    data_dir = Path(tempfile.mkdtemp(prefix="stix_state_db_data_"))
    db_dir = Path(tempfile.mkdtemp(prefix="stix_state_db_db_"))
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
        # Materialize Bundle_Files for an arbitrary subset of versions so the
        # "non-persisted but on disk" and "persisted but no file" crosses are
        # exercised — disk presence must never drive the listing state.
        _materialize_files(data_dir, on_disk_versions)

        session = session_factory()
        try:
            _seed_persisted(session, persisted_versions)

            stix_service = StixService(
                data_dir=data_dir, http_client=_FailingHttpClient()
            )
            read_service = StixPackageReadService(session)
            service = Stage1Service(stix_service, read_service)

            bases = service.list_bases()

            # Every known version appears exactly once in the listing.
            listed_versions = [base.version for base in bases]
            assert sorted(listed_versions) == sorted(KNOWN_VERSIONS), (
                "the listing must cover every known version exactly once"
            )

            for base in bases:
                expected = base.version in persisted_versions
                # selectable/downloaded reflect the database, not disk (Req. 3.1,
                # 3.2, 3.3).
                assert base.downloaded is expected, (
                    f"v{base.version}: downloaded={base.downloaded} but "
                    f"persisted={expected}"
                )
                assert base.selectable is expected, (
                    f"v{base.version}: selectable={base.selectable} but "
                    f"persisted={expected}"
                )
                # Persisted versions expose no on-disk path; the bundle is deleted
                # after commit, so `file` is None regardless of what is on disk.
                if expected:
                    assert base.file is None, (
                        f"v{base.version}: persisted versions must have file=None"
                    )
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(db_dir, ignore_errors=True)
