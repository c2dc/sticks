"""Root WebSocket/SSE router (task 11.3).

Wires the real-time progress channel described in the design's *WebSocket/SSE
channels* section:

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
The route accepts an optional ``interval`` (seconds) query param so a client -
and, importantly, the test suite - can shrink the push interval to drive a
couple of frames without waiting, and ``max_frames`` to bound the loop.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, WebSocket

from app.ws.channels import (
    OPERATION_PUSH_INTERVAL_SECONDS,
    run_operation_progress_channel,
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
