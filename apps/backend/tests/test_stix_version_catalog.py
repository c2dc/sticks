"""Deterministic tests of the upstream ATT&CK version list (task 38.3).

No network, no real sleeps, no Hypothesis: the upstream ``index.json`` is a fake
dict in the real ``collections[].versions[]`` shape served by an injected fetcher,
and TTLs are driven by an injectable clock. Covered:

* parsing and semantic ordering (ICS/Mobile ignored, malformed entries skipped);
* the cache (1 h hit, 60 s negative cache, refresh after the TTL, concurrency);
* the offline fallback to ``KNOWN_VERSIONS`` (never raises);
* the ``stix_packages`` plan: newer release detection, banco, seed and UI combined,
  ``STIX_VERSIONS`` replacing only ``latest``, no network wait on request threads,
  resumable failures;
* the lifespan warm-up of the cache does not block the startup.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from _stix_fakes import FakeStixHttp, small_bundle
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StixPackage
from app.models.enums import IngestSource, IngestStatus
from app.services.ingestion import runners as runners_module
from app.services.ingestion.orchestrator import IngestionOrchestrator
from app.services.ingestion.runners import STIX_VERSIONS_ENV, StixPackagesSourceRunner
from app.services.stix.stix_service import (
    KNOWN_VERSIONS,
    StixService,
    StixVersionCatalog,
    _semantic_version_key,
    enterprise_versions,
    get_version_catalog,
    list_remote_versions,
    set_version_catalog,
)

_BASE_URL = "https://example.invalid/mitre-attack/{collection}/{collection}-{version}.json"
_FALLBACK = sorted(KNOWN_VERSIONS, key=_semantic_version_key, reverse=True)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def make_index(
    *versions: str, ics: tuple[str, ...] = (), mobile: tuple[str, ...] = ()
) -> dict[str, Any]:
    """An index in the real upstream shape: one collection per domain."""

    def _collection(name: str, slug: str, items: tuple[str, ...]) -> dict[str, Any]:
        return {
            "name": name,
            "versions": [
                {
                    "version": v,
                    "url": _BASE_URL.format(collection=slug, version=v),
                    "modified": "2025-01-01T00:00:00.000Z",
                }
                for v in items
            ],
        }

    return {
        "collections": [
            _collection("Enterprise ATT&CK", "enterprise-attack", tuple(versions)),
            _collection("Mobile ATT&CK", "mobile-attack", mobile),
            _collection("ICS ATT&CK", "ics-attack", ics),
        ]
    }


class FakeClock:
    """Monotonic clock moved by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeIndex:
    """Injectable fetcher: serves ``payload`` (or raises ``error``) and records calls."""

    def __init__(self, payload: Any = None, error: BaseException | None = None) -> None:
        self.payload = payload
        self.error = error
        self.timeouts: list[float] = []
        self.threads: list[str] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        return len(self.timeouts)

    def __call__(self, timeout: float) -> Any:
        with self._lock:
            self.timeouts.append(timeout)
            self.threads.append(threading.current_thread().name)
        if self.error is not None:
            raise self.error
        return self.payload


class _ClockedTime:
    """Stand-in for the ``time`` module of ``runners`` with a controllable monotonic."""

    def __init__(self, clock: FakeClock) -> None:
        self.monotonic = clock

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def _join_refresh_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "stix-version-refresh":
            thread.join(5)


def _run_in_thread(fn: Callable[[], Any], name: str = "ingestion-test") -> Any:
    out: dict[str, Any] = {}

    def _target() -> None:
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the caller
            out["error"] = exc

    thread = threading.Thread(target=_target, name=name)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    if "error" in out:
        raise out["error"]
    return out["value"]


# ---------------------------------------------------------------------------
# (a) Parsing and semantic order
# ---------------------------------------------------------------------------


def test_enterprise_versions_are_ordered_semantically_not_lexically() -> None:
    index = make_index("9.0", "19.1", "8.10", "20.1", "8.2")

    assert enterprise_versions(index) == ["20.1", "19.1", "9.0", "8.10", "8.2"]
    assert _semantic_version_key("8.2") < _semantic_version_key("8.10")


