"""Deterministic tests of the ``stix_packages`` ingestion source (task 21.3).

The orchestrator drives :class:`StixPackagesSourceRunner` with a fake HTTP client
(no network) over a temp-file SQLite (no disk bundle). Covered:

* the desired set: persisted versions are kept, ``latest`` is the default and
  ``STIX_VERSIONS`` overrides it (invalid tokens are ignored);
* idempotency per version: a second run downloads nothing and stays ``ready``;
* failure isolation and resume: one failing version never blocks the others, the
  source ends ``failed`` with a secret-free cause and the next run fetches only
  what is still missing;
* nothing is written to the working directory.

**Validates: Requirements 7.1, 7.2, 7.3, 7.5, 7.6, 7.7**
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StixCampaign, StixPackage
from app.models.enums import IngestSource, IngestStatus
from app.services.ingestion.orchestrator import IngestionOrchestrator, SOURCE_ORDER
from app.services.ingestion.runners import (
    STIX_VERSIONS_ENV,
    StixPackagesSourceRunner,
    build_default_runners,
)
from app.services.stix.stix_service import KNOWN_VERSIONS, StixService
from _stix_fakes import FakeStixHttp, small_bundle

LATEST = KNOWN_VERSIONS[0]


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="stix_ingest_"))
    engine = create_db_engine(
        Settings(database_url=f"sqlite:///{(tmp_dir / 'stix.db').as_posix()}")
    )
    import_models()
    Base.metadata.create_all(bind=engine)
    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True, class_=Session
    )
    try:
        yield session_factory
    finally:
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _make(
    factory: sessionmaker[Session],
    http: FakeStixHttp,
    versions: list[str] | None,
    required: list[str] | None = (),
    latest_fetcher=lambda: LATEST,
) -> tuple[IngestionOrchestrator, StixPackagesSourceRunner]:
    # No network: the newest-version lookup is injected. ``required=()`` keeps
    # these tests independent from the versions the curated seed requires.
    runner = StixPackagesSourceRunner(
        stix_service=StixService(http_client=http),
        versions=versions,
        required_versions=required,
        latest_fetcher=latest_fetcher,
        index_vectors=False,
    )
    return IngestionOrchestrator(factory, {IngestSource.STIX_PACKAGES: runner}), runner


def _persisted(factory: sessionmaker[Session]) -> set[str]:
    with factory() as session:
        return set(session.execute(select(StixPackage.version)).scalars())


def _state(orchestrator: IngestionOrchestrator):
    return next(s for s in orchestrator.status() if s.source == IngestSource.STIX_PACKAGES)


def test_stix_packages_is_a_registered_source_with_default_runner() -> None:
    assert IngestSource.STIX_PACKAGES in SOURCE_ORDER
    assert isinstance(
        build_default_runners()[IngestSource.STIX_PACKAGES], StixPackagesSourceRunner
    )


def test_run_persists_configured_versions_and_is_idempotent(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    http = FakeStixHttp({"18.0": small_bundle(), "17.1": small_bundle("Beta")})
    orchestrator, _ = _make(factory, http, ["18.0", "17.1"])

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"18.0", "17.1"}
    state = _state(orchestrator)
    assert state.status == IngestStatus.READY
    assert state.loaded_count == 2
    assert len(http.calls) == 2

    # Second run: nothing is missing, no network, and still ready.
    statuses = orchestrator.verify_all()
    assert statuses[IngestSource.STIX_PACKAGES].status == IngestStatus.READY
    orchestrator.run_source(IngestSource.STIX_PACKAGES)
    assert len(http.calls) == 2
    with factory() as session:
        assert session.execute(select(func.count()).select_from(StixPackage)).scalar_one() == 2
        assert session.execute(select(func.count()).select_from(StixCampaign)).scalar_one() == 2
    assert list(tmp_path.iterdir()) == []  # nothing written to disk


def test_failure_is_isolated_and_resumable(factory: sessionmaker[Session]) -> None:
    http = FakeStixHttp({"18.0": small_bundle(), "19.1": small_bundle("Gamma")})
    http.fail_versions.add("19.1")
    orchestrator, _ = _make(factory, http, ["19.1", "18.0"])

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"18.0"}  # the healthy version still landed
    state = _state(orchestrator)
    assert state.status == IngestStatus.FAILED
    assert state.error == "network: StixDownloadError"

    http.fail_versions.clear()
    http.calls.clear()
    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"18.0", "19.1"}
    assert _state(orchestrator).status == IngestStatus.READY
    assert [c.rsplit("-", 1)[-1] for c in http.calls] == ["19.1.json"]  # only what was missing


def test_persisted_versions_are_kept_and_latest_is_the_default(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(STIX_VERSIONS_ENV, raising=False)
    with factory() as session:
        session.add(StixPackage(version="17.0", name="Enterprise ATT&CK v17.0"))
        session.commit()
    http = FakeStixHttp({LATEST: small_bundle()})
    orchestrator, runner = _make(factory, http, None)

    with factory() as session:
        plan = runner.plan(session)
    assert plan.needs_fetch is True
    assert plan.expected_count == 2  # persisted 17.0 + latest

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"17.0", LATEST}
    assert len(http.calls) == 1  # 17.0 was already in the database


def test_stix_versions_env_overrides_the_default_and_ignores_invalid_tokens(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STIX_VERSIONS_ENV, "18.0, latest ../etc/passwd,v9")
    http = FakeStixHttp({"18.0": small_bundle(), LATEST: small_bundle("Delta")})
    orchestrator, _ = _make(factory, http, None)

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"18.0", LATEST}
    assert len(http.calls) == 2

    monkeypatch.setenv(STIX_VERSIONS_ENV, "17.1")
    http.bundles["17.1"] = small_bundle("Epsilon")
    orchestrator.run_source(IngestSource.STIX_PACKAGES)
    assert _persisted(factory) == {"18.0", LATEST, "17.1"}  # persisted ones are never dropped


def test_requested_version_is_ingested_even_when_not_configured(
    factory: sessionmaker[Session],
) -> None:
    http = FakeStixHttp({"16.1": small_bundle()})
    orchestrator, runner = _make(factory, http, [])

    with factory() as session:
        assert runner.plan(session).is_empty

    runner.request_version("16.1")
    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"16.1"}
    assert _state(orchestrator).status == IngestStatus.READY


# ---------------------------------------------------------------------------
# Version policy (task 33.2): latest (upstream) + database + seed-required
# ---------------------------------------------------------------------------


def test_seed_required_version_is_always_part_of_the_plan(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STIX_VERSIONS_ENV, "17.1")  # overrides only item (a)
    http = FakeStixHttp({"18.0": small_bundle(), "17.1": small_bundle("Beta")})
    orchestrator, runner = _make(factory, http, None, required=None)

    with factory() as session:
        assert runner.plan(session).expected_count == 2  # 17.1 + seed-required 18.0

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"18.0", "17.1"}  # LATEST was replaced, 18.0 was not


def test_stix_versions_never_removes_seed_required_or_persisted_versions(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STIX_VERSIONS_ENV, "19.0")
    with factory() as session:
        session.add(StixPackage(version="17.0", name="Enterprise ATT&CK v17.0"))
        session.commit()
    http = FakeStixHttp({"19.0": small_bundle(), "18.0": small_bundle("Beta")})
    # Default required versions (the seed's): 18.0.
    orchestrator, runner = _make(factory, http, None, required=None)

    with factory() as session:
        assert runner.plan(session).expected_count == 3  # 17.0 + 18.0 + 19.0

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"17.0", "18.0", "19.0"}  # no latest, nothing removed
    assert sorted(c.rsplit("-", 1)[-1] for c in http.calls) == ["18.0.json", "19.0.json"]


def test_default_plan_is_latest_union_seed_required_union_database(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(STIX_VERSIONS_ENV, raising=False)
    with factory() as session:
        session.add(StixPackage(version="17.0", name="Enterprise ATT&CK v17.0"))
        session.commit()
    http = FakeStixHttp({"20.1": small_bundle(), "18.0": small_bundle("Beta")})
    orchestrator, _ = _make(factory, http, None, required=None, latest_fetcher=lambda: "20.1")

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"17.0", "18.0", "20.1"}
    assert _state(orchestrator).status == IngestStatus.READY


def test_latest_is_resolved_from_the_upstream_fetcher(factory: sessionmaker[Session]) -> None:
    http = FakeStixHttp({"20.1": small_bundle(), "18.0": small_bundle("Beta")})
    orchestrator, _ = _make(factory, http, None, required=["18.0"], latest_fetcher=lambda: "20.1")

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {"20.1", "18.0"}


def test_latest_falls_back_to_the_known_newest_when_the_lookup_fails(
    factory: sessionmaker[Session],
) -> None:
    def offline() -> str:
        raise ConnectionError("offline")

    http = FakeStixHttp({LATEST: small_bundle()})
    orchestrator, _ = _make(factory, http, None, latest_fetcher=offline)

    orchestrator.run_source(IngestSource.STIX_PACKAGES)

    assert _persisted(factory) == {LATEST}
    assert _state(orchestrator).status == IngestStatus.READY


def test_plan_does_not_use_the_network_and_is_empty_when_a_newer_version_is_installed(
    factory: sessionmaker[Session],
) -> None:
    def forbidden() -> str:
        raise AssertionError("plan must not query the upstream index")

    with factory() as session:
        session.add(StixPackage(version="99.0", name="newer than the built-in latest"))
        session.commit()
    runner = StixPackagesSourceRunner(
        stix_service=StixService(http_client=FakeStixHttp()),
        required_versions=[],
        latest_fetcher=forbidden,
        index_vectors=False,
    )
    with factory() as session:
        assert runner.plan(session).is_empty


def test_latest_enterprise_version_reads_the_upstream_index_format() -> None:
    from app.services.stix.stix_service import StixDownloadError, latest_enterprise_version

    def entry(domain: str, version: str) -> dict:
        url = f"https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/{domain}/{domain}-{version}.json"
        return {"version": version, "url": url, "modified": "2026-01-01T00:00:00.000Z"}

    index = {
        "collections": [
            {"name": "Enterprise ATT&CK", "versions": [entry("enterprise-attack", v) for v in ("9.0", "19.2", "20.1", "20.0")]},
            {"name": "ICS ATT&CK", "versions": [entry("ics-attack", "99.0")]},
            {"name": "Mobile ATT&CK", "versions": [entry("mobile-attack", "98.0")]},
        ]
    }
    assert latest_enterprise_version(index) == "20.1"
    with pytest.raises(StixDownloadError):
        latest_enterprise_version({"collections": []})
    with pytest.raises(StixDownloadError):
        latest_enterprise_version("not an index")
