"""Root WebSocket/SSE router (task 11.3).

Wires the two real-time progress channels described in the design's *Canais
WebSocket/SSE* section:

* ``WS /ws/estagios/{caso}`` — pushes a stage's state transitions and progress
  percentage; while a stage is ``em_andamento`` it re-pushes at least every 5s
  (Req. 1.4, 1.5).
* ``WS /ws/operacao/{operacao}`` — pushes per-Ability status
  (``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``), command output and
  the aggregate at the end (Req. 4.3, 4.4, 4.5).

The actual push loops live in :mod:`app.ws.channels`; the routes here only
``accept`` the socket, parse path/query params and hand off. The DB session
factory defaults to the app's ``SessionLocal`` (a fresh short-lived session is
opened per poll inside the loop) but is resolved lazily so tests can override
``app.db.session.SessionLocal`` with an ephemeral SQLite factory.

Query parameters
----------------
Both routes accept an optional ``intervalo`` (seconds) query param so a client —
and, importantly, the test suite — can shrink the 5s push interval to drive a
couple of frames without waiting. ``/ws/estagios/{caso}`` also accepts
``estagio`` (1/2/3, default 1) selecting which stage to report, and both accept
``max_frames`` to bound the loop.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, WebSocket

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


@ws_router.websocket("/ws/estagios/{caso}")
async def ws_estagios(
    websocket: WebSocket,
    caso: str,
    estagio: int = 1,
    intervalo: float = STAGE_PUSH_INTERVAL_SECONDS,
    max_frames: Optional[int] = None,
) -> None:
    """Stage-progress channel for a case (Req. 1.4, 1.5).

    Accepts the socket and streams :class:`StageProgressEvent` frames for
    ``caso`` / ``estagio``: on connect, on every state transition, and at least
    every ``intervalo`` seconds while the stage is ``em_andamento`` — closing
    once the stage reaches ``concluido`` / ``erro``.
    """
    await websocket.accept()
    await run_stage_progress_channel(
        websocket,
        caso,
        session_factory=_session_factory(),
        estagio=estagio,
        interval=intervalo,
        max_frames=max_frames,
    )


@ws_router.websocket("/ws/operacao/{operacao}")
async def ws_operacao(
    websocket: WebSocket,
    operacao: int,
    intervalo: float = OPERATION_PUSH_INTERVAL_SECONDS,
    max_frames: Optional[int] = None,
) -> None:
    """Operation-progress channel for an Operation (Req. 4.3, 4.4, 4.5).

    Accepts the socket and streams per-Ability status + command output frames
    for the persisted Operation ``operacao``, plus the aggregate when the
    Operation reaches its final state — then closes. A missing Operation yields
    a single ``not_found`` frame.
    """
    await websocket.accept()
    await run_operation_progress_channel(
        websocket,
        operacao,
        session_factory=_session_factory(),
        interval=intervalo,
        max_frames=max_frames,
    )
