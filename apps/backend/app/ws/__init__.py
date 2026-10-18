"""WebSocket / SSE channels package.

Channels (``/ws/stages/{campaign}``, ``/ws/operation/{operation}``) are
implemented in later tasks. This package is created now so the structure is in
place.
"""

from app.ws.router import ws_router

__all__ = ["ws_router"]