def test_enterprise_versions_ignores_ics_and_mobile_collections() -> None:
    index = make_index("19.1", ics=("99.0",), mobile=("98.0",))

    assert enterprise_versions(index) == ["19.1"]


def test_enterprise_versions_skips_malformed_entries_and_duplicates() -> None:
    def good(v: str) -> dict[str, str]:
        return {
            "version": v,
            "url": _BASE_URL.format(collection="enterprise-attack", version=v),
        }

    index = {
        "collections": [
            "not-a-collection",
            {"name": "no versions key"},
            {"name": "bad versions", "versions": "oops"},
            {
                "name": "Enterprise ATT&CK",
                "versions": [
                    good("19.1"),
                    good("19.1"),  # duplicate
                    "not-a-dict",
                    {"version": "19.0"},  # no url
                    {"url": _BASE_URL.format(collection="enterprise-attack", version="18.1")},
                    {
                        "version": 18.0,
                        "url": _BASE_URL.format(collection="enterprise-attack", version="18.0"),
                    },
                    {
                        "version": "17.1",  # url points at another version
                        "url": _BASE_URL.format(collection="enterprise-attack", version="17.0"),
                    },
                    {"version": "16.1", "url": "https://example.invalid/other/x.json"},
                    {"version": "15.1", "url": None},
                ],
            },
        ]
    }

    assert enterprise_versions(index) == ["19.1"]
    for garbage in (None, [], "text", 42, {}, {"collections": None}, {"collections": {}}):
        assert enterprise_versions(garbage) == []


# ---------------------------------------------------------------------------
# (b) Cache
# ---------------------------------------------------------------------------


def test_second_call_within_the_ttl_does_not_hit_the_network() -> None:
    clock, fetcher = FakeClock(), FakeIndex(make_index("20.2", "20.1"))
    catalog = StixVersionCatalog(fetcher, clock=clock)

    first = catalog.list_versions()
    clock.advance(3599)
    second = catalog.list_versions()

    assert first == second == ["20.2", "20.1"]
    assert fetcher.calls == 1
    assert fetcher.timeouts == [5.0]  # short timeout passed to the fetcher


def test_catalog_queries_again_after_the_ttl() -> None:
    clock, fetcher = FakeClock(), FakeIndex(make_index("20.1"))
    catalog = StixVersionCatalog(fetcher, clock=clock)
    assert catalog.list_versions() == ["20.1"]

    fetcher.payload = make_index("20.2", "20.1")
    clock.advance(3601)

    assert catalog.list_versions() == ["20.2", "20.1"]
    assert fetcher.calls == 2


