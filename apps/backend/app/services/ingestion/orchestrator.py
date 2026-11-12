"""Ingestion orchestrator: keeps the knowledge sources loaded and embedded.

One :class:`SourceRunner` per :class:`~app.models.enums.IngestSource`. A runner
computes a :class:`ReconcilePlan` from the database only (what is missing) and
executes just that plan. The orchestrator owns everything around it:

* **State and lock** in ``ingest_source_state``. A source moves to ``running``
  through a single ``UPDATE ... WHERE`` that only matches when the row is not
  ``running`` or its ``heartbeat_at`` is older than ``stale_after``; the claim
  succeeds only if ``rowcount == 1`` (Req. 7.10). The heartbeat is refreshed on
  every progress callback.
* **Empty plan means ``ready`` without any network access** (Req. 7.3).
* **Sequential execution** ``stix_packages -> curated_seed -> atomic ->
  docker_catalog -> attack_flow`` in one
  background thread (Req. 7.1). Each source has its own ``try/except`` and its
  own session, so one failure never stops the others (Req. 7.6).
* **Failure causes** are short and secret-free: ``network: <ExceptionClass>``
  for connectivity problems, the embedder error code for Ollama problems, and
  ``error: <ExceptionClass>`` otherwise. Partial counts are kept and the next
  run recomputes the plan, so only what is still missing is resumed (Req. 7.7).

Importing this module performs no I/O and starts no thread.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
import socket
import threading
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol

import httpx
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import IngestSource, IngestStatus

logger = logging.getLogger(__name__)

# Canonical execution order (design section 7).
SOURCE_ORDER: tuple[IngestSource, ...] = (
    IngestSource.STIX_PACKAGES,
    IngestSource.CURATED_SEED,
    IngestSource.ATOMIC,
    IngestSource.DOCKER_CATALOG,
    IngestSource.ATTACK_FLOW,
)

DEFAULT_STALE_AFTER = dt.timedelta(minutes=15)

# ``progress(done, total)``: ``total`` is the expected number of items of the
# source when the runner knows it (for example the corpus listing size), else 0.
ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class ReconcilePlan:
    """What a source still needs, computed from the database only.

    ``needs_fetch`` means the network is required (the source is empty or the
    stored data is known to be incomplete). ``missing_embed`` counts stored
    items without an embedding (vectorize-only work, no download).
    ``missing_rows`` counts rows an embedded seed still has to insert.
    """

    source: IngestSource
    needs_fetch: bool = False
    missing_embed: int = 0
    loaded_count: int = 0
    embedded_count: int = 0
    expected_count: Optional[int] = None
    # Rows an embedded (offline) source still has to write; no network involved.
    missing_rows: int = 0

    @property
    def is_empty(self) -> bool:
        return (
            not self.needs_fetch and self.missing_embed == 0 and self.missing_rows == 0
        )


@dataclass(frozen=True)
class RunOutcome:
    """Optional extra information a runner hands back after ``run``."""

    skipped: list[dict[str, str]] = field(default_factory=list)


class SourceRunner(Protocol):
    """Adapter between one knowledge source and the orchestrator."""

    source: IngestSource

    def plan(self, session: Session) -> ReconcilePlan:
        """Compare the database with what is expected; no network access."""
        ...

    def run(
        self, session: Session, plan: ReconcilePlan, progress: ProgressCallback
    ) -> Optional[RunOutcome]:
        """Execute only what ``plan`` says is missing. Does not commit."""
        ...

    def counts(self, session: Session) -> tuple[int, int]:
        """Cheap ``(loaded_count, embedded_count)`` for state reporting."""
        ...


class StartResult(str, enum.Enum):
    STARTED = "started"
    ALREADY_RUNNING = "already_running"


@dataclass(frozen=True)
class SourceStatus:
    """Read model of one ``ingest_source_state`` row (Req. 7.8)."""

    source: IngestSource
    status: IngestStatus
    loaded_count: Optional[int] = None
    embedded_count: Optional[int] = None
    expected_count: Optional[int] = None
    error: Optional[str] = None
    skipped_count: int = 0
    updated_at: Optional[dt.datetime] = None


_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    urllib.error.URLError,
    socket.timeout,
    ConnectionError,
    TimeoutError,
)


def _network_error_types() -> tuple[type[BaseException], ...]:
    """Network error classes, including the ingest-specific fetch errors."""
    from app.services.atomic_red_team_ingest_service import AtomicFetchError
    from app.services.attack_flow.corpus_fetcher import CorpusFetchError
    from app.services.docker_catalog_ingest_service import DockerCatalogFetchError
    from app.services.stix.stix_service import StixDownloadError

    return _NETWORK_ERRORS + (
        AtomicFetchError,
        DockerCatalogFetchError,
        CorpusFetchError,
        StixDownloadError,
    )


def describe_failure(exc: BaseException) -> str:
    """Short, secret-free failure cause stored in ``ingest_source_state.error``."""
    from app.services.ai.embedder import EmbedderError

    if isinstance(exc, EmbedderError):
        return exc.code
    if isinstance(exc, _network_error_types()):
        return f"network: {type(exc).__name__}"
    return f"error: {type(exc).__name__}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class IngestionOrchestrator:
    """Verifies, schedules and runs the knowledge-source ingestions."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        sources: Mapping[IngestSource, SourceRunner],
        *,
        stale_after: dt.timedelta = DEFAULT_STALE_AFTER,
        clock: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._sources = dict(sources)
        self._stale_after = stale_after
        self._clock = clock
        self._thread_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    # ---- public API ---------------------------------------------------------

    def start_background(self) -> None:
        """Run every source sequentially in a daemon thread; never blocks."""
        with self._thread_lock:
            if any(t.name == "ingestion-all" and t.is_alive() for t in self._threads):
                return
            thread = threading.Thread(
                target=self.run_all, name="ingestion-all", daemon=True
            )
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
            thread.start()

    def run_all(self) -> None:
        """Synchronously run every registered source in the canonical order."""
        self._ensure_rows()
        for source in self._ordered_sources():
            try:
                if self._claim(source):
                    self._execute(source)
            except Exception:  # noqa: BLE001 - one source never stops the others
                logger.exception("ingestion of %s aborted unexpectedly", source.value)

    def verify_all(self) -> dict[IngestSource, SourceStatus]:
        """Recompute every plan (database only) and refresh the stored state."""
        statuses, _ = self._verify_all()
        return statuses

    def verify_and_start(self) -> dict[IngestSource, SourceStatus]:
        """``verify_all`` and, when something is missing, start the background run."""
        statuses, pending = self._verify_all()
        if pending:
            self.start_background()
        return statuses

    def start(self, source: IngestSource) -> StartResult:
        """Claim ``source`` and run it in a background thread."""
        source = IngestSource(source)
        if source not in self._sources:
            raise ValueError(f"no runner registered for source {source.value!r}")
        self._ensure_rows()
        if not self._claim(source):
            return StartResult.ALREADY_RUNNING
        thread = threading.Thread(
            target=self._execute_safely,
            args=(source,),
            name=f"ingestion-{source.value}",
            daemon=True,
        )
        with self._thread_lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()
        return StartResult.STARTED

    def run_source(self, source: IngestSource) -> StartResult:
        """Synchronous variant of :meth:`start` (used by tests and scripts)."""
        source = IngestSource(source)
        self._ensure_rows()
        if not self._claim(source):
            return StartResult.ALREADY_RUNNING
        self._execute(source)
        return StartResult.STARTED

    def runner(self, source: IngestSource) -> SourceRunner:
        """The runner registered for ``source`` (``KeyError`` when absent)."""
        return self._sources[IngestSource(source)]

    def status(self) -> list[SourceStatus]:
        """Persisted state of every registered source, in canonical order."""
        self._ensure_rows()
        with self._session_factory() as session:
            return [self._snapshot(session, source) for source in self._ordered_sources()]

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for the background threads (mainly for tests)."""
        with self._thread_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    # ---- state helpers ------------------------------------------------------

    def _ordered_sources(self) -> list[IngestSource]:
        return [s for s in SOURCE_ORDER if s in self._sources]

    def _ensure_rows(self) -> None:
        from app.models.domain import IngestSourceState

        with self._session_factory() as session:
            missing = [
                s
                for s in SOURCE_ORDER
                if session.get(IngestSourceState, s) is None
            ]
            if not missing:
                return
            for source in missing:
                session.add(IngestSourceState(source=source, status=IngestStatus.PENDING))
            try:
                session.commit()
            except IntegrityError:  # another process created them first
                session.rollback()

    def _stale_threshold(self) -> dt.datetime:
        return self._clock() - self._stale_after

    def _claim(self, source: IngestSource) -> bool:
        """Atomic ``-> running`` transition; True only when ``rowcount == 1``."""
        from app.models.domain import IngestSourceState as State

        now = self._clock()
        statement = (
            update(State)
            .where(
                State.source == source,
                or_(
                    State.status != IngestStatus.RUNNING,
                    State.heartbeat_at.is_(None),
                    State.heartbeat_at < self._stale_threshold(),
                ),
            )
            .values(
                status=IngestStatus.RUNNING,
                started_at=now,
                heartbeat_at=now,
                finished_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        with self._session_factory() as session:
            result = session.execute(statement)
            session.commit()
            return result.rowcount == 1

    def _snapshot(self, session: Session, source: IngestSource) -> SourceStatus:
        from app.models.domain import IngestSourceState

        session.expire_all()
        row = session.get(IngestSourceState, source)
        if row is None:
            return SourceStatus(source=source, status=IngestStatus.PENDING)
        stamps = [t for t in (row.heartbeat_at, row.finished_at, row.started_at) if t]
        return SourceStatus(
            source=source,
            status=row.status,
            loaded_count=row.loaded_count,
            embedded_count=row.embedded_count,
            expected_count=row.expected_count,
            error=row.error,
            skipped_count=len(row.skipped) if isinstance(row.skipped, list) else 0,
            updated_at=max(stamps) if stamps else None,
        )

    def _write_state(self, source: IngestSource, **values: Any) -> None:
        """Update the state row in its own short transaction."""
        from app.models.domain import IngestSourceState as State

        with self._session_factory() as session:
            session.execute(
                update(State)
                .where(State.source == source)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            session.commit()

    # ---- verification -------------------------------------------------------

    def _verify_all(self) -> tuple[dict[IngestSource, SourceStatus], bool]:
        self._ensure_rows()
        pending = False
        for source in self._ordered_sources():
            try:
                if not self._verify(source):
                    pending = True
            except Exception as exc:  # noqa: BLE001 - verification must not raise
                logger.exception("verification of %s failed", source.value)
                self._write_state(
                    source, status=IngestStatus.FAILED, error=f"plan: {type(exc).__name__}"
                )
        with self._session_factory() as session:
            statuses = {s: self._snapshot(session, s) for s in self._ordered_sources()}
        return statuses, pending

    def _verify(self, source: IngestSource) -> bool:
        """Refresh one source; returns True when nothing is missing."""
        from app.models.domain import IngestSourceState

        runner = self._sources[source]
        with self._session_factory() as session:
            state = session.get(IngestSourceState, source)
            now = self._clock()
            running = (
                state is not None
                and state.status == IngestStatus.RUNNING
                and state.heartbeat_at is not None
                and state.heartbeat_at >= self._stale_threshold()
            )
            plan = runner.plan(session)
            if running:
                return plan.is_empty
            if state is None:
                return plan.is_empty
            state.loaded_count = plan.loaded_count
            state.embedded_count = plan.embedded_count
            if plan.expected_count is not None:
                state.expected_count = plan.expected_count
            if plan.is_empty:
                if state.status != IngestStatus.READY:
                    state.finished_at = now
                state.status = IngestStatus.READY
                state.error = None
            elif state.status == IngestStatus.RUNNING:
                # Orphan of a dead process: resumable, not running.
                state.status = IngestStatus.FAILED
                state.error = "interrupted"
            session.commit()
            return plan.is_empty

    # ---- execution ----------------------------------------------------------

    def _execute_safely(self, source: IngestSource) -> None:
        try:
            self._execute(source)
        except Exception:  # noqa: BLE001
            logger.exception("ingestion of %s aborted unexpectedly", source.value)

    def _execute(self, source: IngestSource) -> None:
        """Run an already claimed source and persist the final state."""
        runner = self._sources[source]
        session = self._session_factory()
        failure: Optional[str] = None
        outcome: Optional[RunOutcome] = None
        plan: Optional[ReconcilePlan] = None
        try:
            plan = runner.plan(session)
            if not plan.is_empty:
                def progress(done: int, total: int) -> None:
                    session.commit()  # persist partial work before the heartbeat
                    values: dict[str, Any] = {"heartbeat_at": self._clock()}
                    loaded, embedded = runner.counts(session)
                    values["loaded_count"] = loaded
                    values["embedded_count"] = embedded
                    if total and total > 0:
                        values["expected_count"] = total
                    self._write_state(source, **values)

                outcome = runner.run(session, plan, progress)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - isolate this source
            failure = describe_failure(exc)
            logger.warning("ingestion of %s failed: %s", source.value, failure)
            try:
                session.commit()  # keep what was already flushed
            except Exception:  # noqa: BLE001
                session.rollback()

        values: dict[str, Any] = {"finished_at": self._clock()}
        try:
            loaded, embedded = runner.counts(session)
            values["loaded_count"] = loaded
            values["embedded_count"] = embedded
        except Exception:  # noqa: BLE001 - keep the last persisted counts
            session.rollback()
        finally:
            session.close()
        if outcome is not None:
            values["skipped"] = list(outcome.skipped)
        values["status"] = IngestStatus.FAILED if failure else IngestStatus.READY
        values["error"] = failure
        self._write_state(source, **values)


__all__ = [
    "DEFAULT_STALE_AFTER",
    "IngestionOrchestrator",
    "ReconcilePlan",
    "RunOutcome",
    "SOURCE_ORDER",
    "SourceRunner",
    "SourceStatus",
    "StartResult",
    "describe_failure",
]
