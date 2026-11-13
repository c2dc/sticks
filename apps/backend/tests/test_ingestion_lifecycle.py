"""Deterministic tests for the ingestion lifecycle (one cycle, heartbeat, orphans).

Covers the real-application failure of task 40.2: a source left ``running`` by a
process that died (``--reload``), concurrent ``verify`` calls and exceptions in
the background thread. Uses a fake runner and a temp-file SQLite database.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import IngestSourceState
from app.models.enums import IngestSource, IngestStatus
from app.services.ingestion.orchestrator import (
    IngestionOrchestrator,
    ReconcilePlan,
    RunOutcome,
    StartResult,
    make_owner_token,
)

SOURCE = IngestSource.ATOMIC


class FakeRunner:
    """One missing item until ``run`` succeeds; the body is injectable."""

    def __init__(self, source: IngestSource, body: Callable[..., Any] | None = None) -> None:
        self.source = source
        self.body = body
        self.done = False
        self.runs = 0
        self.plans = 0

    def plan(self, session: Session) -> ReconcilePlan:
        self.plans += 1
        return ReconcilePlan(
            source=self.source,
            needs_fetch=not self.done,
            loaded_count=1 if self.done else 0,
            embedded_count=1 if self.done else 0,
        )

    def run(self, session: Session, plan: ReconcilePlan, progress: Any) -> RunOutcome | None:
        self.runs += 1
        if self.body is not None:
            self.body(session, progress)
        self.done = True
        return None

    def counts(self, session: Session) -> tuple[int, int]:
        return (1, 1) if self.done else (0, 0)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.engine = create_db_engine(
            Settings(database_url=f"sqlite:///{(tmp / 'life.db').as_posix()}")
        )
        import_models()
        Base.metadata.create_all(bind=self.engine)
        self.factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )

    def orchestrator(self, runner: FakeRunner, **kwargs: Any) -> IngestionOrchestrator:
        kwargs.setdefault("heartbeat_interval", 0.05)
        return IngestionOrchestrator(self.factory, {runner.source: runner}, **kwargs)

    def state(self) -> IngestSourceState:
        with self.factory() as session:
            return session.get(IngestSourceState, SOURCE)

    def set_state(self, **values: Any) -> None:
        with self.factory() as session:
            session.execute(
                update(IngestSourceState).where(IngestSourceState.source == SOURCE).values(**values)
            )
            session.commit()


@pytest.fixture()
def env() -> Iterator[Env]:
    tmp = Path(tempfile.mkdtemp(prefix="ingestion_lifecycle_"))
    environment = Env(tmp)
    yield environment
    environment.engine.dispose()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def _wait(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def test_concurrent_verifies_start_a_single_cycle(env: Env) -> None:
    release = threading.Event()
    runner = FakeRunner(SOURCE, lambda session, progress: release.wait(10))
    orchestrator = env.orchestrator(runner)

    results: list[bool] = []
    barrier = threading.Barrier(4)

    def call(kind: str) -> None:
        barrier.wait()
        if kind == "start":
            results.append(orchestrator.start_background())
        else:
            orchestrator.verify_and_start()

    threads = [threading.Thread(target=call, args=(k,)) for k in ("start", "v", "v", "v")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    _wait(lambda: runner.runs >= 1)
    # A late verify while the cycle is alive must not start another one.
    orchestrator.verify_and_start()
    assert orchestrator.start_background() is False
    assert [t for t in threading.enumerate() if t.name == "ingestion-all"] != []
    release.set()
    orchestrator.join(10)

    assert runner.runs == 1
    assert env.state().status == IngestStatus.READY


def test_runner_exception_becomes_failed_with_cause(env: Env) -> None:
    def boom(session: Session, progress: Any) -> None:
        raise RuntimeError("secret-token-123")

    runner = FakeRunner(SOURCE, boom)
    orchestrator = env.orchestrator(runner)
    assert orchestrator.start_background() is True
    orchestrator.join(10)

    state = env.state()
    assert state.status == IngestStatus.FAILED
    assert state.error == "error: RuntimeError"
    assert "secret" not in (state.error or "")
    assert state.finished_at is not None


def test_non_exception_termination_never_leaves_running(env: Env) -> None:
    def die(session: Session, progress: Any) -> None:
        raise SystemExit(1)

    runner = FakeRunner(SOURCE, die)
    orchestrator = env.orchestrator(runner)
    orchestrator.start_background()
    orchestrator.join(10)

    state = env.state()
    assert state.status == IngestStatus.FAILED
    assert state.error == "interrupted"


def test_heartbeat_advances_without_progress_calls(env: Env) -> None:
    seen: list[dt.datetime] = []
    release = threading.Event()

    def slow(session: Session, progress: Any) -> None:
        # No progress() at all, as during a long download.
        _wait(lambda: env.state().heartbeat_at is not None)
        first = env.state().heartbeat_at
        seen.append(first)
        _wait(lambda: env.state().heartbeat_at > first)
        seen.append(env.state().heartbeat_at)
        release.set()

    runner = FakeRunner(SOURCE, slow)
    orchestrator = env.orchestrator(runner)
    orchestrator.start_background()
    orchestrator.join(20)

    assert release.is_set()
    assert seen[1] > seen[0]
    assert env.state().status == IngestStatus.READY


def test_no_transaction_is_open_while_the_runner_works(env: Env) -> None:
    opened: list[int] = []

    class Spy(FakeRunner):
        def plan(self, session: Session) -> ReconcilePlan:
            plan = super().plan(session)
            session.execute(select(IngestSourceState.source))  # opens a transaction
            self.plan_session = session
            return plan

    def body(session: Session, progress: Any) -> None:
        opened.append(1 if runner.plan_session.in_transaction() else 0)

    runner = Spy(SOURCE, body)
    env.orchestrator(runner).run_source(SOURCE)
    assert opened == [0]


def test_orphan_running_of_a_previous_process_is_reclaimed(env: Env) -> None:
    runner = FakeRunner(SOURCE)
    first = env.orchestrator(runner, owner=make_owner_token())
    first.status()  # creates the rows
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    # Fresh heartbeat, but owned by a pid that does not exist (previous boot).
    import socket

    ghost = f"{socket.gethostname()}:99999999:deadbeef"
    env.set_state(
        status=IngestStatus.RUNNING,
        started_at=now,
        heartbeat_at=now,
        finished_at=None,
        error=f"run:{ghost}|",
    )

    restarted = env.orchestrator(runner, owner=make_owner_token())
    statuses = restarted.verify_and_start()
    assert statuses[SOURCE].status == IngestStatus.FAILED  # orphan is not "running"
    restarted.join(10)

    assert runner.runs == 1
    state = env.state()
    assert state.status == IngestStatus.READY
    assert state.error is None


def test_live_foreign_owner_with_fresh_heartbeat_is_respected(env: Env) -> None:
    import os
    import socket

    runner = FakeRunner(SOURCE)
    orchestrator = env.orchestrator(runner)
    orchestrator.status()
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    live_other_boot = f"{socket.gethostname()}:{os.getpid()}:otherboot"
    env.set_state(status=IngestStatus.RUNNING, heartbeat_at=now, error=f"run:{live_other_boot}|")
    assert orchestrator.start(SOURCE) is StartResult.ALREADY_RUNNING
    assert runner.runs == 0


def test_stale_heartbeat_is_reclaimed_and_error_marker_is_hidden(env: Env) -> None:
    runner = FakeRunner(SOURCE)
    orchestrator = env.orchestrator(runner, stale_after=dt.timedelta(minutes=2))
    orchestrator.status()
    old = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=3)
    env.set_state(
        status=IngestStatus.RUNNING,
        heartbeat_at=old,
        error="run:otherhost:1:abcd|network: Boom",
    )
    # While running, the public status never exposes the marker.
    assert orchestrator.status()[0].error == "network: Boom"
    assert orchestrator.run_source(SOURCE) is StartResult.STARTED
    assert env.state().status == IngestStatus.READY


def test_zombie_run_stops_and_does_not_overwrite_the_new_owner(env: Env) -> None:
    proceed = threading.Event()
    stopped: list[str] = []

    def zombie(session: Session, progress: Any) -> None:
        proceed.wait(10)
        try:
            progress(1, 2)
        except Exception as exc:
            stopped.append(type(exc).__name__)
            raise

    runner = FakeRunner(SOURCE, zombie)
    orchestrator = env.orchestrator(runner, heartbeat_interval=60)
    assert orchestrator.start(SOURCE) is StartResult.STARTED
    _wait(lambda: env.state().status == IngestStatus.RUNNING and runner.runs == 1)
    # Another boot steals the lock.
    env.set_state(error="run:otherhost:1:abcd|", heartbeat_at=dt.datetime(2000, 1, 1))
    proceed.set()
    orchestrator.join(10)

    assert stopped == ["OwnershipLostError"]
    state = env.state()
    assert state.status == IngestStatus.RUNNING  # the new owner's state is untouched
    assert state.error == "run:otherhost:1:abcd|"
