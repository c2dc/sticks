"""REST endpoint for the Estado_de_Sessão (task 11.1).

``GET /api/estado-sessao`` retrieves the persisted session state so the
Pesquisador regains continuity on open (Req. 9.4). Delegates to
:meth:`SessionStateService.get_session_state`, which returns a default
all-not-started state when none exists (Req. 9.5) and a ``restore_failed`` view
when the persisted state cannot be read (Req. 9.6) — never raising.

_Requisitos: 9.4_
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.session.session_state_service import (
    SessionStateService,
    SessionStateView,
)

router = APIRouter(prefix="/api", tags=["estado-sessao"])


@router.get(
    "/estado-sessao",
    response_model=SessionStateView,
    summary="Recupera o Estado_de_Sessão persistido",
)
def obter_estado_sessao(db: Session = Depends(get_db)) -> SessionStateView:
    """Retrieve the persisted Estado_de_Sessão (Req. 9.4).

    Returns the current case + completed-stages-per-case map. A missing state
    yields the default view (``exists=False``); an unreadable state yields
    ``restore_failed=True`` without discarding the persisted row.
    """
    return SessionStateService(db).get_session_state()
