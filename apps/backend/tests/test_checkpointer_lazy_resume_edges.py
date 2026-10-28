"""Unit tests for the checkpointer's lazy connect and resume edge cases (task 4.3).

Cover :mod:`app.services.ai.checkpointer` (:func:`get_checkpointer`,
:func:`setup_checkpointer`, :func:`_to_libpq_dsn`) and the resume-contract
behaviour that sits directly on top of the LangGraph ``PostgresSaver`` API,
asserting the Requirement 4 guarantees that are achievable at the checkpointer
level today:

* **Req 4.4 — no connection at import / deferred connect.** Importing the
  module opens no socket, and :func:`get_checkpointer` builds its psycopg
  connection pool with ``open=False`` so the pool stays *closed / not opened*
  (no connection established) until the first checkpoint read or write.
* **Req 4.5 — resuming an unknown ``thread_id``.** When a run is resumed with a
  ``thread_id`` that has no persisted checkpoint, the resume path reports "no
  checkpoint exists" for that ``thread_id`` and starts no new run under that
  identifier. The LangGraph saver signals "no checkpoint" by returning ``None``
  from ``get_tuple(config)``; the resume contract tested here turns that into a
  caller-visible "no checkpoint" report without invoking the graph.
* **Req 4.6 — a failed checkpoint persist halts the run.** When persisting a
  checkpoint fails, the run halts, is marked ``FAILED`` with the failure cause,
  and the last successfully persisted checkpoint is retained unchanged.

Testing note (per task 4.3): the *run-level* ``FAILED`` marking ultimately
belongs to ``AgentRunService`` (task 15, not yet implemented). Where that
integration is unavailable, these tests exercise the behaviour *contract* that
is achievable now — the thin resume / persist orchestration expressed directly
over the checkpointer's ``get_tuple`` / ``put`` API — using fakes that mirror
the real ``BaseCheckpointSaver`` surface rather than a live PostgreSQL.

_Requirements: 4.4, 4.5, 4.6_
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.services.ai import checkpointer as checkpointer_module
from app.services.ai.checkpointer import (
    _to_libpq_dsn,
    get_checkpointer,
    setup_checkpointer,
)

# AgentRunStatus.FAILED value as defined by the portable enum (task 2.1). The
# resume/persist contract reports this exact string when a persist fails so the
# assertions pin the observable status without importing run-service internals
# that do not exist yet (task 15).
_FAILED = "falhou"


# ---------------------------------------------------------------------------
# Fakes mirroring the LangGraph BaseCheckpointSaver surface used by resume.
# ---------------------------------------------------------------------------


def _thread_id(config: dict) -> str:
    """Extract the checkpoint thread key from a LangGraph-style config."""
    return config["configurable"]["thread_id"]


class FakeCheckpointSaver:
    """In-memory stand-in for ``PostgresSaver`` with the methods resume needs.

    Mirrors the real saver contract: ``get_tuple(config)`` returns the last
    persisted checkpoint for the config's ``thread_id`` or ``None`` when none
    exists (the "no checkpoint" signal), and ``put(config, checkpoint)`` persists
    a checkpoint for that ``thread_id``. ``put`` may be told to fail for a given
    checkpoint to exercise the failed-persist path (Req 4.6).
    """

    def __init__(self, *, fail_on: object | None = None) -> None:
        # thread_id -> last successfully persisted checkpoint payload.
        self._store: dict[str, object] = {}
        # When a put is asked to persist this exact checkpoint, it raises.
        self._fail_on = fail_on
        self.put_calls: list[tuple[str, object]] = []

    def get_tuple(self, config: dict) -> object | None:
        """Return the last checkpoint for the thread, or ``None`` if unknown."""
        return self._store.get(_thread_id(config))

    def put(self, config: dict, checkpoint: object) -> dict:
        """Persist a checkpoint, raising when it is the configured failure one."""
        tid = _thread_id(config)
        self.put_calls.append((tid, checkpoint))
        if self._fail_on is not None and checkpoint == self._fail_on:
            raise RuntimeError("simulated checkpoint persist failure")
        self._store[tid] = checkpoint
        return config


# ---------------------------------------------------------------------------
# Resume / persist contract under test (the behaviour achievable now).
# ---------------------------------------------------------------------------


class NoCheckpointError(Exception):
    """Raised when resuming a ``thread_id`` that has no persisted checkpoint."""


def resume_run(saver: object, thread_id: str, *, start_new) -> object:
    """Resume a run from the last checkpoint, or report "no checkpoint".

    Models the Req 4.5 contract directly over the saver API: look up the last
    persisted checkpoint for ``thread_id``; if none exists, report that no
    checkpoint exists for the identifier and DO NOT start a new run under it
    (``start_new`` is never invoked). Otherwise return the restored checkpoint.
    """
    config = {"configurable": {"thread_id": thread_id}}
    restored = saver.get_tuple(config)
    if restored is None:
        raise NoCheckpointError(
            f"no checkpoint exists for thread_id {thread_id!r}"
        )
    return restored


class RunHalted(Exception):
    """Carries the terminal run state produced by a failed checkpoint persist."""

    def __init__(self, status: str, cause: str) -> None:
        super().__init__(cause)
        self.status = status
        self.cause = cause


def run_with_checkpoints(saver: object, thread_id: str, checkpoints: list) -> None:
    """Persist each node's checkpoint in turn, halting on the first failure.

    Models the Req 4.6 contract: after each node completes its checkpoint is
    persisted; if a persist fails the run halts immediately and is marked
    ``FAILED`` with the failure cause. No further checkpoints are attempted, and
    whatever was last successfully persisted is retained (left to the saver).
    """
    config = {"configurable": {"thread_id": thread_id}}
    for checkpoint in checkpoints:
        try:
            saver.put(config, checkpoint)
        except Exception as exc:  # noqa: BLE001 - surfaced as the failure cause
            raise RunHalted(_FAILED, str(exc)) from exc


# ===========================================================================
# Requirement 4.4 — no connection at import; pool built with open=False
# ===========================================================================


def test_import_opens_no_network_connection() -> None:
    """Importing the checkpointer module opens no socket connection.

    A hard socket guard is installed in a child process *before* the module is
    imported: any ``connect`` / ``create_connection`` attempt raises, so a
    non-zero exit proves import-time network I/O. The child process guarantees a
    genuinely fresh import without disturbing the parent's module registry.
    """
    program = textwrap.dedent(
        """
        import socket
        import sys

        def _forbid(*_a, **_k):
            raise AssertionError("import opened a network connection")

        socket.socket.connect = _forbid  # type: ignore[assignment]
        socket.create_connection = _forbid  # type: ignore[assignment]

        import app.services.ai.checkpointer  # noqa: F401

        sys.exit(0)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, (
        "importing the AI checkpointer module performed network I/O:\n"
        f"{result.stderr}"
    )


