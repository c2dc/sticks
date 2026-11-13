"""Deterministic tests for ``DockerHubCatalogFetcher`` (task 39.3).

No network, no real sleeps: ``urllib.request.urlopen`` is replaced by an
in-memory fake that serves responses in the real Docker Hub shape, the fetcher
receives a recording ``sleep`` and a fixed ``clock``, and the backoff jitter is
pinned. The database is a temporary SQLite file and the embedder is a fake.
"""

from __future__ import annotations

import email.message
import io
import json
import random
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import ExperienceMemory as ExperienceMemoryRow
from app.models.domain import ImageCatalog
from app.models.enums import IngestSource, IngestStatus
from app.services.ai.memory import ExperienceMemory
from app.services.docker_catalog_ingest_service import (
    USER_AGENT,
    DockerCatalogFetchError,
    DockerCatalogIngestService,
    DockerHubCatalogFetcher,
)
from app.services.ingestion.orchestrator import IngestionOrchestrator, describe_failure
from app.services.ingestion.runners import DockerCatalogSourceRunner

FIXED_NOW = 2_000_000_000.0
PAGINATION_MESSAGE = "pagination too large for anonymous requests"

# A handler maps (path, query-dict) to (status, body, headers).
Handler = Callable[[str, dict[str, str]], tuple[int, Any, dict[str, str]]]


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeHub:
    """Callable standing in for ``urllib.request.urlopen``."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: Any = None) -> Any:
        self.requests.append(request)
        parsed = urllib.parse.urlparse(request.full_url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        status, body, headers = self.handler(parsed.path, query)
        raw = json.dumps(body).encode("utf-8")
        if status >= 400:
            msg = email.message.Message()
            for key, value in headers.items():
                msg[key] = value
            raise urllib.error.HTTPError(request.full_url, status, "error", msg, io.BytesIO(raw))
        return _FakeResponse(raw)

    def paths(self) -> list[str]:
        return [urllib.parse.urlparse(r.full_url).path for r in self.requests]


class Sleeper:
    """Recording ``sleep``; only non-zero waits are backoff/Retry-After waits."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def waits(self) -> list[float]:
        return [c for c in self.calls if c > 0]


def official_page(names: list[str]) -> dict[str, Any]:
    """``/v2/repositories/library/`` shape: ``name``/``description``, ``next`` null."""
    return {
        "count": len(names),
        "next": None,
        "previous": None,
        "results": [
            {"name": n, "namespace": "library", "description": f"{n} official image"} for n in names
        ],
    }


def search_page(rows: list[tuple[str, str, int]]) -> dict[str, Any]:
    """``/v2/search/repositories/`` shape: ``repo_name``/``short_description``/..."""
    return {
        "count": len(rows),
        "next": None,
        "previous": None,
        "results": [
            {
                "repo_name": name,
                "short_description": desc,
                "pull_count": pulls,
                "star_count": 1,
                "is_official": False,
                "is_automated": False,
            }
            for name, desc, pulls in rows
        ],
    }


OFFICIAL_PATH = "/v2/repositories/library/"
SEARCH_PATH = "/v2/search/repositories/"


@pytest.fixture()
def sleeper(monkeypatch: pytest.MonkeyPatch) -> Sleeper:
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)
    return Sleeper()


