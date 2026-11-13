"""Deterministic ingest test for ``DockerCatalogIngestService`` (task 10.2).

Property 5 — Idempotência de ingestão.
**Validates: Requirements 3.2, 5.5**

One deterministic test module (no massive generation) exercising the three
observable facets of the Docker_Catalog_Ingest_Service contract against a
doubled, **no-network** fetcher:

* **Idempotent upsert (Req. 3.2, 5.5).** Ingesting the same two text-only
  descriptions twice persists exactly two ``image_catalog`` rows: the second
  run reuses the existing rows by ``(image, target_os)`` rather than
  duplicating them. The first run reports two inserts; the re-run reports zero
  inserts and two skips.
* **Caps (Req. 5.2).** Asserted by direct assertion — ``max_images`` stops the
  ingest after the Nth entry, and ``max_text_bytes`` stops it before an entry
  that would overflow the cumulative text budget. Both cases report
  ``capped is True``.
* **Best-effort vectorization (Req. 5.3, and the 5.5 idempotency it must not
  break).** When the injected embedder raises for every entry, vectorization
  fails for each entry but NEVER aborts the relational persistence: the
  ``image_catalog`` rows are still committed and idempotent on re-run.

Test infra
----------
The relational path uses an ephemeral temp-file SQLite ``Session`` (the same
``Base.metadata.create_all`` pattern as the sibling ingest tests). The fetcher
is a pure in-memory double that yields pre-built :class:`DockerCatalogEntry`
values and records no network access. The Experience_Memory is the real
:class:`~app.services.ai.memory.ExperienceMemory` wired to a deterministic
fake embedder (happy path) or a raising embedder (best-effort case) — never a
real embedding backend.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import ImageCatalog
from app.services.ai.memory import ExperienceMemory
from app.services.docker_catalog_ingest_service import (
    DockerCatalogEntry,
    DockerCatalogIngestService,
)

# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, tmp_dir, engine)`` so the caller can dispose the engine
    and remove the directory afterwards. Mirrors the temp-file SQLite +
    ``Base.metadata.create_all`` pattern of the sibling ingest tests.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="docker_catalog_ingest_"))
    db_path = tmp_dir / "docker_catalog_ingest.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), tmp_dir, engine


def _dispose(session: Session, engine: Any, tmp_dir: Path) -> None:
    """Close the session, dispose the engine and remove the temp dir."""
    session.close()
    engine.dispose()
    shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture()
def db_session() -> Any:
    """Yield an ephemeral SQLite session, disposed after the test."""
    session, tmp_dir, engine = _build_ephemeral_session()
    try:
        yield session
    finally:
        _dispose(session, engine, tmp_dir)


# ---------------------------------------------------------------------------
# Doubles: a no-network fetcher and deterministic / raising embedders.
# ---------------------------------------------------------------------------


class _FakeFetcher:
    """No-network ``DockerHubCatalogFetcher`` double yielding in-memory entries.

    Satisfies the ``fetch_entries(*, max_images) -> Iterable[DockerCatalogEntry]``
    contract by returning a pre-built list, honoring ``max_images`` exactly as
    the real fetcher would, and recording each call so a test can prove no
    extra fetching happens. No network, no image layers.
    """

    def __init__(self, entries: list[DockerCatalogEntry]) -> None:
        self._entries = entries
        self.calls: list[int] = []

    def fetch_entries(self, *, max_images: int) -> Iterable[DockerCatalogEntry]:
        self.calls.append(max_images)
        for index, entry in enumerate(self._entries):
            if index >= max_images:
                break
            yield entry


class _FakeEmbedder:
    """Deterministic ``Embedder`` double — a stable projection, never network.

    The best-effort vectorization path calls
    ``memory._resolve_embedder().embed(content)``; SQLite has no pgvector and we
    want no real embedding backend, so this fake returns a fixed-length vector
    derived deterministically from the text.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        value = float(len(text) % 7)
        return [value, value, value]