def test_get_checkpointer_defers_heavy_imports_to_body() -> None:
    """The psycopg / LangGraph imports live inside the factory, not at top level.

    Keeping the connection pool, saver and psycopg row factory imports inside
    ``get_checkpointer`` is what lets merely importing the module stay free of
    the database driver and any I/O (Req 4.4 / 1.7).
    """
    source = Path(checkpointer_module.__file__).read_text(encoding="utf-8")

    # No eager top-level imports of the connection/saver stack.
    assert "\nfrom psycopg_pool import ConnectionPool" not in source
    assert "\nfrom langgraph.checkpoint.postgres import PostgresSaver" not in source
    # They appear deferred inside the factory body (indented).
    assert "    from psycopg_pool import ConnectionPool" in source
    assert (
        "    from langgraph.checkpoint.postgres import PostgresSaver" in source
    )


def test_get_checkpointer_builds_pool_closed_until_first_use() -> None:
    """The factory builds a pool that is not opened (no connection) yet.

    A pool created with ``open=False`` reports ``closed is True`` and
    ``_opened is False`` — it has established no connection. Building the
    checkpointer must leave the pool in that deferred state so the first
    connection waits for the first checkpoint read/write (Req 4.4).
    """
    get_checkpointer.cache_clear()
    try:
        saver = get_checkpointer()
        pool = saver.conn  # PostgresSaver stores its ConnectionPool on .conn

        # Not opened yet => no connection established at build time.
        assert pool.closed is True
        assert getattr(pool, "_opened") is False
    finally:
        # Drop the cached instance so no half-built pool leaks to other tests.
        get_checkpointer.cache_clear()


