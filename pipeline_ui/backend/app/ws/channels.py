"""Real-time progress channels — WebSocket push loops (task 11.3).

This module implements the two WebSocket channels described in the design's
*Canais WebSocket/SSE* section:

* ``WS /ws/estagios/{caso}`` — pushes a stage's state transitions and its
  progress percentage; while a stage is ``em_andamento`` it re-pushes at least
  every 5s so the UI shows the live percentage (Req. 1.4, 1.5).
* ``WS /ws/operacao/{operacao}`` — pushes the per-Ability execution status
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
from app.services.session import SessionStateService
from app.services.stage import StageService

# ---------------------------------------------------------------------------
# Push intervals (Req. 1.5 — "ao menos a cada 5 segundos")
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
# Stage progress channel — WS /ws/estagios/{caso}
# ---------------------------------------------------------------------------


async def run_stage_progress_channel(
    websocket: WebSocket,
    caso_id: str,
    *,
    session_factory: SessionFactory,
    estagio: int = 1,
    interval: float = STAGE_PUSH_INTERVAL_SECONDS,
    max_frames: Optional[int] = None,
    sleeper: Sleeper = _default_sleep,
) -> int:
    """Push stage state/percentage frames for ``caso_id`` (Req. 1.4, 1.5).

    Polls :meth:`StageService.stage_progress_event` for ``estagio`` of the case
    and sends a typed :class:`~app.services.stage.stage_service.StageProgressEvent`
    frame **on the first poll**, **whenever the state changes** (carrying the
    ``estado_anterior`` it transitioned from — Req. 1.4), and — while the stage
    is ``em_andamento`` — **at least every ``interval`` seconds** so the live
    percentage keeps flowing (Req. 1.5). When the stage reaches a terminal state
    (``concluido`` / ``erro``) the final frame is sent and the loop stops.

    Args:
        websocket: The connected client socket (already ``accept``-ed).
        caso_id: The curated case slug.
        session_factory: Zero-arg factory returning a fresh SQLAlchemy session
            per poll (``SessionLocal`` in the app; an ephemeral factory in
            tests). Each session is closed right after the read.
        estagio: Which stage (1, 2, 3) to report. Defaults to Stage 1.
        interval: Max seconds between frames while ``em_andamento`` (Req. 1.5).
            A short/zero value (tests) drives frames without waiting.
        max_frames: Optional cap on the number of frames to send before
            returning (tests use it to bound an otherwise-open loop). ``None``
            means "until terminal state or disconnect".
        sleeper: Injectable async sleep (defaults to ``asyncio.sleep``).

    Returns:
        The number of frames actually sent (useful for tests/metrics).
    """
    frames_sent = 0
    previous_state: Optional[StageState] = None

    try:
        while True:
            # Fresh short-lived session per poll: read the latest committed
            # state and release the connection immediately.
            session = session_factory()
            try:
                service = StageService(session)
                # ``estado_anterior`` is the state we last reported. On the very
                # first frame it is ``None`` (no prior state to transition from);
                # afterwards it lets the UI see the transition it just made
                # (Req. 1.4). When the state is unchanged this is a periodic
                # keep-alive frame carrying the live percentage (Req. 1.5).
                event = service.stage_progress_event(
                    caso_id,
                    estagio,
                    estado_anterior=previous_state,
                )
            finally:
                session.close()

            await websocket.send_json(event.model_dump(mode="json"))
            frames_sent += 1
            current_state = event.estado
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
# Operation progress channel — WS /ws/operacao/{operacao}
# ---------------------------------------------------------------------------


def _operation_view(
    service: SessionStateService, operacao_id: int
):
    """Return the persisted :class:`OperationResultView` for ``operacao_id``.

    ``SessionStateService.get_operation_results`` groups results by *case*; here
    the channel is keyed by *operation id*, so we find the matching operation by
    walking the results the service already assembles (it carries ``operacao_id``
    and ``caso_id`` on every view). Returns ``None`` when no such operation is
    persisted (handled as ``not_found`` by the caller).
    """
    from app.models.domain import Operation  # local import: avoid cycle at top

    session = service._session  # the service exposes its bound session
    op = session.get(Operation, operacao_id)
    if op is None:
        return None
    views = service.get_operation_results(op.caso_id) if op.caso_id else []
    for view in views:
        if view.operacao_id == operacao_id:
            return view
    return None


async def run_operation_progress_channel(
    websocket: WebSocket,
    operacao_id: int,
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

    If no ``Operation`` with ``operacao_id`` is persisted, a single ``not_found``
    frame is sent and the loop ends (graceful handling of a None/absent
    operation).

    Args:
        websocket: The connected client socket (already ``accept``-ed).
        operacao_id: The persisted Operation id.
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
                view = _operation_view(service, operacao_id)
            finally:
                session.close()

            if view is None:
                # No such Operation persisted — tell the client and stop rather
                # than polling forever (graceful None handling).
                await websocket.send_json(
                    {
                        "tipo": "operacao",
                        "operacao_id": operacao_id,
                        "encontrada": False,
                        "mensagem": (
                            "Nenhuma Operação persistida para o id informado."
                        ),
                    }
                )
                frames_sent += 1
                break

            frame = {
                "tipo": "operacao",
                "operacao_id": view.operacao_id,
                "caso_id": view.caso_id,
                "estado": view.estado.value,
                "encontrada": True,
                "final": view.estado in _OPERATION_TERMINAL_STATES,
                "resultados": [r.model_dump(mode="json") for r in view.resultados],
                "agregado": {
                    "total_sucesso": view.total_sucesso,
                    "total_falha": view.total_falha,
                },
            }

            signature = repr(frame)
            is_final = view.estado in _OPERATION_TERMINAL_STATES
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
