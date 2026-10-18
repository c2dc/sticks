"""Root WebSocket/SSE router (task 11.3).

Wires the two real-time progress channels described in the design's *WebSocket/SSE
channels* section:

* ``WS /ws/stages/{campaign}`` — pushes a stage's state transitions and progress
  percentage; while a stage is ``em_andamento`` it re-pushes at least every 5s
  (Req. 1.4, 1.5). For the provisioning stage (Stage 5) the channel also
  carries the live log lines, reading the **process-level shared**
  :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer` injected
  here via :func:`~app.services.environment.log_buffer.get_log_buffer` — the
  same instance the ``EnvironmentStageService`` feeds and the REST endpoints
  read (Req. 2.2, 2.5, 2.7).
* ``WS /ws/operation/{operation}`` — pushes per-Ability status
  (``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``), command output and
  the aggregate at the end (Req. 4.3, 4.4, 4.5).

The actual push loops live in :mod:`app.ws.channels`; the routes here only
``accept`` the socket, parse path/query params and hand off. The DB session
factory defaults to the app's ``SessionLocal`` (a fresh short-lived session is
opened per poll inside the loop) but is resolved lazily so tests can override
``app.db.session.SessionLocal`` with an ephemeral SQLite factory.

Query parameters
----------------
Both routes accept an optional ``interval`` (seconds) query param so a client —
and, importantly, the test suite — can shrink the 5s push interval to drive a
couple of frames without waiting. ``/ws/stages/{campaign}`` also accepts
``stage`` (one of ``VALID_STAGES`` — 1..6, default 1) selecting which stage to
report; an out-of-range value yields a single ``invalid`` frame and closes.
Both routes accept ``max_frames`` to bound the loop.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, WebSocket

from app.services.environment.log_buffer import get_log_buffer
from app.ws.channels import (
    OPERATION_PUSH_INTERVAL_SECONDS,
    STAGE_PUSH_INTERVAL_SECONDS,
    run_operation_progress_channel,
    run_stage_progress_channel,
)

ws_router = APIRouter()


def _session_factory():
    """Return the app's session factory.

    Imported lazily (inside the function) so that a test which monkeypatches
    ``app.db.session.SessionLocal`` before connecting is honored — the module is
    read at call time, not at import time.
    """
    from app.db.session import SessionLocal

    return SessionLocal


@ws_router.websocket("/ws/stages/{campaign}")
async def ws_stages(
    websocket: WebSocket,
    campaign: str,
    stage: int = 1,
    interval: float = STAGE_PUSH_INTERVAL_SECONDS,
    max_frames: Optional[int] = None,
) -> None:
    """Stage-progress channel for a case (Req. 1.4, 1.5, 1.6).

    Accepts the socket and streams :class:`StageProgressEvent` frames for
    ``campaign`` / ``stage``: on connect, on every state transition, and at
    least every ``interval`` seconds while the stage is ``em_andamento`` —
    closing once the stage reaches ``concluido`` / ``erro``. ``stage`` may be
    any of ``VALID_STAGES`` (1..6: 1 STIX base selection, 2 campaign selection,
    3 DAG generation, 4 operationalization, 5 simulation environment creation, 6
    execution + measurement + report); an out-of-range value yields a single
    ``invalid`` frame and closes.
    """
    await websocket.accept()
    # Forward the process-level shared log buffer (task 6.1): the **same**
    # instance the ``EnvironmentStageService`` feeds during Stage 5 provisioning,
    # so the channel carries the live terminal lines (Req. 2.2, 2.5, 2.7). For
    # the other Stages the ``logs`` field stays empty (the channel only fills it
    # for the provisioning Stage).
    await run_stage_progress_channel(
        websocket,
        campaign,
        session_factory=_session_factory(),
        stage=stage,
        interval=interval,
        max_frames=max_frames,
        log_buffer=get_log_buffer(),
    )


@ws_router.websocket("/ws/operation/{operation}")
async def ws_operation(
    websocket: WebSocket,
    operation: int,
    interval: float = OPERATION_PUSH_INTERVAL_SECONDS,
    max_frames: Optional[int] = None,
) -> None:
    """Operation-progress channel for an Operation (Req. 4.3, 4.4, 4.5).

    Accepts the socket and streams per-Ability status + command output frames
    for the persisted Operation ``operation``, plus the aggregate when the
    Operation reaches its final state — then closes. A missing Operation yields
    a single ``not_found`` frame.
    """
    await websocket.accept()
    await run_operation_progress_channel(
        websocket,
        operation,
        session_factory=_session_factory(),
        interval=interval,
        max_frames=max_frames,
    )