def test_concurrent_calls_on_a_warm_cache_share_one_query() -> None:
    fetcher = FakeIndex(make_index("20.1", "19.1"))
    catalog = StixVersionCatalog(fetcher, clock=FakeClock())
    catalog.warm_up()

    results: list[list[str]] = []
    threads = [
        threading.Thread(target=lambda: results.append(catalog.list_versions())) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 8 and all(r == ["20.1", "19.1"] for r in results)
    assert fetcher.calls == 1


def test_cold_cache_with_concurrent_callers_queries_upstream_once() -> None:
    entered = threading.Event()
    release = threading.Event()
    inner = FakeIndex(make_index("20.1", "19.1"))

    def blocking_fetcher(timeout: float) -> Any:
        entered.set()
        assert release.wait(10)  # held open while the other callers pile up
        return inner(timeout)

    catalog = StixVersionCatalog(blocking_fetcher, clock=FakeClock())
    results: list[list[str]] = []
    threads = [
        threading.Thread(target=lambda: results.append(catalog.list_versions())) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(10)
    release.set()
    for thread in threads:
        thread.join(10)

    assert not any(t.is_alive() for t in threads)
    assert len(results) == 8 and all(r == ["20.1", "19.1"] for r in results)
    assert inner.calls == 1


def test_cached_latest_is_the_newest_remote_version_and_none_without_remote() -> None:
    clock = FakeClock()
    remote = StixVersionCatalog(FakeIndex(make_index("20.2", "20.1")), clock=clock)
    assert remote.cached_latest(block=True) == "20.2"

    offline = StixVersionCatalog(FakeIndex(error=OSError("offline")), clock=clock)
    assert offline.cached_latest(block=True) is None  # only the fallback is cached


def test_cached_latest_without_block_never_queries_on_the_calling_thread() -> None:
    fetcher = FakeIndex(make_index("20.2"))
    catalog = StixVersionCatalog(fetcher, clock=FakeClock())

    assert catalog.cached_latest(block=False) is None  # cold cache
    _join_refresh_threads()

    assert threading.main_thread().name not in fetcher.threads
    assert catalog.cached_latest(block=False) == "20.2"  # filled by the background refresh


# ---------------------------------------------------------------------------
# (c) Offline, errors, empty or invalid index
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fetcher",
    [
        FakeIndex(error=OSError("network down")),
        FakeIndex(error=TimeoutError("timed out")),
        FakeIndex(error=ValueError("invalid JSON")),
        FakeIndex(payload={}),
        FakeIndex(payload={"collections": []}),
        FakeIndex(payload=make_index()),
        FakeIndex(payload="<html>not json</html>"),
        FakeIndex(payload=None),
    ],
    ids=[
        "oserror",
        "timeout",
        "invalid-json",
        "empty-dict",
        "no-collections",
        "no-versions",
        "html",
        "none",
    ],
)
def test_failure_falls_back_to_known_versions_with_negative_cache(fetcher: FakeIndex) -> None:
    clock = FakeClock()
    catalog = StixVersionCatalog(fetcher, clock=clock)

    assert catalog.list_versions() == _FALLBACK  # does not raise
    clock.advance(30)
    assert catalog.list_versions() == _FALLBACK
    clock.advance(29)  # 59 s after the failure: still negative-cached
    assert catalog.list_versions() == _FALLBACK
    assert fetcher.calls == 1

    clock.advance(2)  # 61 s: the negative cache expired
    assert catalog.list_versions() == _FALLBACK
    assert fetcher.calls == 2
    assert catalog.cached_latest(block=True) is None


def test_fallback_includes_the_documented_known_versions() -> None:
    for version in ("20.1", "20.0", "19.2", "18.1"):
        assert version in KNOWN_VERSIONS


def test_catalog_recovers_after_the_negative_cache_when_the_network_is_back() -> None:
    clock, fetcher = FakeClock(), FakeIndex(error=OSError("offline"))
    catalog = StixVersionCatalog(fetcher, clock=clock)
    assert catalog.list_versions() == _FALLBACK

    fetcher.error, fetcher.payload = None, make_index("21.0", "20.1")
    clock.advance(61)

    assert catalog.list_versions() == ["21.0", "20.1"]
    clock.advance(3000)  # positive cache is the long one
    catalog.list_versions()
    assert fetcher.calls == 2


def test_is_known_uses_the_remote_list_or_the_fallback() -> None:
    remote = StixVersionCatalog(FakeIndex(make_index("21.0")), clock=FakeClock())
    assert remote.is_known("21.0") and not remote.is_known("0.0-unknown")

    offline = StixVersionCatalog(FakeIndex(error=OSError("x")), clock=FakeClock())
    assert offline.is_known("19.2") and not offline.is_known("99.9")


def test_list_remote_versions_with_an_explicit_fetcher_is_isolated_from_the_shared_cache() -> None:
    fetcher = FakeIndex(make_index("21.0"))
    shared = get_version_catalog()

    assert list_remote_versions(fetcher) == ["21.0"]
    assert list_remote_versions(fetcher) == ["21.0"]
    assert fetcher.calls == 2  # throw-away catalog: no shared cache
    assert get_version_catalog() is shared


# ---------------------------------------------------------------------------
# (f) Runner plan
# ---------------------------------------------------------------------------


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="stix_catalog_"))
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


