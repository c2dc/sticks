"""Per-case provisioning log buffer (Stage 5).

This module defines :class:`ProvisioningLogBuffer` — the **in-memory**, per-case
buffer of the Stage 5 provisioning log lines. It is the structure that the
:class:`~app.services.environment.stage_service.EnvironmentStageService` (tasks
5.1/5.2) feeds with each :class:`~app.services.environment.compose_executor.LogLine`
emitted by the executor and the pipeline steps, and from which the WebSocket
channel (``ws/channels.py``, task 4.3) and the REST endpoints (task 6.x) read for:

- **live tracking** — the channel sends the new lines since the last frame
  (delta by ``seq``) and, on connect, the full buffer (Req. 2.5);
- **recovery on reconnect / late-opened screen** — ``GET .../logs`` and the
  connect return the full buffer of the current execution, so the screen can
  rebuild the history without losing lines (Req. 2.7).

By design it is **ephemeral** (in-memory, per-process): it serves live tracking
and reconnection of the current execution (Req. 2.5, 2.7), **not** a persistent
audit history — the per-command audit of the *execution* (Stage 6, via
``AuditLogEntry``) is out of scope for this spec.

Design decisions
----------------

**Monotonic ``seq`` per case, starting at 1.** Each case has its own counter.
The first appended line gets ``seq == 1``; each subsequent append increments. The
``seq`` is **never reused**, even when the list is trimmed on exceeding the limit:
the counter keeps advancing regardless of the list's current size. This gives the
screen a stable key to deduplicate/reorder the lines on reconnect (Req. 2.5) — the
client only needs to keep the greatest ``seq`` seen and request ``new_since`` that
value.

**Bounded list.** To avoid unbounded growth in a long-lived process, each case
keeps at most :data:`DEFAULT_MAX_LINES` lines (default ``2000``, configurable in
the constructor). On exceeding, the **oldest** lines are discarded (FIFO) — but
the ``seq`` stays monotonic, so the client notices the "jump" and knows old lines
were trimmed (acceptable behavior for a live terminal; long-term history is not
this buffer's responsibility).

**Naive-UTC timestamp on append.** The executor's :class:`LogLine` does **not**
carry a timestamp (decoupling of production↔transport). The buffer is the one that
stamps ``at`` (naive-UTC) on append, materializing the :class:`LogLineView`. The
clock is **injectable** (``clock``) and defaults to the same naive-UTC helper as
the service (:func:`datetime.now(UTC).replace(tzinfo=None)`), portable between
PostgreSQL and the test SQLite and controllable by tests.

**Thread-safe enough for the single FastAPI process.** A lightweight
:class:`threading.Lock` guards each mutation/read, so the append triggered by the
provisioning task and the channel/endpoint reads do not race each other. Reads
return **copies** (new lists of immutable objects), so the caller never mutates
the buffer by reference.

_Requirements: 2.5, 2.7_
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Callable, Optional

from app.services.environment.compose_executor import LogLine
from app.services.stage.stage_service import LogLineView

# Default per-case line limit. Keeps a live execution's buffer within a
# predictable memory band in a long-lived process; on exceeding, the oldest
# lines are trimmed (the ``seq`` stays monotonic). Ephemeral by design — not an
# audit history (Req. 2.5, 2.7).
DEFAULT_MAX_LINES: int = 2000


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable between PostgreSQL and the test SQLite).

    Same contract as the same-named helper in
    :mod:`app.services.environment.stage_service`: ``datetime.now(UTC)`` with the
    ``tzinfo`` stripped, so the :class:`LogLineView` ``at`` is naive-UTC and
    consistently comparable across any database backend.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class ProvisioningLogBuffer:
    """In-memory, per-case buffer of the provisioning log lines (Req. 2.5, 2.7).

    Keeps, for each ``campaign_id``, a **bounded** list of :class:`LogLineView` in
    monotonic ``seq`` order. Fed by the
    :class:`~app.services.environment.stage_service.EnvironmentStageService` with
    each :class:`LogLine` emitted (via :meth:`append`), and read by the WebSocket
    channel and the REST endpoints (via :meth:`read` for the full buffer and
    :meth:`new_since` for the reconnection delta).

    It is **ephemeral** (in-memory, per-process) — it serves live tracking and
    reconnection of the current execution, not a persistent history (Req. 2.5, 2.7).

    Parameters
    ----------
    max_lines:
        Maximum number of lines retained per case (default
        :data:`DEFAULT_MAX_LINES`). On exceeding, the oldest lines are discarded
        (FIFO); the ``seq`` stays monotonic. Must be ``>= 1``.
    clock:
        Zero-argument callable returning the (naive-UTC) timestamp assigned to
        ``at`` on append. Default: :func:`_utcnow`. Injectable in tests to control
        time.
    """

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        clock: Optional[Callable[[], dt.datetime]] = None,
    ) -> None:
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        self._max_lines = max_lines
        self._clock: Callable[[], dt.datetime] = clock if clock is not None else _utcnow
        self._lock = threading.Lock()
        # campaign_id -> bounded list of LogLineView (in seq order).
        self._buffers: dict[str, list[LogLineView]] = {}
        # campaign_id -> greatest seq assigned so far (never reused, even after trim).
        self._seq: dict[str, int] = {}

    def append(self, campaign_id: str, line: LogLine) -> LogLineView:
        """Append an executor :class:`LogLine` to the case's buffer (Req. 2.5).

        Assigns the case's next monotonic ``seq`` (starting at ``1``) and the
        clock's naive-UTC timestamp, materializing the stored
        :class:`LogLineView`. On exceeding :attr:`max_lines`, the oldest line is
        discarded (FIFO) — but the ``seq`` is **not** reused.

        Parameters
        ----------
        campaign_id:
            The case whose buffer receives the line.
        line:
            The :class:`LogLine` emitted by the executor/step (``step``/``stream``/
            ``text``, with no timestamp).

        Returns
        -------
        LogLineView
            The materialized, stored line (with ``seq`` and ``at`` assigned).
        """
        with self._lock:
            next_seq = self._seq.get(campaign_id, 0) + 1
            self._seq[campaign_id] = next_seq
            view = LogLineView(
                seq=next_seq,
                step=line.step,
                stream=line.stream,
                text=line.text,
                at=self._clock(),
            )
            buffer = self._buffers.setdefault(campaign_id, [])
            buffer.append(view)
            # Trim the oldest on exceeding the limit (FIFO); seq keeps advancing.
            excess = len(buffer) - self._max_lines
            if excess > 0:
                del buffer[:excess]
            return view

    def read(self, campaign_id: str) -> list[LogLineView]:
        """Return the case's full current buffer, in ``seq`` order (Req. 2.5, 2.7).

        Used on WebSocket connect and in ``GET .../logs`` to recover the current
        execution's history. Returns a **copy** of the list (of immutable
        objects), so the caller never mutates the buffer by reference. If
        ``campaign_id`` has no lines, returns an empty list.
        """
        with self._lock:
            return list(self._buffers.get(campaign_id, ()))

    def new_since(self, campaign_id: str, seq: int) -> list[LogLineView]:
        """Return the case's lines with ``seq`` strictly greater than ``seq`` (Req. 2.5, 2.7).

        This is the delta the channel uses to send only the new lines since the
        last frame (the client keeps the greatest ``seq`` seen). Returns a new
        list, in ``seq`` order; empty when there are no newer lines. Passing
        ``seq=0`` is equivalent to :meth:`read` (all current lines).

        Parameters
        ----------
        campaign_id:
            The case to read the delta from.
        seq:
            The greatest ``seq`` already known to the caller; only lines with a
            greater ``seq`` are returned.
        """
        with self._lock:
            buffer = self._buffers.get(campaign_id, ())
            return [line for line in buffer if line.seq > seq]

    def clear(self, campaign_id: str) -> None:
        """Reset the case's buffer when a new provisioning starts.

        Discards the accumulated lines **and** the case's ``seq`` counter, so the
        next execution restarts numbering at ``1``. Called by the service when it
        triggers a fresh provisioning (tasks 5.1/5.2), separating the live
        terminal of the previous execution from the new one. Idempotent: clearing
        a case with no lines is a no-op.
        """
        with self._lock:
            self._buffers.pop(campaign_id, None)
            self._seq.pop(campaign_id, None)


# ---------------------------------------------------------------------------
# Process-level shared provider (Stage 5 — tasks 5.1/4.3/6.x)
# ---------------------------------------------------------------------------
#
# The provisioning log buffer must be **a single instance per process**, shared
# across three consumers:
#
# * the :class:`~app.services.environment.stage_service.EnvironmentStageService`,
#   which **appends** each :class:`LogLine` emitted by the pipeline (task 5.1);
# * the Stage WebSocket channel (``app.ws.channels.run_stage_progress_channel``),
#   which **reads** the buffer to feed the screen's live terminal (task 4.3); and
# * the Stage 5 REST endpoints (``GET .../stage/5`` and ``.../stage/5/logs``),
#   which **read** the current buffer (task 6.x).
#
# Since the three seams run in the same FastAPI process, a module-level
# *singleton* is enough. :func:`get_log_buffer` is the dependency the WS router
# and the endpoints resolve; tests **override** it (via ``app.dependency_overrides``
# or by replacing ``_LOG_BUFFER``) to inject an isolated, deterministic buffer.
_LOG_BUFFER: ProvisioningLogBuffer = ProvisioningLogBuffer()


def get_log_buffer() -> ProvisioningLogBuffer:
    """Return the process-level shared :class:`ProvisioningLogBuffer`.

    It is the **same** instance that the :class:`EnvironmentStageService` feeds
    (appending each pipeline :class:`LogLine`), that the Stage WebSocket channel
    reads for the live terminal (Req. 2.2) and that the Stage 5 REST endpoints
    read to return the current buffer (Req. 2.5, 2.7). In production it returns the
    module *singleton* :data:`_LOG_BUFFER`; tests **override** this dependency
    (``dependency_overrides``) to inject an isolated buffer.
    """
    return _LOG_BUFFER


__all__ = [
    "ProvisioningLogBuffer",
    "DEFAULT_MAX_LINES",
    "get_log_buffer",
]
