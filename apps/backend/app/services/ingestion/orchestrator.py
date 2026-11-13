"""Ingestion orchestrator: keeps the knowledge sources loaded and embedded.

One :class:`SourceRunner` per :class:`~app.models.enums.IngestSource`. A runner
computes a :class:`ReconcilePlan` from the database only (what is missing) and
executes just that plan. The orchestrator owns everything around it:

* **State and lock** in ``ingest_source_state``. A source moves to ``running``
  through a single compare-and-swap ``UPDATE ... WHERE`` that only matches the
  row exactly as it was read; the claim succeeds only if ``rowcount == 1``
  (Req. 7.10). A ``running`` row is claimable when its ``heartbeat_at`` is older
  than ``stale_after`` or when its *owner* is gone: while running, the ``error``
  column carries ``run:<host>:<pid>:<boot-id>|<previous error>`` so a restarted
  server (new pid/boot id) reclaims the orphan of the previous process at once.
* **Heartbeat.** A ticker thread refreshes ``heartbeat_at`` every few seconds for
  the whole duration of a run (download, parse, persistence, embedding), and
  every progress callback refreshes it too. A zombie run that lost its lock stops
  at the next progress call and never overwrites the new owner's state.
* **One cycle per process.** ``start_background`` / ``verify_and_start`` never
  start a second cycle while one is alive; ``verify`` only schedules what is
  missing.
* **Empty plan means ``ready`` without any network access** (Req. 7.3).
* **Sequential execution** ``stix_packages -> curated_seed -> atomic ->
  docker_catalog -> attack_flow`` in one
  background thread (Req. 7.1). Each source has its own ``try/except`` and its
  own sessions (the plan runs in a short session that is closed before any
  network I/O), so one failure never stops the others (Req. 7.6). Whatever
  happens, the final state is written in a ``finally`` and is never ``running``.
* **Failure causes** are short and secret-free: ``network: <ExceptionClass>``
  for connectivity problems, the embedder error code for Ollama problems, and
  ``error: <ExceptionClass>`` otherwise. Partial counts are kept and the next
  run recomputes the plan, so only what is still missing is resumed (Req. 7.7).

Importing this module performs no I/O and starts no thread.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import enum
import logging
import os
import socket
import threading
import urllib.error
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from sqlalchemy import select, update
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

# A ``running`` source whose heartbeat is older than this is reclaimed. The ticker
# refreshes it every ``DEFAULT_HEARTBEAT_INTERVAL`` seconds, so a live run never
# gets close to the limit while a dead one is picked up quickly.
DEFAULT_STALE_AFTER = dt.timedelta(minutes=2)
DEFAULT_HEARTBEAT_INTERVAL = 20.0

_RUN_PREFIX = "run:"

# ``progress(done, total)``: ``total`` is the expected number of items of the
# source when the runner knows it (for example the corpus listing size), else 0.
ProgressCallback = Callable[[int, int], None]


def make_owner_token() -> str:
    """Identifier of this process boot: ``host:pid:boot-id``."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def split_run_marker(error: str | None) -> tuple[str | None, str | None]:
    """``(owner, previous_error)`` of a stored ``error`` (owner is None when absent)."""
    if error is None or not error.startswith(_RUN_PREFIX):
        return None, error
    owner, _, previous = error[len(_RUN_PREFIX) :].partition("|")
    return owner, (previous or None)


def strip_run_marker(error: str | None) -> str | None:
    """The failure cause stored in ``error`` without the running-owner marker."""
    return split_run_marker(error)[1]