def test_get_checkpointer_build_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling the factory establishes no connection (no ``socket.connect``).

    Guards ``socket.socket.connect`` / ``socket.create_connection`` for the
    duration of the build; because the pool is created with ``open=False`` the
    factory must not touch the network, so neither guard fires (Req 4.4).
    """
    import socket

    def _forbid(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("building the checkpointer opened a connection")

    monkeypatch.setattr(socket.socket, "connect", _forbid, raising=True)
    monkeypatch.setattr(socket, "create_connection", _forbid, raising=True)

    get_checkpointer.cache_clear()
    try:
        saver = get_checkpointer()
        assert saver is not None
    finally:
        get_checkpointer.cache_clear()


def test_get_checkpointer_is_cached_single_pool() -> None:
    """The factory is cached so the whole process shares one saver/pool."""
    get_checkpointer.cache_clear()
    try:
        first = get_checkpointer()
        second = get_checkpointer()
        assert first is second
    finally:
        get_checkpointer.cache_clear()


def test_setup_checkpointer_defers_connection_to_setup_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setup_checkpointer`` is the controlled point that touches the DB.

    Importing and building the checkpointer stays connection-free; the one place
    a connection is opened at a controlled time is ``setup()``. This asserts the
    wiring: ``setup_checkpointer`` calls ``setup`` on the cached saver and does
    nothing else, so no connection happens until that explicit call.
    """
    calls: list[str] = []

    class _SaverStub:
        def setup(self) -> None:
            calls.append("setup")

    monkeypatch.setattr(
        checkpointer_module, "get_checkpointer", lambda: _SaverStub()
    )

    setup_checkpointer()

    assert calls == ["setup"]


# ---- _to_libpq_dsn: the normalisation feeding the deferred pool -------------


def test_to_libpq_dsn_strips_driver_marker() -> None:
    """``postgresql+psycopg://`` is normalised to the bare libpq scheme."""
    dsn = _to_libpq_dsn("postgresql+psycopg://u:p@host:9090/db")
    assert dsn == "postgresql://u:p@host:9090/db"


def test_to_libpq_dsn_passes_through_plain_scheme() -> None:
    """A URL without a ``+driver`` marker is returned unchanged."""
    assert (
        _to_libpq_dsn("postgresql://u:p@host:9090/db")
        == "postgresql://u:p@host:9090/db"
    )


def test_to_libpq_dsn_returns_non_url_unchanged() -> None:
    """A value with no ``://`` separator is returned unchanged."""
    assert _to_libpq_dsn("not-a-url") == "not-a-url"


# ===========================================================================
# Requirement 4.5 — resuming an unknown thread_id reports "no checkpoint"
#                   and starts no new run under that identifier
# ===========================================================================


def test_resume_unknown_thread_id_reports_no_checkpoint() -> None:
    """Resuming a never-seen ``thread_id`` reports no checkpoint exists.

    The saver returns ``None`` from ``get_tuple`` for an unknown thread, which
    the resume contract turns into a caller-visible "no checkpoint" report that
    names the offending ``thread_id`` (Req 4.5).
    """
    saver = FakeCheckpointSaver()  # empty store: nothing persisted yet

    def _start_new() -> None:  # pragma: no cover - must never be called
        raise AssertionError("resume must not start a new run")

    with pytest.raises(NoCheckpointError) as excinfo:
        resume_run(saver, "ghost-thread-404", start_new=_start_new)

    assert "no checkpoint" in str(excinfo.value).lower()
    assert "ghost-thread-404" in str(excinfo.value)