class Env:
    """Fake upstream + clock wired into the process-wide catalog and the runner."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
        index_versions: tuple[str, ...],
        bundles: tuple[str, ...] = (),
    ) -> None:
        self.factory = factory
        self.clock = FakeClock()
        self.index = FakeIndex(make_index(*index_versions))
        self.http = FakeStixHttp({v: small_bundle(f"C{v}") for v in bundles})
        self.catalog = StixVersionCatalog(self.index, clock=self.clock)
        set_version_catalog(self.catalog)  # restored by the autouse conftest fixture
        monkeypatch.delenv(STIX_VERSIONS_ENV, raising=False)
        monkeypatch.setattr(runners_module, "time", _ClockedTime(self.clock))
        # ``run`` builds its own StixService: serve it from memory.
        original_init = StixService.__init__
        monkeypatch.setattr(
            StixService,
            "__init__",
            lambda svc, http_client=None: original_init(svc, http_client or self.http),
        )
        self.runner = StixPackagesSourceRunner(required_versions=["18.0"], index_vectors=False)
        self.orchestrator = IngestionOrchestrator(
            factory, {IngestSource.STIX_PACKAGES: self.runner}
        )

    def persist(self, *versions: str) -> None:
        with self.factory() as session:
            for version in versions:
                session.add(StixPackage(version=version, name=f"Enterprise ATT&CK v{version}"))
            session.commit()

    def persisted(self) -> set[str]:
        with self.factory() as session:
            return set(session.execute(select(StixPackage.version)).scalars())

    def plan(self, thread_name: str = "ingestion-plan"):
        def _plan():
            with self.factory() as session:
                return self.runner.plan(session)

        return _run_in_thread(_plan, thread_name)

    def run(self) -> None:
        self.catalog.warm_up()  # fresh cache: the plan inside ``run_source`` is deterministic
        self.orchestrator.run_source(IngestSource.STIX_PACKAGES)

    def fetched_versions(self) -> list[str]:
        return [c.rsplit("-", 1)[-1][: -len(".json")] for c in self.http.calls]


def test_plan_downloads_a_newer_index_version_and_keeps_the_others(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, ("20.1", "19.1", "19.0", "18.0"), bundles=("20.1",))
    env.persist("18.0", "19.0", "19.1")

    plan = env.plan()
    assert plan.needs_fetch is True
    assert plan.expected_count == 4  # 18.0, 19.0, 19.1 + 20.1 (nothing is dropped)

    env.run()

    assert env.persisted() == {"18.0", "19.0", "19.1", "20.1"}
    assert env.fetched_versions() == ["20.1"]  # only what was missing
    assert env.plan().is_empty


def test_plan_is_empty_without_network_once_the_latest_is_installed(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, ("20.1",))
    env.persist("18.0", "20.1")
    env.catalog.warm_up()
    queries = env.index.calls

    env.index.error = OSError("network must not be used")
    assert env.plan().is_empty
    assert env.plan("request-thread").is_empty
    assert env.index.calls == queries


def test_new_release_after_the_latest_is_installed_is_downloaded(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, ("20.1", "18.0"), bundles=("20.1", "20.2"))
    env.persist("18.0")
    env.run()
    assert env.persisted() == {"18.0", "20.1"}
    assert env.plan().is_empty

    # MITRE publishes 20.2; both the catalog and the run memory expire (1 h).
    env.index.payload = make_index("20.2", "20.1", "18.0")
    env.clock.advance(3601)

    # A request thread gets the stale answer (empty plan) and never waits...
    mark = len(env.index.threads)
    assert env.plan("request-thread").is_empty
    _join_refresh_threads()
    assert env.index.threads[mark:] == ["stix-version-refresh"]
    # ...and the background refresh makes the next plan see the release.
    plan = env.plan("request-thread")
    assert plan.needs_fetch is True
    assert plan.expected_count == 3  # 18.0, 20.1 + 20.2

    env.http.calls.clear()
    env.run()

    assert env.persisted() == {"18.0", "20.1", "20.2"}
    assert env.fetched_versions() == ["20.2"]
    assert env.plan().is_empty


def test_stix_versions_replaces_only_latest_and_never_removes_database_or_seed(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, ("20.1", "19.0"), bundles=("19.0", "18.0"))
    monkeypatch.setenv(STIX_VERSIONS_ENV, "19.0")
    env.persist("19.1")  # 18.0 (seed) is missing and must come back

    plan = env.plan()
    assert plan.needs_fetch is True
    assert plan.expected_count == 3  # 19.1 (db) + 18.0 (seed) + 19.0 (pinned)

    env.run()

    assert env.persisted() == {"18.0", "19.0", "19.1"}  # no 20.1: latest was replaced
    assert sorted(env.fetched_versions()) == ["18.0", "19.0"]


def test_ui_requested_version_is_part_of_the_plan(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, ("20.1", "17.1"), bundles=("17.1",))
    env.persist("18.0", "20.1")
    assert env.plan().is_empty

    env.runner.request_version("17.1")

    assert env.plan().needs_fetch is True
    env.run()
    assert env.persisted() == {"18.0", "20.1", "17.1"}


def test_plan_offline_falls_back_to_the_newest_known_version(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, (), bundles=(_FALLBACK[0], "18.0"))
    env.index.error = OSError("offline")
    env.persist("18.0")

    plan = env.plan()  # never raises

    assert plan.needs_fetch is True
    assert plan.expected_count == 2  # 18.0 + max(KNOWN_VERSIONS)
    env.run()
    assert env.persisted() == {"18.0", max(KNOWN_VERSIONS, key=_semantic_version_key)}


def test_plan_on_a_request_thread_does_not_wait_for_the_network_and_refreshes_in_background(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, ("21.0",))
    env.persist("18.0", _FALLBACK[0])

    # A request thread with a cold cache: the fallback latest is already installed.
    assert env.plan("anyio-worker-0").is_empty
    _join_refresh_threads()
    assert env.index.calls == 1
    assert env.index.threads == ["stix-version-refresh"]  # never the calling thread

    # The background refresh made the newer release visible to the next plan.
    plan = env.plan("anyio-worker-0")
    assert plan.needs_fetch is True
    assert plan.expected_count == 3  # 18.0 + 20.1 + 21.0


def test_plan_in_an_ingestion_thread_fills_a_cold_cache_inline(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, ("21.0",))
    env.persist("18.0", _FALLBACK[0])

    plan = env.plan("ingestion-stix")

    assert plan.needs_fetch is True  # 21.0 is newer than everything installed
    assert env.index.threads == ["ingestion-stix"]


def test_failed_download_is_isolated_and_resumable(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(factory, monkeypatch, ("20.1",), bundles=("20.1", "18.0"))
    env.persist("19.0")
    env.http.fail_versions.add("20.1")

    env.run()

    assert env.persisted() == {"18.0", "19.0"}  # the healthy seed version still landed
    state = next(s for s in env.orchestrator.status() if s.source == IngestSource.STIX_PACKAGES)
    assert state.status == IngestStatus.FAILED
    assert env.plan().needs_fetch is True

    env.http.fail_versions.clear()
    env.http.calls.clear()
    env.run()

    assert env.persisted() == {"18.0", "19.0", "20.1"}
    assert env.fetched_versions() == ["20.1"]
    state = next(s for s in env.orchestrator.status() if s.source == IngestSource.STIX_PACKAGES)
    assert state.status == IngestStatus.READY


# ---------------------------------------------------------------------------
# (g) Lifespan warm-up
# ---------------------------------------------------------------------------


def test_lifespan_warms_the_cache_without_blocking_the_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    entered, release = threading.Event(), threading.Event()
    index = make_index("21.0", "20.1")

    def _slow_fetcher(timeout: float) -> Any:
        entered.set()
        release.wait(10)  # blocks the warm-up thread until the test lets it go
        return index

    catalog = StixVersionCatalog(_slow_fetcher)
    set_version_catalog(catalog)
    monkeypatch.setattr(main_module, "ingest_on_start_enabled", lambda: True)
    monkeypatch.setattr(runners_module, "build_default_runners", lambda **k: {})
    monkeypatch.setattr(IngestionOrchestrator, "start_background", lambda self: None)

    try:
        with TestClient(main_module.create_app()):
            # The startup completed while the fetcher is still blocked.
            assert entered.wait(5)
            warmup = [t for t in threading.enumerate() if t.name == "stix-version-warmup"]
            assert warmup and warmup[0].daemon and warmup[0].is_alive()
            release.set()
            for thread in threading.enumerate():
                if thread.name == "stix-version-warmup":
                    thread.join(5)
        assert catalog.list_versions() == ["21.0", "20.1"]
    finally:
        release.set()
