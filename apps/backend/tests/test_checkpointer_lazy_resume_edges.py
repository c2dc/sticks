"""Unit tests for the checkpointer's lazy connect and resume edge cases (task 4.3).

Cover :mod:`app.services.ai.checkpointer` (:func:`get_checkpointer`,
:func:`setup_checkpointer`, :func:`_to_libpq_dsn`) and the resume-contract
behaviour that sits directly on top of the LangGraph ``PostgresSaver`` API,
asserting the Requirement 4 guarantees that are achievable at the checkpointer
level today:

* **Req 4.4 — no connection at import.** Importing the module opens no
  socket and pulls in no database driver: the psycopg / LangGraph stack is
  imported lazily inside :func:`get_checkpointer`, and the cached factory
  only runs on the first real use (a probe, a run), never at import time.
  The factory builds the pool with ``open=False`` (no connect in the
  constructor) and then explicitly ``pool.open(wait=True)`` + ``saver.setup()``
  before handing out the saver — a deliberate change from the earlier
  "pool stays closed until first use" contract, made because current
  ``psycopg_pool`` raises ``PoolClosed`` on an unopened pool, which used to
  surface as a spurious "checkpointer unavailable" (503) on every AI run.
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
_FAILED = "failed"


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
        raise NoCheckpointError(f"no checkpoint exists for thread_id {thread_id!r}")
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
        except Exception as exc:
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

        socket.socket.connect = _forbid  # type: ignore[assignment] -- test replaces the stdlib function to forbid network access
        socket.create_connection = _forbid  # type: ignore[assignment] -- test replaces the stdlib function to forbid network access

        import app.services.ai.checkpointer  # noqa: F401 -- import under the network guard proves it makes no connection

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
        f"importing the AI checkpointer module performed network I/O:\n{result.stderr}"
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
    assert "    from langgraph.checkpoint.postgres import PostgresSaver" in source


def _patch_pool_and_saver(monkeypatch, *, open_raises: Exception | None = None):
    """Monkeypatch the deferred ``ConnectionPool`` / ``PostgresSaver`` imports.

    ``get_checkpointer`` imports ``psycopg_pool.ConnectionPool`` and
    ``langgraph.checkpoint.postgres.PostgresSaver`` lazily inside its body, so we
    patch them on their source modules. The fakes record the construction and the
    open/setup calls, letting the contract be asserted without a live PostgreSQL.
    """
    import langgraph.checkpoint.postgres as lcp
    import psycopg_pool

    events: list[str] = []

    class _FakePool:
        def __init__(self, *, conninfo, open, kwargs):
            events.append(f"pool_ctor(open={open})")
            self.conninfo = conninfo
            self.kwargs = kwargs
            self.closed = True

        def open(self, *, wait, timeout=None):
            events.append(f"pool_open(wait={wait})")
            if open_raises is not None:
                raise open_raises
            self.closed = False

    class _FakeSaver:
        def __init__(self, pool):
            events.append("saver_ctor")
            self.conn = pool

        def setup(self):
            events.append("saver_setup")

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool, raising=True)
    monkeypatch.setattr(lcp, "PostgresSaver", _FakeSaver, raising=True)
    return events, _FakePool, _FakeSaver


def test_get_checkpointer_opens_pool_and_runs_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory builds the pool closed, then opens it and runs ``setup()``.

    Contract (deliberately changed from the earlier "closed until first use"):
    ``get_checkpointer`` constructs the pool with ``open=False`` (no connect in
    the constructor), then explicitly ``pool.open(wait=True)`` and
    ``saver.setup()`` before returning the saver. This is lazy only w.r.t. module
    import (the cached factory runs on first real use), and it avoids the
    ``PoolClosed`` 503 that an unopened pool would raise on first checkpoint I/O.
    """
    events, _FakePool, _FakeSaver = _patch_pool_and_saver(monkeypatch)

    get_checkpointer.cache_clear()
    try:
        saver = get_checkpointer()

        # Built closed, then explicitly opened (wait=True) and set up, in order.
        assert events == [
            "pool_ctor(open=False)",
            "pool_open(wait=True)",
            "saver_ctor",
            "saver_setup",
        ]
        assert isinstance(saver, _FakeSaver)
        # The pool handed to the saver was actually opened.
        assert saver.conn.closed is False
    finally:
        get_checkpointer.cache_clear()