def _pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists on this host."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined] -- ctypes.windll exists only on Windows and is guarded by os.name == 'nt'
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class OwnershipLostError(Exception):
    """The source was reclaimed by another run; this (zombie) run must stop."""


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
    expected_count: int | None = None
    # Rows an embedded (offline) source still has to write; no network involved.
    missing_rows: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.needs_fetch and self.missing_embed == 0 and self.missing_rows == 0


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
    ) -> RunOutcome | None:
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
    loaded_count: int | None = None
    embedded_count: int | None = None
    expected_count: int | None = None
    error: str | None = None
    skipped_count: int = 0
    updated_at: dt.datetime | None = None


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

    return (
        *_NETWORK_ERRORS,
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
    from app.services.atomic_red_team_ingest_service import ImplausibleAtomicCountError

    if isinstance(exc, ImplausibleAtomicCountError):
        return exc.code
    if isinstance(exc, _network_error_types()):
        from app.services.atomic_red_team_ingest_service import AtomicFetchError
        from app.services.docker_catalog_ingest_service import DockerCatalogFetchError

        detailed = isinstance(exc, (DockerCatalogFetchError, AtomicFetchError))
        detail = str(exc).strip() if detailed else ""
        if detail:
            # Original cause (HTTP status, URL path, API message); no secrets.
            return f"network: {type(exc).__name__}: {detail}"[:300]
        return f"network: {type(exc).__name__}"
    return f"error: {type(exc).__name__}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class IngestionOrchestrator:
    """Verifies, schedules and runs the knowledge-source ingestions."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        sources: Mapping[IngestSource, SourceRunner],
        *,
        stale_after: dt.timedelta = DEFAULT_STALE_AFTER,
        clock: Callable[[], dt.datetime] = _utcnow,
        owner: str | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._session_factory = session_factory
        self._sources = dict(sources)
        self._stale_after = stale_after
        self._clock = clock
        self._owner = owner or make_owner_token()
        self._heartbeat_interval = heartbeat_interval
        self._thread_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._cycle: threading.Thread | None = None
        # Sources claimed by this process whose run has not finished yet.
        self._active: set[IngestSource] = set()

    # ---- public API ---------------------------------------------------------

    def start_background(self) -> bool:
        """Run every source sequentially in a daemon thread; never blocks.

        Idempotent: at most one cycle exists per orchestrator (process). Returns
        True when a new cycle was started, False when one is already alive.
        """
        with self._thread_lock:
            if self._cycle is not None and self._cycle.is_alive():
                return False
            thread = threading.Thread(
                target=self._run_all_safely, name="ingestion-all", daemon=True
            )
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
            self._cycle = thread
            thread.start()
            return True

    def run_all(self) -> None:
        """Synchronously run every registered source in the canonical order."""
        self._ensure_rows()
        for source in self._ordered_sources():
            try:
                if self._claim(source):
                    self._execute(source)
            except Exception:
                logger.exception("ingestion of %s aborted unexpectedly", source.value)

    def verify_all(self) -> dict[IngestSource, SourceStatus]:
        """Recompute every plan (database only) and refresh the stored state."""
        statuses, _ = self._verify_all()
        return statuses

    def verify_and_start(self) -> dict[IngestSource, SourceStatus]:
        """``verify_all`` and, when something is missing, ensure a background cycle.

        Idempotent: with a cycle already alive nothing new is started (the cycle
        itself reaches whatever is still missing).
        """
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
        try:
            thread.start()
        except BaseException:
            self._discard_active(source)
            self._write_state(source, status=IngestStatus.FAILED, error="interrupted")
            raise
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

    def join(self, timeout: float | None = None) -> None:
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
            missing = [s for s in SOURCE_ORDER if session.get(IngestSourceState, s) is None]
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

    def _discard_active(self, source: IngestSource) -> None:
        with self._thread_lock:
            self._active.discard(source)

    def _is_orphan(self, source: IngestSource, error: str | None) -> bool:
        """True when the run recorded in ``error`` can no longer be alive.

        * no marker (rows written by older code): not provably an orphan, the
          heartbeat decides;
        * this process boot: an orphan unless a run of the source is active here
          (its thread died without writing the final state);
        * another boot on this host: an orphan when that pid is gone (the usual
          case after a server restart / ``--reload``); on another host only the
          heartbeat decides.
        """
        owner, _ = split_run_marker(error)
        if owner is None:
            return False
        if owner == self._owner:
            with self._thread_lock:
                return source not in self._active
        host, _, rest = owner.partition(":")
        pid_text = rest.partition(":")[0]
        if host == socket.gethostname() and pid_text.isdigit():
            return not _pid_alive(int(pid_text))
        return False

    def _marker(self, previous: str | None) -> str:
        return f"{_RUN_PREFIX}{self._owner}|{previous or ''}"

    def _claim(self, source: IngestSource) -> bool:
        """Atomic ``-> running`` transition; True only when ``rowcount == 1``.

        The row is read, judged claimable (not running, no heartbeat, stale
        heartbeat or orphaned owner) and then updated with a compare-and-swap on
        exactly the values that were read, so two concurrent claimers cannot both
        win.
        """
        from app.models.domain import IngestSourceState as State

        with self._session_factory() as session:
            row = session.execute(
                select(State.status, State.heartbeat_at, State.error).where(State.source == source)
            ).first()
        if row is None:
            return False
        status, heartbeat, error = row
        if (
            status == IngestStatus.RUNNING
            and heartbeat is not None
            and heartbeat >= self._stale_threshold()
            and not self._is_orphan(source, error)
        ):
            return False
        if status == IngestStatus.RUNNING:
            logger.warning(
                "reclaiming %s: previous run is stale or orphaned (owner=%s)",
                source.value,
                split_run_marker(error)[0] or "unknown",
            )
        now = self._clock()
        statement = (
            update(State)
            .where(
                State.source == source,
                State.status == status,
                State.heartbeat_at.is_(None)
                if heartbeat is None
                else State.heartbeat_at == heartbeat,
                State.error.is_(None) if error is None else State.error == error,
            )
            .values(
                status=IngestStatus.RUNNING,
                started_at=now,
                heartbeat_at=now,
                finished_at=None,
                error=self._marker(strip_run_marker(error)),
            )
            .execution_options(synchronize_session=False)
        )
        # Registered before the UPDATE so a concurrent verify never sees our
        # marker without an active run.
        with self._thread_lock:
            was_active = source in self._active
            self._active.add(source)
        won = False
        try:
            with self._session_factory() as session:
                result = session.execute(statement)
                session.commit()
                won = result.rowcount == 1
        finally:
            if not won and not was_active:
                self._discard_active(source)
        return won

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
            error=strip_run_marker(row.error),
            skipped_count=len(row.skipped) if isinstance(row.skipped, list) else 0,
            updated_at=max(stamps) if stamps else None,
        )

    def _write_state(
        self, source: IngestSource, *, only_owner: bool = False, **values: Any
    ) -> bool:
        """Update the state row in its own short transaction.

        With ``only_owner`` the update only applies while this process boot still
        holds the lock (``running`` with our marker); returns whether a row was
        updated.
        """
        from app.models.domain import IngestSourceState as State

        conditions = [State.source == source]
        if only_owner:
            conditions += [
                State.status == IngestStatus.RUNNING,
                State.error.like(f"{_RUN_PREFIX}{self._owner}|%"),
            ]
        with self._session_factory() as session:
            result = session.execute(
                update(State)
                .where(*conditions)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return result.rowcount == 1

    # ---- verification -------------------------------------------------------

    def _verify_all(self) -> tuple[dict[IngestSource, SourceStatus], bool]:
        self._ensure_rows()
        pending = False
        for source in self._ordered_sources():
            try:
                if not self._verify(source):
                    pending = True
            except Exception as exc:
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
        from app.models.domain import IngestSourceState as State

        runner = self._sources[source]
        with self._session_factory() as session:
            state = session.get(IngestSourceState, source)
            now = self._clock()
            running = (
                state is not None
                and state.status == IngestStatus.RUNNING
                and state.heartbeat_at is not None
                and state.heartbeat_at >= self._stale_threshold()
                and not self._is_orphan(source, state.error)
            )
            plan = runner.plan(session)
            if running:
                return plan.is_empty
            if state is None:
                return plan.is_empty
            values: dict[str, Any] = {
                "loaded_count": plan.loaded_count,
                "embedded_count": plan.embedded_count,
            }
            if plan.expected_count is not None:
                values["expected_count"] = plan.expected_count
            if plan.is_empty:
                if state.status != IngestStatus.READY:
                    values["finished_at"] = now
                values["status"] = IngestStatus.READY
                values["error"] = None
            elif state.status == IngestStatus.RUNNING:
                # Orphan of a dead process: resumable, not running.
                values["status"] = IngestStatus.FAILED
                values["error"] = "interrupted"
            # Compare-and-swap on the row as it was read: if a run claimed or
            # finished the source meanwhile, this stale verification must not
            # overwrite that newer state.
            observed = [
                State.source == source,
                State.status == state.status,
                State.heartbeat_at.is_(None)
                if state.heartbeat_at is None
                else State.heartbeat_at == state.heartbeat_at,
                State.error.is_(None) if state.error is None else State.error == state.error,
            ]
            session.rollback()
            session.execute(
                update(State)
                .where(*observed)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return plan.is_empty

    # ---- execution ----------------------------------------------------------

    def _run_all_safely(self) -> None:
        """Cycle entry point of the background thread: nothing escapes it."""
        try:
            logger.info("ingestion cycle started")
            self.run_all()
            logger.info("ingestion cycle finished")
        except BaseException:
            logger.exception("ingestion cycle aborted unexpectedly")

    def _execute_safely(self, source: IngestSource) -> None:
        try:
            self._execute(source)
        except BaseException:
            logger.exception("ingestion of %s aborted unexpectedly", source.value)

    def _heartbeat_loop(
        self, source: IngestSource, stop: threading.Event, lost: threading.Event
    ) -> None:
        while not stop.wait(self._heartbeat_interval):
            try:
                if not self._write_state(source, only_owner=True, heartbeat_at=self._clock()):
                    lost.set()
                    return
            except Exception:  # noqa: BLE001 -- a transient DB error must not kill the ticker
                logger.warning("heartbeat of %s could not be written", source.value)

    def _execute(self, source: IngestSource) -> None:
        """Run an already claimed source and persist the final state.

        The final state is written in a ``finally``: whatever happens (runner
        exception, lost lock, interpreter shutdown) the source never stays
        ``running`` because of this run.
        """
        runner = self._sources[source]
        stop = threading.Event()
        lost = threading.Event()
        ticker = threading.Thread(
            target=self._heartbeat_loop,
            args=(source, stop, lost),
            name=f"ingestion-heartbeat-{source.value}",
            daemon=True,
        )
        failure: str | None = "interrupted"
        outcome: RunOutcome | None = None
        session: Session | None = None
        ownership_lost = False
        started = self._clock()
        try:
            ticker.start()
            logger.info("ingestion of %s started", source.value)
            # The plan runs in its own short session, closed before any network
            # I/O, so no transaction stays open while downloading or embedding.
            with self._session_factory() as plan_session:
                plan = runner.plan(plan_session)
            if not plan.is_empty:
                session = self._session_factory()

                def progress(done: int, total: int) -> None:
                    if lost.is_set():
                        raise OwnershipLostError(source.value)
                    session.commit()  # persist partial work before the heartbeat
                    values: dict[str, Any] = {"heartbeat_at": self._clock()}
                    loaded, embedded = runner.counts(session)
                    values["loaded_count"] = loaded
                    values["embedded_count"] = embedded
                    if total and total > 0:
                        values["expected_count"] = total
                    if not self._write_state(source, only_owner=True, **values):
                        raise OwnershipLostError(source.value)

                outcome = runner.run(session, plan, progress)
                session.commit()
            failure = None
        except OwnershipLostError:
            ownership_lost = True
            failure = None
            logger.warning("ingestion of %s lost its lock; stopping", source.value)
            self._safe_rollback(session)
        except Exception as exc:
            failure = describe_failure(exc)
            logger.exception("ingestion of %s failed: %s", source.value, failure)
            if session is not None:
                try:
                    session.commit()  # keep what was already flushed
                except Exception:  # noqa: BLE001 -- a failed commit during failure handling is rolled back and must not mask the original error
                    self._safe_rollback(session)
        finally:
            stop.set()
            if ticker.is_alive():
                # Never leave a tick racing with the final state write.
                ticker.join(timeout=5.0)
            if session is not None:
                with contextlib.suppress(Exception):  # best-effort cleanup
                    session.close()
            try:
                if not ownership_lost:
                    self._finish(source, runner, failure, outcome)
                    logger.info(
                        "ingestion of %s finished: %s (%.1fs)",
                        source.value,
                        failure or "ready",
                        (self._clock() - started).total_seconds(),
                    )
            except Exception:
                logger.exception("could not write the final state of %s", source.value)
            finally:
                self._discard_active(source)

    @staticmethod
    def _safe_rollback(session: Session | None) -> None:
        if session is None:
            return
        with contextlib.suppress(Exception):  # best-effort cleanup
            session.rollback()

    def _finish(
        self,
        source: IngestSource,
        runner: SourceRunner,
        failure: str | None,
        outcome: RunOutcome | None,
    ) -> None:
        values: dict[str, Any] = {"finished_at": self._clock()}
        try:
            with self._session_factory() as session:
                loaded, embedded = runner.counts(session)
            values["loaded_count"] = loaded
            values["embedded_count"] = embedded
        except Exception:  # noqa: BLE001 -- keep the last persisted counts
            logger.warning("could not refresh the counts of %s", source.value)
        if outcome is not None:
            values["skipped"] = list(outcome.skipped)
        values["status"] = IngestStatus.FAILED if failure else IngestStatus.READY
        values["error"] = failure
        self._write_state(source, only_owner=True, **values)


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_STALE_AFTER",
    "SOURCE_ORDER",
    "IngestionOrchestrator",
    "OwnershipLostError",
    "ReconcilePlan",
    "RunOutcome",
    "SourceRunner",
    "SourceStatus",
    "StartResult",
    "describe_failure",
    "make_owner_token",
    "split_run_marker",
    "strip_run_marker",
]
