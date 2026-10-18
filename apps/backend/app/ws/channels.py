"""Real-time progress channels — WebSocket push loops (task 11.3).

This module implements the two WebSocket channels described in the design's
*WebSocket/SSE channels* section:

* ``WS /ws/stages/{campaign}`` — pushes a stage's state transitions and its
  progress percentage; while a stage is ``em_andamento`` it re-pushes at least
  every 5s so the UI shows the live percentage (Req. 1.4, 1.5).
* ``WS /ws/operation/{operation}`` — pushes the per-Ability execution status
  (``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``), the command output
  and, when the Operation reaches its final state, the aggregate result
  (Req. 4.3, 4.4, 4.5).

Design notes
------------
* **Typed JSON frames.** Every frame is one of the services' own typed Pydantic
  views (``StageProgressEvent`` for stages, and per-Ability / aggregate views
  for operations) serialized with ``model_dump(mode="json")`` and sent with
  ``websocket.send_json``. No hand-rolled payload shapes.
* **Poll-based push.** The underlying services expose *reads* over the persisted
  state (``StageService`` reads ``StageRun`` rows; ``SessionStateService`` reads
  ``Operation`` / ``AbilityResult`` rows). There is no Caldera/Docker here — the
  channels poll that persisted state and push a frame **on change** and **at
  least every ``interval`` seconds** while the subject is still active. This
  matches the dev constraint: no real emulation, the channels reflect whatever
  the services have persisted.
* **Injectable / shrinkable interval.** The push interval is a parameter with a
  module-level default constant (5s per Req. 1.5). Tests inject a *short/zero*
  interval so one or two frames are produced without waiting 5s.
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
from typing import Awaitable, Callable, Optional

from sqlalchemy.orm import Session
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.models.enums import OperationState, StageState
from app.services.environment.log_buffer import ProvisioningLogBuffer
from app.services.session import SessionStateService
from app.services.stage import StageService
from app.services.stage.stage_service import ESTAGIO_PROVISIONAMENTO, VALID_STAGES

# ---------------------------------------------------------------------------
# Push intervals (Req. 1.5 — "at least every 5 seconds")
# ---------------------------------------------------------------------------

#: Default max interval between stage frames while a stage is ``em_andamento``
#: (Req. 1.5). Tests pass a short/zero value to drive frames without waiting.
STAGE_PUSH_INTERVAL_SECONDS: float = 5.0

#: Default max interval between operation frames while an Operation is running
#: (Req. 4.3). Tests pass a short/zero value.
OPERATION_PUSH_INTERVAL_SECONDS: float = 5.0

#: Terminal stage states — once a stage reaches one of these there will be no
#: further transition, so the channel sends the final frame and closes.
_STAGE_TERMINAL_STATES: frozenset[StageState] = frozenset(
    {StageState.COMPLETED, StageState.ERROR}
)

#: Terminal operation states — final states of an Operation (Req. 4.5). Once
#: reached, the channel sends the final aggregate and closes.
_OPERATION_TERMINAL_STATES: frozenset[OperationState] = frozenset(
    {OperationState.FINISHED, OperationState.ABORTED}
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
# Stage progress channel — WS /ws/stages/{campaign}
# ---------------------------------------------------------------------------


async def run_stage_progress_channel(
    websocket: WebSocket,
    campaign_id: str,
    *,
    session_factory: SessionFactory,
    stage: int = 1,
    interval: float = STAGE_PUSH_INTERVAL_SECONDS,
    max_frames: Optional[int] = None,
    sleeper: Sleeper = _default_sleep,
    log_buffer: Optional[ProvisioningLogBuffer] = None,
) -> int:
    """Push stage state/percentage frames for ``campaign_id`` (Req. 1.4, 1.5).

    Polls :meth:`StageService.stage_progress_event` for ``stage`` of the case
    and sends a typed :class:`~app.services.stage.stage_service.StageProgressEvent`
    frame **on the first poll**, **whenever the state changes** (carrying the
    ``previous_state`` it transitioned from — Req. 1.4), and — while the stage
    is ``em_andamento`` — **at least every ``interval`` seconds** so the live
    percentage keeps flowing (Req. 1.5). When the stage reaches a terminal state
    (``concluido`` / ``erro``) the final frame is sent and the loop stops.

    Provisioning log terminal (Stage 5 — Req. 2.2, 2.5, 2.7)
    --------------------------------------------------------
    When ``stage`` is the provisioning stage
    (:data:`~app.services.stage.stage_service.ESTAGIO_PROVISIONAMENTO`, Stage 5)
    **and** a ``log_buffer`` is injected, each frame also carries the
    provisioning log lines so the on-screen terminal streams the commands and
    their output in near real time (Req. 2.2):

    * **On connect** (the first frame) the frame carries the **full** current
      buffer (:meth:`ProvisioningLogBuffer.read`), so a screen opened late or a
      reconnecting client recovers the whole history of the current execution
      without losing lines (Req. 2.5, 2.7).
    * **On every subsequent frame** it carries only the **delta** — the lines
      emitted since the last frame (:meth:`ProvisioningLogBuffer.new_since`
      from the greatest ``seq`` already sent) — so the terminal appends new
      lines without re-sending the whole buffer (Req. 2.2, 2.5).

    ``last_seq`` (the greatest ``seq`` sent so far) is tracked across iterations
    and advanced from the lines actually included in each frame. For any other
    stage (1–4, 6), or when ``log_buffer`` is ``None``, ``logs`` stays empty and
    the frame is byte-for-byte what the channel sent before — a pure additive,
    backward-compatible change. The process-level shared buffer is wired into
    the router (``app.ws.router`` passes
    :func:`~app.services.environment.log_buffer.get_log_buffer`) and fed by the
    ``EnvironmentStageService`` (task 5.1); here the buffer is only read.

    Any stage in ``VALID_STAGES`` (1..6 — Req. 1.2, 3.1) may be reported;
    the push loop is stage-agnostic, reading whatever the ``StageRun`` for
    ``(campaign_id, stage)`` holds. Because the valid range is sourced from
    ``VALID_STAGES`` (widened to ``(1, 2, 3, 4, 5, 6)`` in ``stage_service``),
    this channel covers the full six-stage pipeline without changes here. An
    ``stage`` outside ``VALID_STAGES`` is
    handled gracefully — mirroring the operation channel's missing-subject
    convention — by sending a single informational ``invalido`` frame and
    closing, rather than letting the service's stage guard raise into the socket.

    Args:
        websocket: The connected client socket (already ``accept``-ed).
        campaign_id: The curated case slug.
        session_factory: Zero-arg factory returning a fresh SQLAlchemy session
            per poll (``SessionLocal`` in the app; an ephemeral factory in
            tests). Each session is closed right after the read.
        stage: Which stage to report — one of ``VALID_STAGES`` (1..6: 1 STIX
            base selection, 2 campaign selection, 3 DAG generation, 4
            operationalization, 5 simulation environment creation, 6 execution +
            measurement + report). Defaults to Stage 1.
        interval: Max seconds between frames while ``em_andamento`` (Req. 1.5).
            A short/zero value (tests) drives frames without waiting.
        max_frames: Optional cap on the number of frames to send before
            returning (tests use it to bound an otherwise-open loop). ``None``
            means "until terminal state or disconnect".
        sleeper: Injectable async sleep (defaults to ``asyncio.sleep``).
        log_buffer: Optional process-level provisioning log buffer. When injected
            and ``stage`` is the provisioning stage (Stage 5), the frame's
            ``logs`` carry the full buffer on connect and the delta thereafter
            (Req. 2.2, 2.5, 2.7). ``None`` (the default) keeps ``logs`` empty —
            the pre-existing behavior — for every stage.

    Returns:
        The number of frames actually sent (useful for tests/metrics).
    """
    frames_sent = 0
    previous_state: Optional[StageState] = None
    # Stage 5 log terminal (Req. 2.2, 2.5, 2.7): we only carry ``logs`` for the
    # provisioning stage and when a buffer was injected. ``last_seq`` is the
    # greatest ``seq`` already sent — 0 means "nothing sent yet", so the first
    # frame carries the full buffer (connect) and the following ones only the delta.
    carry_logs = log_buffer is not None and stage == ESTAGIO_PROVISIONAMENTO
    last_seq = 0

    # Reject an out-of-range stage gracefully (no uncaught stage guard raised
    # into the socket). Mirrors the operation channel's absent-subject frame:
    # send a single informational frame keyed by the invalid stage, then close.
    if stage not in VALID_STAGES:
        await websocket.send_json(
            {
                "type": "stage",
                "campaign_id": campaign_id,
                "stage": stage,
                "valid": False,
                "message": (
                    f"Invalid stage: {stage!r}. Valid stages are "
                    f"{VALID_STAGES}."
                ),
            }
        )
        return 1

    try:
        while True:
            # Fresh short-lived session per poll: read the latest committed
            # state and release the connection immediately.
            session = session_factory()
            try:
                service = StageService(session)
                # ``previous_state`` is the state we last reported. On the very
                # first frame it is ``None`` (no prior state to transition from);
                # afterwards it lets the UI see the transition it just made
                # (Req. 1.4). When the state is unchanged this is a periodic
                # keep-alive frame carrying the live percentage (Req. 1.5).
                event = service.stage_progress_event(
                    campaign_id,
                    stage,
                    previous_state=previous_state,
                )
            finally:
                session.close()

            # Stage 5 log terminal (Req. 2.2, 2.5, 2.7). On connect
            # (``last_seq == 0``) we send the full buffer so the screen recovers
            # the whole history of the current execution; on the following frames,
            # only the delta since the greatest ``seq`` already sent.
            # ``new_since(campaign, 0)`` is equivalent to ``read(campaign)``, so
            # connect uses the same path.
            if carry_logs:
                assert log_buffer is not None  # guaranteed by ``carry_logs``
                event.logs = log_buffer.new_since(campaign_id, last_seq)
                if event.logs:
                    last_seq = max(line.seq for line in event.logs)

            await websocket.send_json(event.model_dump(mode="json"))
            frames_sent += 1
            current_state = event.state
            previous_state = current_state

            # Stop once the stage can no longer change, or the frame cap is hit.
            if current_state in _STAGE_TERMINAL_STATES:
                break
            if max_frames is not None and frames_sent >= max_frames:
                break

            await sleeper(interval)
    except WebSocketDisconnect:
        # Client went away: end quietly (Req.: graceful disconnect handling).
        pass

    return frames_sent


# ---------------------------------------------------------------------------
# Operation progress channel — WS /ws/operation/{operation}
# ---------------------------------------------------------------------------


def _operation_view(
    service: SessionStateService, operation_id: int
):
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
    max_frames: Optional[int] = None,
    sleeper: Sleeper = _default_sleep,
) -> int:
    """Push per-Ability status + final aggregate for an Operation (Req. 4.3–4.5).

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
    last_signature: Optional[str] = None

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
                        "message": (
                            "No Operation persisted for the given id."
                        ),
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
    "STAGE_PUSH_INTERVAL_SECONDS",
    "OPERATION_PUSH_INTERVAL_SECONDS",
    "run_stage_progress_channel",
    "run_operation_progress_channel",
]
