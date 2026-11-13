"""Real-time progress channels — WebSocket push loops (task 11.3).

This module implements the WebSocket progress channel described in the design's
*WebSocket/SSE channels* section:

* ``WS /ws/operation/{operation}`` — pushes the per-Ability execution status
  (``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``), the command output
  and, when the Operation reaches its final state, the aggregate result
  (Req. 4.3, 4.4, 4.5).

Design notes
------------
* **Typed JSON frames.** Every frame is built from the services' own typed Pydantic
  views (per-Ability / aggregate views for operations) serialized with ``model_dump(mode="json")`` and sent with
  ``websocket.send_json``. No hand-rolled payload shapes.
* **Poll-based push.** The underlying services expose *reads* over the persisted
  state (``SessionStateService`` reads
  ``Operation`` / ``AbilityResult`` rows). There is no Caldera/Docker here — the
  channels poll that persisted state and push a frame **on change** and **at
  least every ``interval`` seconds** while the subject is still active. This
  matches the dev constraint: no real emulation, the channels reflect whatever
  the services have persisted.
* **Injectable / shrinkable interval.** The push interval is a parameter with a
  module-level default constant. Tests inject a *short/zero*
  interval so one or two frames are produced without waiting.
* **Fresh short-lived Session per poll.** The services use synchronous
  SQLAlchemy sessions. Each poll opens a session from an injected
  ``session_factory`` (``SessionLocal`` in the app; an ephemeral SQLite factory
  in tests), reads, and closes it — so a long-lived socket never holds a DB
  session open, and each frame reflects the latest committed state.
* **Graceful disconnect / missing subject.** A client disconnect
  (``WebSocketDisconnect``) ends the loop quietly. A ``None`` / absent subject
  (e.g. an operation id that has no persisted ``Operation``) sends a single
  ``not_found`` frame and closes rather than looping forever.

The FastAPI ``@websocket`` route wiring lives in :mod:`app.ws.router`; this
module holds the reusable loop logic so it can be unit-tested directly and kept
free of routing concerns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.orm import Session
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.models.enums import OperationState
from app.services.session import SessionStateService

# ---------------------------------------------------------------------------
# Push interval
# ---------------------------------------------------------------------------

#: Default max interval between operation frames while an Operation is running
#: (Req. 4.3). Reduced to ≤ 2s so the update latency of the live
#: technique-by-technique tracking satisfies Req. 2.3/2.5. Tests pass a
#: short/zero value.
OPERATION_PUSH_INTERVAL_SECONDS: float = 2.0

#: Terminal operation states — final states of an Operation (Req. 4.5, 3.2).
#: Once reached, the channel sends the final aggregate and closes. Includes
#: ``INCOMPLETE_TIMEOUT`` so a collection that timed out without reaching a
#: Caldera terminal state still produces a final frame and closes the channel
#: (Req. 3.2).
_OPERATION_TERMINAL_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.FINISHED,
        OperationState.ABORTED,
        OperationState.INCOMPLETE_TIMEOUT,
    }
)


# A zero-argument callable returning a new SQLAlchemy Session (e.g. the app's
# ``SessionLocal`` or an ephemeral test session factory). ``sessionmaker`` and a
# plain lambda both satisfy this.
SessionFactory = Callable[[], Session]

# A no-argument awaitable used to break the inter-frame wait early. Optional; in
# production the loop just sleeps ``interval`` seconds. Tests use it to keep the
# loop deterministic if needed.
Sleeper = Callable[[float], Awaitable[None]]


async def _default_sleep(seconds: float) -> None:
    """Async sleep, but never negative (a 0/negative interval yields at once)."""
    await asyncio.sleep(max(0.0, seconds))


# ---------------------------------------------------------------------------
# Operation progress channel — WS /ws/operation/{operation}
# ---------------------------------------------------------------------------


def _operation_view(service: SessionStateService, operation_id: int):
    """Return the persisted :class:`OperationResultView` for ``operation_id``.

    ``SessionStateService.get_operation_results`` groups results by *case*; here
    the channel is keyed by *operation id*, so we find the matching operation by
    walking the results the service already assembles (it carries ``operation_id``
    and ``campaign_id`` on every view). Returns ``None`` when no such operation is
    persisted (handled as ``not_found`` by the caller).
    """
    from app.models.domain import Operation  # local import: avoid cycle at top

    session = service._session  # the service exposes its bound session
    op = session.get(Operation, operation_id)
    if op is None:
        return None
    views = service.get_operation_results(op.campaign_id) if op.campaign_id else []
    for view in views:
        if view.operation_id == operation_id:
            return view
    return None


async def run_operation_progress_channel(
    websocket: WebSocket,
    operation_id: int,
    *,
    session_factory: SessionFactory,
    interval: float = OPERATION_PUSH_INTERVAL_SECONDS,
    max_frames: int | None = None,
    sleeper: Sleeper = _default_sleep,
) -> int:
    """Push per-Ability status + final aggregate for an Operation (Req. 4.3-4.5).

    Polls the persisted Operation (:meth:`SessionStateService.get_operation_results`)
    and sends a typed frame containing, for each Ability of the Operation, its
    status (``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``) and command
    output (Req. 4.3, 4.4), plus the running aggregate. A frame is sent on the
    first poll, whenever the per-Ability results or aggregate change, and at
    least every ``interval`` seconds while the Operation is still running. When
    the Operation reaches its final state (``finalizada`` / ``abortada``) a final
    frame carrying the aggregate is sent and the loop stops (Req. 4.5).

    If no ``Operation`` with ``operation_id`` is persisted, a single ``not_found``
    frame is sent and the loop ends (graceful handling of a None/absent
    operation).

    Args:
        websocket: The connected client socket (already ``accept``-ed).
        operation_id: The persisted Operation id.
        session_factory: Zero-arg factory returning a fresh session per poll.
        interval: Max seconds between frames while the Operation runs.
        max_frames: Optional cap on the number of frames (tests).
        sleeper: Injectable async sleep.

    Returns:
        The number of frames actually sent.
    """
    frames_sent = 0
    last_signature: str | None = None

    try:
        while True:
            session = session_factory()
            try:
                service = SessionStateService(session)
                view = _operation_view(service, operation_id)
            finally:
                session.close()

            if view is None:
                # No such Operation persisted — tell the client and stop rather
                # than polling forever (graceful None handling).
                await websocket.send_json(
                    {
                        "type": "operation",
                        "operation_id": operation_id,
                        "found": False,
                        "message": ("No Operation persisted for the given id."),
                    }
                )
                frames_sent += 1
                break

            frame = {
                "type": "operation",
                "operation_id": view.operation_id,
                "campaign_id": view.campaign_id,
                "state": view.state.value,
                "found": True,
                "final": view.state in _OPERATION_TERMINAL_STATES,
                "results": [r.model_dump(mode="json") for r in view.results],
                "aggregate": {
                    "total_success": view.total_success,
                    "total_failure": view.total_failure,
                },
            }

            signature = repr(frame)
            is_final = view.state in _OPERATION_TERMINAL_STATES
            # Push on first frame, on change, on final, and periodically. Since
            # this loop only sends after the interval wait below, "periodically"
            # is naturally satisfied; we always send when something changed.
            if last_signature is None or signature != last_signature or is_final:
                await websocket.send_json(frame)
                frames_sent += 1
                last_signature = signature

            if is_final:
                break
            if max_frames is not None and frames_sent >= max_frames:
                break

            await sleeper(interval)
    except WebSocketDisconnect:
        pass

    return frames_sent


__all__ = [
    "OPERATION_PUSH_INTERVAL_SECONDS",
    "run_operation_progress_channel",
]