def _install(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> FakeHub:
    hub = FakeHub(handler)
    monkeypatch.setattr(urllib.request, "urlopen", hub)
    return hub


def _fetcher(sleeper: Sleeper, **kwargs: Any) -> DockerHubCatalogFetcher:
    kwargs.setdefault("search_terms", ())
    kwargs.setdefault("request_delay", 0)
    return DockerHubCatalogFetcher(sleep=sleeper, clock=lambda: FIXED_NOW, **kwargs)


def _official_only(names: list[str]) -> Handler:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        assert path == OFFICIAL_PATH
        return 200, official_page(names), {}

    return handler


# ---------------------------------------------------------------------------
# (a) real response shape, (b) union, (k) headers
# ---------------------------------------------------------------------------


def test_official_and_search_response_shapes_are_parsed(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH:
            return 200, official_page(["nginx"]), {}
        return 200, search_page([("acme/web", "A web server", 500_000)]), {}

    hub = _install(monkeypatch, handler)
    entries = list(_fetcher(sleeper, search_terms=("web",)).fetch_entries(max_images=50))

    assert [(e.image, e.target_os, e.description) for e in entries] == [
        ("nginx", "linux", "nginx official image"),
        ("acme/web", "linux", "A web server"),
    ]
    # ``next`` is null at the end and is never followed.
    assert hub.paths() == [OFFICIAL_PATH, OFFICIAL_PATH, SEARCH_PATH]


def test_official_union_of_both_orderings_is_deduplicated(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    names = [f"img{i:03d}" for i in range(181)]

    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        assert query["page_size"] == "100"
        ordered = names if query["ordering"] == "name" else names[::-1]
        return 200, official_page(ordered[:100]), {}

    hub = _install(monkeypatch, handler)
    entries = list(_fetcher(sleeper).fetch_entries(max_images=10_000))

    images = [e.image for e in entries]
    assert len(images) == 181
    assert len(set(images)) == 181
    assert sorted(images) == names
    assert [r.full_url.split("ordering=")[1] for r in hub.requests] == ["name", "-name"]


def test_user_agent_and_accept_headers_are_sent(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    hub = _install(monkeypatch, _official_only(["nginx"]))
    list(_fetcher(sleeper).fetch_entries(max_images=10))

    assert hub.requests
    for request in hub.requests:
        headers = {k.lower(): v for k, v in request.header_items()}
        assert headers["user-agent"] == USER_AGENT
        assert USER_AGENT.startswith("sticks-ingest/1.0")
        assert headers["accept"] == "application/json"


# ---------------------------------------------------------------------------
# (c) anonymous pagination 403 is a normal end
# ---------------------------------------------------------------------------


def test_pagination_403_ends_the_query_without_retry_or_failure(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    rows = [(f"acme/app{i:03d}", "An app", 1_000_000) for i in range(100)]

    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH:
            return 200, official_page(["nginx"]), {}
        if query["page"] == "1":
            return 200, search_page(rows), {}
        return 403, {"message": PAGINATION_MESSAGE}, {}

    hub = _install(monkeypatch, handler)
    fetcher = _fetcher(sleeper, search_terms=("app",))
    entries = list(fetcher.fetch_entries(max_images=10_000))

    assert len(entries) == 101
    assert fetcher.warnings == []
    search_requests = [r for r in hub.requests if SEARCH_PATH in r.full_url]
    assert len(search_requests) == 2  # page 2 asked exactly once (not retried)
    assert sleeper.waits == []


def test_empty_page_is_a_normal_end(monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH:
            return 200, official_page(["nginx"]), {}
        return 200, search_page([]), {}

    _install(monkeypatch, handler)
    fetcher = _fetcher(sleeper, search_terms=("nothing",))
    assert [e.image for e in fetcher.fetch_entries(max_images=10)] == ["nginx"]
    assert fetcher.warnings == []


# ---------------------------------------------------------------------------
# (d) 429 then success, (e) persistent 5xx
# ---------------------------------------------------------------------------


def _flaky(failures: int, status: int, headers: dict[str, str]) -> Handler:
    state = {"left": failures}

    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if state["left"] > 0:
            state["left"] -= 1
            return status, {"message": "slow down"}, headers
        return 200, official_page(["nginx"]), {}

    return handler


def test_429_without_retry_after_backs_off_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    hub = _install(monkeypatch, _flaky(2, 429, {}))
    entries = list(_fetcher(sleeper).fetch_entries(max_images=10))

    assert [e.image for e in entries] == ["nginx"]
    assert sleeper.waits == [1.0, 2.0]  # exponential backoff (jitter pinned to 0)
    assert len(hub.requests) == 4  # 3 for the first query, 1 for the second


def test_429_with_retry_after_waits_the_indicated_time(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    _install(monkeypatch, _flaky(1, 429, {"Retry-After": "7"}))
    entries = list(_fetcher(sleeper).fetch_entries(max_images=10))

    assert [e.image for e in entries] == ["nginx"]
    assert sleeper.waits == [7.0]


def test_429_with_ratelimit_reset_epoch_uses_the_injected_clock(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    reset = str(int(FIXED_NOW) + 12)
    _install(monkeypatch, _flaky(1, 429, {"x-ratelimit-reset": reset}))
    list(_fetcher(sleeper).fetch_entries(max_images=10))

    assert sleeper.waits == [12.0]


def test_persistent_5xx_raises_fetch_error_after_four_attempts(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        return 503, {"message": "upstream unavailable"}, {}

    hub = _install(monkeypatch, handler)
    fetcher = _fetcher(sleeper)
    with pytest.raises(DockerCatalogFetchError) as info:
        list(fetcher.fetch_entries(max_images=10))

    error = info.value
    assert error.status == 503
    assert error.url == OFFICIAL_PATH
    assert error.message == "upstream unavailable"
    assert "HTTP 503" in str(error) and OFFICIAL_PATH in str(error)
    # 4 attempts per query, 3 failing queries (official x2 ...) then stop: here
    # only 2 queries exist (no search terms), so 8 requests in total.
    assert len(hub.requests) == 8
    assert sleeper.waits == [1.0, 2.0, 4.0] * 2


def test_non_retryable_client_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        return 404, {"message": "not found"}, {}

    hub = _install(monkeypatch, handler)
    with pytest.raises(DockerCatalogFetchError) as info:
        list(_fetcher(sleeper).fetch_entries(max_images=10))
    assert info.value.status == 404
    assert len(hub.requests) == 2  # one attempt per query


# ---------------------------------------------------------------------------
# (f) partial failure, (g) filters
# ---------------------------------------------------------------------------


def test_partial_failure_keeps_images_and_records_a_warning(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH:
            return 200, official_page(["nginx", "redis"]), {}
        if query["query"] == "bad":
            return 400, {"message": "bad query"}, {}
        return 200, search_page([("acme/good", "Good image", 900_000)]), {}

    _install(monkeypatch, handler)
    fetcher = _fetcher(sleeper, search_terms=("bad", "good"))
    entries = list(fetcher.fetch_entries(max_images=100))

    assert [e.image for e in entries] == ["nginx", "redis", "acme/good"]
    assert len(fetcher.warnings) == 1
    assert fetcher.warnings[0]["item"] == "search bad"
    assert "HTTP 400" in fetcher.warnings[0]["reason"]


def test_fetch_stops_after_three_failed_queries(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH and query["ordering"] == "name":
            return 200, official_page(["nginx"]), {}
        return 400, {"message": "boom"}, {}

    hub = _install(monkeypatch, handler)
    fetcher = _fetcher(sleeper, search_terms=("a", "b", "c", "d", "e"))
    entries = list(fetcher.fetch_entries(max_images=100))

    assert [e.image for e in entries] == ["nginx"]
    assert len(fetcher.warnings) == 3
    assert len(hub.requests) == 1 + 3  # one success + three failed queries, no more


def test_no_image_at_all_raises(monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        return 400, {"message": "nope"}, {}

    _install(monkeypatch, handler)
    with pytest.raises(DockerCatalogFetchError):
        list(_fetcher(sleeper, search_terms=("a",)).fetch_entries(max_images=10))


def test_search_filters_drop_missing_description_and_low_pull_counts(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH:
            return 200, official_page([]), {}
        return (
            200,
            search_page(
                [
                    ("acme/popular", "Popular", 100_000),
                    ("acme/unpopular", "Unpopular", 99_999),
                    ("acme/nodesc", "", 5_000_000),
                    ("acme/blank", "   ", 5_000_000),
                ]
            ),
            {},
        )

    _install(monkeypatch, handler)
    monkeypatch.delenv("DOCKER_CATALOG_MIN_PULLS", raising=False)
    fetcher = _fetcher(sleeper, search_terms=("x",))
    assert [e.image for e in fetcher.fetch_entries(max_images=10)] == ["acme/popular"]


def test_min_pulls_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH:
            return 200, official_page([]), {}
        return 200, search_page([("acme/a", "A", 600), ("acme/b", "B", 400)]), {}

    _install(monkeypatch, handler)
    monkeypatch.setenv("DOCKER_CATALOG_MIN_PULLS", "500")
    fetcher = _fetcher(sleeper, search_terms=("x",))
    assert [e.image for e in fetcher.fetch_entries(max_images=10)] == ["acme/a"]


def test_official_entry_without_description_is_dropped(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    page = official_page(["nginx", "scratch"])
    page["results"][1]["description"] = None

    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        return 200, page, {}

    _install(monkeypatch, handler)
    assert [e.image for e in _fetcher(sleeper).fetch_entries(max_images=10)] == ["nginx"]


def test_max_images_cap_is_honored_by_the_fetcher(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper
) -> None:
    _install(monkeypatch, _official_only([f"i{n}" for n in range(10)]))
    assert len(list(_fetcher(sleeper).fetch_entries(max_images=3))) == 3


# ---------------------------------------------------------------------------
# Service + orchestrator on a temporary SQLite database
# ---------------------------------------------------------------------------


class _Embedder:
    def embed(self, text: str) -> list[float]:
        value = float(len(text) % 7)
        return [value, value, value]


@pytest.fixture()
def factory() -> Any:
    tmp_dir = Path(tempfile.mkdtemp(prefix="docker_hub_fetcher_"))
    engine = create_db_engine(
        Settings(database_url=f"sqlite:///{(tmp_dir / 'db.sqlite').as_posix()}")
    )
    import_models()
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        yield session_factory
    finally:
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _count(factory: Any, model: Any) -> int:
    with factory() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _status(orchestrator: IngestionOrchestrator) -> Any:
    return next(s for s in orchestrator.status() if s.source == IngestSource.DOCKER_CATALOG)


def test_service_ingest_with_partial_failure_reports_warning_and_keeps_images(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper, factory: Any
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH and query["ordering"] == "name":
            return 200, official_page(["nginx", "redis"]), {}
        return 400, {"message": "boom"}, {}

    _install(monkeypatch, handler)
    with factory() as session:
        service = DockerCatalogIngestService(
            session,
            memory=ExperienceMemory(session, embedder=_Embedder()),
            fetcher=_fetcher(sleeper),
        )
        summary = service.ingest()
        session.commit()

    assert summary.entries_seen == 2
    assert len(summary.warnings) == 1
    assert _count(factory, ImageCatalog) == 2


def test_orchestrator_partial_failure_finishes_ready_with_skipped(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper, factory: Any
) -> None:
    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if path == OFFICIAL_PATH and query["ordering"] == "name":
            return 200, official_page(["nginx"]), {}
        return 400, {"message": "boom"}, {}

    _install(monkeypatch, handler)
    monkeypatch.setenv("DOCKER_CATALOG_MIN_IMAGES", "1")
    orchestrator = IngestionOrchestrator(
        factory,
        {
            IngestSource.DOCKER_CATALOG: DockerCatalogSourceRunner(
                embedder=_Embedder(), fetcher=_fetcher(sleeper)
            )
        },
    )
    orchestrator.run_source(IngestSource.DOCKER_CATALOG)

    state = _status(orchestrator)
    assert state.status == IngestStatus.READY
    assert state.error is None
    assert state.skipped_count == 1
    assert _count(factory, ImageCatalog) == 1


def test_failed_run_records_original_cause_preserves_rows_and_resumes(
    monkeypatch: pytest.MonkeyPatch, sleeper: Sleeper, factory: Any
) -> None:
    mode = {"names": ["a1", "a2"], "down": False}

    def handler(path: str, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if mode["down"]:
            return 429, {"message": "rate limited"}, {}
        return 200, official_page(mode["names"]), {}  # type: ignore[arg-type] -- test double stands in for the real collaborator

    _install(monkeypatch, handler)
    monkeypatch.setenv("DOCKER_CATALOG_MIN_IMAGES", "2")
    orchestrator = IngestionOrchestrator(
        factory,
        {
            IngestSource.DOCKER_CATALOG: DockerCatalogSourceRunner(
                embedder=_Embedder(), fetcher=_fetcher(sleeper)
            )
        },
    )
    orchestrator.run_source(IngestSource.DOCKER_CATALOG)
    assert _status(orchestrator).status == IngestStatus.READY
    assert _count(factory, ImageCatalog) == 2

    # The floor rises: the catalog is incomplete and the network is down.
    monkeypatch.setenv("DOCKER_CATALOG_MIN_IMAGES", "4")
    mode["down"] = True
    orchestrator.run_source(IngestSource.DOCKER_CATALOG)

    failed = _status(orchestrator)
    assert failed.status == IngestStatus.FAILED
    assert failed.error is not None
    assert failed.error.startswith(
        "network: DockerCatalogFetchError: HTTP 429 on /v2/repositories/library/"
    )
    assert len(failed.error) <= 300
    assert _count(factory, ImageCatalog) == 2  # loaded rows preserved

    # Resume: the network is back and more images exist.
    mode["down"] = False
    mode["names"] = ["a1", "a2", "a3", "a4"]
    orchestrator.run_source(IngestSource.DOCKER_CATALOG)

    ready = _status(orchestrator)
    assert ready.status == IngestStatus.READY
    assert ready.error is None
    assert _count(factory, ImageCatalog) == 4
    assert _count(factory, ExperienceMemoryRow) == 4


def test_describe_failure_includes_cause_and_is_capped() -> None:
    error = DockerCatalogFetchError(
        "HTTP 429 on /v2/repositories/library/ (official ordering=name, page 1): " + "x" * 400,
        status=429,
        url="/v2/repositories/library/",
    )
    text = describe_failure(error)
    assert text.startswith(
        "network: DockerCatalogFetchError: HTTP 429 on /v2/repositories/library/"
    )
    assert len(text) == 300