def test_resume_unknown_thread_id_does_not_start_new_run() -> None:
    """Resuming an unknown ``thread_id`` never starts a run under that id.

    The ``start_new`` callback records any invocation; it must stay untouched so
    that a failed resume cannot silently spawn a fresh run under the same
    identifier (Req 4.5).
    """
    saver = FakeCheckpointSaver()
    started: list[str] = []

    def _start_new() -> None:
        started.append("started")

    with pytest.raises(NoCheckpointError):
        resume_run(saver, "unknown-thread", start_new=_start_new)

    assert started == [], "no new run may be started for an unknown thread_id"


def test_resume_known_thread_id_restores_last_checkpoint() -> None:
    """Resuming a known ``thread_id`` restores its last persisted checkpoint.

    Positive counterpart to the "unknown" cases: once a checkpoint has been
    persisted for a thread, resume returns exactly that state (so the "no
    checkpoint" report in the other tests is specific to truly-unknown threads).
    """
    saver = FakeCheckpointSaver()
    config = {"configurable": {"thread_id": "known-thread"}}
    saver.put(config, {"iteration": 2, "node": "operationalize"})

    def _start_new() -> None:  # pragma: no cover - must never be called
        raise AssertionError("resume of a known thread must not start a new run")

    restored = resume_run(saver, "known-thread", start_new=_start_new)
    assert restored == {"iteration": 2, "node": "operationalize"}


# ===========================================================================
# Requirement 4.6 — a failed checkpoint persist halts the run, marks it FAILED
#                   with the cause, and retains the last persisted checkpoint
# ===========================================================================


def test_failed_persist_halts_and_marks_failed_with_cause() -> None:
    """A failing persist halts the run and marks it FAILED with the cause.

    The second checkpoint is configured to fail; the run must halt on it, raise
    the terminal ``FAILED`` state, and carry the underlying failure cause
    (Req 4.6).
    """
    failing = {"iteration": 1, "node": "environment"}
    saver = FakeCheckpointSaver(fail_on=failing)
    checkpoints = [{"iteration": 0, "node": "dag"}, failing]

    with pytest.raises(RunHalted) as excinfo:
        run_with_checkpoints(saver, "t-fail", checkpoints)

    assert excinfo.value.status == _FAILED
    assert "persist failure" in excinfo.value.cause


def test_failed_persist_retains_last_successful_checkpoint() -> None:
    """The last successfully persisted checkpoint survives a later failed persist.

    The first checkpoint persists cleanly; the second fails. Afterwards the saver
    must still hold the first checkpoint unchanged — the failure retains, not
    corrupts, prior state (Req 4.6).
    """
    good = {"iteration": 0, "node": "dag"}
    failing = {"iteration": 1, "node": "environment"}
    saver = FakeCheckpointSaver(fail_on=failing)

    with pytest.raises(RunHalted):
        run_with_checkpoints(saver, "t-retain", [good, failing])

    retained = saver.get_tuple({"configurable": {"thread_id": "t-retain"}})
    assert retained == good


def test_failed_persist_halts_before_subsequent_checkpoints() -> None:
    """The run stops at the failing persist; no later checkpoint is attempted.

    A third checkpoint follows the failing one but must never be offered to the
    saver, proving the run halted rather than continuing past the failure
    (Req 4.6).
    """
    failing = {"iteration": 1, "node": "environment"}
    never = {"iteration": 2, "node": "execute"}
    saver = FakeCheckpointSaver(fail_on=failing)

    with pytest.raises(RunHalted):
        run_with_checkpoints(
            saver, "t-halt", [{"iteration": 0, "node": "dag"}, failing, never]
        )

    attempted = [checkpoint for _tid, checkpoint in saver.put_calls]
    assert never not in attempted, "run must not persist past the failure"
    # The first (good) and the failing one were attempted, in order, and no more.
    assert attempted == [{"iteration": 0, "node": "dag"}, failing]