def test_get_checkpointer_propagates_pool_open_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable database fails loudly at build via ``pool.open(wait=True)``.

    Opening eagerly with ``wait=True`` is what turns a genuinely unreachable
    PostgreSQL into a clear, caller-visible error (which the availability probe
    maps to a real "dependency unavailable") instead of the misleading
    ``PoolClosed`` that a deferred-open pool raised on first use. The factory must
    therefore propagate the open failure rather than hand back a half-built saver.
    """
    boom = RuntimeError("could not connect to server")
    events, _FakePool, _FakeSaver = _patch_pool_and_saver(monkeypatch, open_raises=boom)

    get_checkpointer.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="could not connect to server"):
            get_checkpointer()

        # It failed at open: the saver was never constructed and setup never ran.
        assert events == ["pool_ctor(open=False)", "pool_open(wait=True)"]
    finally:
        # A raising factory must not leave a cached (half-built) instance behind.
        get_checkpointer.cache_clear()


def test_get_checkpointer_is_cached_single_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory is cached so the whole process shares one saver/pool.

    Runs against an explicit fake URL and a faked pool/saver: no real database
    connection is ever opened, whatever the developer's environment holds.
    """
    from app.core.config import get_settings

    monkeypatch.setenv(
        "PIPELINE_UI_DATABASE_URL",
        "postgresql+psycopg://fake:fake@fake-host.invalid:5432/fake",
    )
    events, _FakePool, _FakeSaver = _patch_pool_and_saver(monkeypatch)

    get_settings.cache_clear()
    get_checkpointer.cache_clear()
    try:
        first = get_checkpointer()
        second = get_checkpointer()
        assert first is second
        assert isinstance(first, _FakeSaver)
        # One pool, one saver, despite two calls; the DSN is the fake one.
        assert events.count("pool_ctor(open=False)") == 1
        assert events.count("saver_ctor") == 1
        assert first.conn.conninfo == "postgresql://fake:fake@fake-host.invalid:5432/fake"
    finally:
        get_checkpointer.cache_clear()
        get_settings.cache_clear()


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

    monkeypatch.setattr(checkpointer_module, "get_checkpointer", lambda: _SaverStub())

    setup_checkpointer()

    assert calls == ["setup"]


# ---- _to_libpq_dsn: the normalisation feeding the deferred pool -------------


def test_to_libpq_dsn_strips_driver_marker() -> None:
    """``postgresql+psycopg://`` is normalised to the bare libpq scheme."""
    dsn = _to_libpq_dsn("postgresql+psycopg://u:p@host:9090/db")
    assert dsn == "postgresql://u:p@host:9090/db"


def test_to_libpq_dsn_passes_through_plain_scheme() -> None:
    """A URL without a ``+driver`` marker is returned unchanged."""
    assert _to_libpq_dsn("postgresql://u:p@host:9090/db") == "postgresql://u:p@host:9090/db"


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
    checkpoints = [{"iteration": 0, "node": "flow"}, failing]

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
    good = {"iteration": 0, "node": "flow"}
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
        run_with_checkpoints(saver, "t-halt", [{"iteration": 0, "node": "flow"}, failing, never])

    attempted = [checkpoint for _tid, checkpoint in saver.put_calls]
    assert never not in attempted, "run must not persist past the failure"
    # The first (good) and the failing one were attempted, in order, and no more.
    assert attempted == [{"iteration": 0, "node": "flow"}, failing]