class _RaisingEmbedder:
    """An embedder whose :meth:`embed` always raises — models an unavailable backend.

    Exercises the best-effort guarantee (Req. 5.3): a Vector_Store/embedder
    failure for an entry must be isolated and must NOT abort the relational
    persistence already flushed for that entry.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        raise RuntimeError("embedding backend unreachable (simulated)")


# ---------------------------------------------------------------------------
# Fixtures: two text-only catalog entries and service factory.
# ---------------------------------------------------------------------------


def _two_entries() -> list[DockerCatalogEntry]:
    """Two distinct text-only entries keyed by ``(image, target_os)``."""
    return [
        DockerCatalogEntry(
            image="library/nginx",
            target_os="linux",
            description="Official build of nginx.",
            services=("http", "https"),
        ),
        DockerCatalogEntry(
            image="library/redis",
            target_os="linux",
            description="Redis is an in-memory data store.",
            services=("redis",),
        ),
    ]


def _count_rows(session: Session) -> int:
    """Count persisted ``image_catalog`` rows (the duplication check)."""
    return int(session.execute(select(func.count()).select_from(ImageCatalog)).scalar_one())


def _make_service(
    session: Session,
    entries: list[DockerCatalogEntry],
    *,
    embedder: Any,
) -> tuple[DockerCatalogIngestService, _FakeFetcher]:
    """Build the service over a no-network fetcher and the real ExperienceMemory."""
    fetcher = _FakeFetcher(entries)
    memory = ExperienceMemory(session, embedder=embedder)
    service = DockerCatalogIngestService(session, memory=memory, fetcher=fetcher)  # type: ignore[arg-type] -- test double stands in for the real collaborator
    return service, fetcher


# ===========================================================================
# Idempotent upsert — ingest twice, no duplicate rows (Req. 3.2, 5.5)
# ===========================================================================


def test_reingest_is_idempotent_no_duplicate_rows(db_session: Session) -> None:
    """Running the ingest twice over the same entries never duplicates rows.

    First run inserts the two ``(image, target_os)`` rows; the second run finds
    both already present and reuses them, so the table still holds exactly two
    rows. The summaries make the idempotency observable: 2 inserts / 0 skips on
    the first run, 0 inserts / 2 skips on the re-run (Req. 3.2, 5.5).
    """
    entries = _two_entries()
    embedder = _FakeEmbedder()

    service, _ = _make_service(db_session, entries, embedder=embedder)

    first = service.ingest()
    assert first.entries_seen == 2
    assert first.rows_inserted == 2
    assert first.rows_skipped == 0
    assert _count_rows(db_session) == 2

    # Re-run the SAME ingest: both rows already exist, so nothing is inserted.
    second = service.ingest()
    assert second.entries_seen == 2
    assert second.rows_inserted == 0
    assert second.rows_skipped == 2

    # The idempotency invariant: still exactly two rows, no duplicates.
    assert _count_rows(db_session) == 2


# ===========================================================================
# Re-execution through the ingestion orchestrator (Req. 4.4, 7.3, 7.5)
# ===========================================================================


def test_orchestrator_rerun_does_not_duplicate_rows_or_embeddings(
    db_session: Session,
) -> None:
    """Running the docker_catalog source twice via the orchestrator is idempotent.

    The second run finds nothing missing (empty plan), so it neither fetches
    again nor adds ``image_catalog`` or ``experience_memory`` rows.
    """
    from app.models.domain import ExperienceMemory as ExperienceMemoryRow
    from app.models.enums import IngestSource, IngestStatus
    from app.services.ingestion.orchestrator import IngestionOrchestrator
    from app.services.ingestion.runners import DockerCatalogSourceRunner

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, future=True)
    fetcher = _FakeFetcher(_two_entries())
    orchestrator = IngestionOrchestrator(
        factory,
        {
            IngestSource.DOCKER_CATALOG: DockerCatalogSourceRunner(
                embedder=_FakeEmbedder(), fetcher=fetcher
            )
        },
    )

    def _counts() -> tuple[int, int]:
        with factory() as session:
            return (
                _count_rows(session),
                int(
                    session.execute(
                        select(func.count()).select_from(ExperienceMemoryRow)
                    ).scalar_one()
                ),
            )

    orchestrator.run_source(IngestSource.DOCKER_CATALOG)
    first = _counts()
    assert first == (2, 2)

    orchestrator.run_source(IngestSource.DOCKER_CATALOG)
    assert _counts() == first
    assert len(fetcher.calls) == 1
    state = next(s for s in orchestrator.status() if s.source == IngestSource.DOCKER_CATALOG)
    assert state.status == IngestStatus.READY


# ===========================================================================
# Caps — direct assertion on max_images and max_text_bytes (Req. 5.2)
# ===========================================================================


def test_max_images_cap_stops_ingest_after_the_limit(db_session: Session) -> None:
    """``max_images=1`` persists only the first entry and reports capped (Req. 5.2).

    The fetcher is bounded by ``max_images`` and the service also guards the
    count, so only one of the two entries is seen and persisted.
    """
    entries = _two_entries()
    service, fetcher = _make_service(db_session, entries, embedder=_FakeEmbedder())

    summary = service.ingest(max_images=1)

    assert summary.entries_seen == 1
    assert summary.rows_inserted == 1
    assert _count_rows(db_session) == 1
    # The fetcher was asked for at most one image — the cap propagated.
    assert fetcher.calls == [1]


def test_max_text_bytes_cap_stops_before_overflowing_entry(
    db_session: Session,
) -> None:
    """``max_text_bytes`` stops before the entry that would overflow it (Req. 5.2).

    With a budget just large enough for the first entry's text but not the
    second, the ingest persists one row, stops before the second, and reports
    ``capped is True``.
    """
    entries = _two_entries()
    budget = entries[0].text_size()  # room for exactly the first entry

    service, _ = _make_service(db_session, entries, embedder=_FakeEmbedder())
    summary = service.ingest(max_text_bytes=budget)

    assert summary.entries_seen == 1
    assert summary.rows_inserted == 1
    assert summary.capped is True
    assert _count_rows(db_session) == 1


# ===========================================================================
# Best-effort vectorization — embedder failure must not abort persistence
# (Req. 5.3) and must not break idempotency (Req. 5.5)
# ===========================================================================


def test_vectorization_is_best_effort_and_preserves_relational_rows(
    db_session: Session,
) -> None:
    """An embedder that always raises never aborts the relational persistence.

    Every entry's vectorization fails (the embedder raises), yet both
    ``image_catalog`` rows are still persisted and counted; the summary reports
    the vector failures without any embeddings stored. Re-running stays
    idempotent — proving the failed vector writes did not corrupt the relational
    upsert (Req. 5.3, 5.5).
    """
    entries = _two_entries()
    raising = _RaisingEmbedder()

    service, _ = _make_service(db_session, entries, embedder=raising)
    summary = service.ingest()

    # Vectorization failed for both entries, but persistence is intact.
    assert summary.rows_inserted == 2
    assert summary.embeddings_stored == 0
    assert summary.vector_failures == 2
    assert len(summary.errors) == 2
    assert _count_rows(db_session) == 2
    # The embedder was actually exercised (best-effort attempted, not skipped).
    assert len(raising.calls) == 2

    # Idempotency survives the best-effort failures: re-run adds no rows.
    second = service.ingest()
    assert second.rows_inserted == 0
    assert second.rows_skipped == 2
    assert _count_rows(db_session) == 2


# ===========================================================================
# Runner plan — when the network is (not) required (task 39.3)
# ===========================================================================


class _ExplodingFetcher:
    """Fails the test if the plan-driven run touches the (fake) network."""

    def fetch_entries(self, *, max_images: int) -> Iterable[DockerCatalogEntry]:
        raise AssertionError("no network access expected")


def _plan_runner_and_orchestrator(db_session: Session, fetcher: Any) -> tuple[Any, Any]:
    from app.models.enums import IngestSource
    from app.services.ingestion.orchestrator import IngestionOrchestrator
    from app.services.ingestion.runners import DockerCatalogSourceRunner

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, future=True)
    runner = DockerCatalogSourceRunner(embedder=_FakeEmbedder(), fetcher=fetcher)
    orchestrator = IngestionOrchestrator(factory, {IngestSource.DOCKER_CATALOG: runner})
    return runner, orchestrator


def test_plan_fetches_when_the_catalog_is_empty(db_session: Session) -> None:
    runner, orchestrator = _plan_runner_and_orchestrator(db_session, _ExplodingFetcher())
    orchestrator.status()  # creates the state rows

    plan = runner.plan(db_session)

    assert plan.needs_fetch is True
    assert plan.loaded_count == 0
    assert plan.is_empty is False


def test_plan_fetches_below_the_floor_and_is_empty_when_complete(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.enums import IngestSource, IngestStatus

    monkeypatch.setenv("DOCKER_CATALOG_MIN_IMAGES", "2")
    fetcher = _FakeFetcher(_two_entries())
    runner, orchestrator = _plan_runner_and_orchestrator(db_session, fetcher)
    orchestrator.run_source(IngestSource.DOCKER_CATALOG)
    assert len(fetcher.calls) == 1

    # Complete and vectorized: nothing to do, and no network is needed.
    db_session.rollback()
    plan = runner.plan(db_session)
    assert plan.needs_fetch is False
    assert plan.missing_embed == 0
    assert plan.is_empty is True

    # Below the configured floor: the catalog is fetched again.
    db_session.rollback()
    monkeypatch.setenv("DOCKER_CATALOG_MIN_IMAGES", "3")
    below = runner.plan(db_session)
    assert below.needs_fetch is True
    assert below.loaded_count == 2

    # A previous failed state also resumes the download, even above the floor.
    monkeypatch.setenv("DOCKER_CATALOG_MIN_IMAGES", "2")
    from app.models.domain import IngestSourceState

    state = db_session.get(IngestSourceState, IngestSource.DOCKER_CATALOG)
    assert state is not None
    state.status = IngestStatus.FAILED
    state.error = "network: DockerCatalogFetchError: HTTP 503 on /v2/x"
    db_session.flush()
    assert runner.plan(db_session).needs_fetch is True
    db_session.rollback()


def test_empty_plan_run_does_not_use_the_network(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.enums import IngestSource, IngestStatus

    monkeypatch.setenv("DOCKER_CATALOG_MIN_IMAGES", "2")
    _, orchestrator = _plan_runner_and_orchestrator(db_session, _FakeFetcher(_two_entries()))
    orchestrator.run_source(IngestSource.DOCKER_CATALOG)

    # Same database, a fetcher that would fail the test if it were used.
    _, second = _plan_runner_and_orchestrator(db_session, _ExplodingFetcher())
    second.run_source(IngestSource.DOCKER_CATALOG)
    state = next(s for s in second.status() if s.source == IngestSource.DOCKER_CATALOG)
    assert state.status == IngestStatus.READY
