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
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.domain import UserPreferences
from app.services.session.session_state_service import (
    PreferenciasView,
    SessionStateService,
    SessionStateView,
)

router = APIRouter(prefix="/api", tags=["estado-sessao"])


def _load_preferencias(db: Session) -> PreferenciasView | None:
    """Read the persisted theme/language preferences, if any.

    Preferences live on the single ``preferencias`` row (lowest ``id`` wins, the
    same single-row convention the PUT endpoint uses). Returns ``None`` when no
    row was persisted yet (first visit) or when the read fails — the frontend
    then falls back to its defaults (Modo_Claro / pt-BR), so restoring
    preferences never blocks opening the app.
    """
    try:
        prefs = (
            db.execute(select(UserPreferences).order_by(UserPreferences.id.asc()))
            .scalars()
            .first()
        )
    except SQLAlchemyError:
        return None
    if prefs is None:
        return None
    return PreferenciasView(tema=prefs.tema.value, idioma=prefs.idioma.value)


@router.get(
    "/estado-sessao",
    response_model=SessionStateView,
    summary="Recupera o Estado_de_Sessão persistido",
)
def obter_estado_sessao(db: Session = Depends(get_db)) -> SessionStateView:
    """Retrieve the persisted Estado_de_Sessão (Req. 9.4).

    Returns the current case + completed-stages-per-case map, plus the persisted
    theme/language preferences so the frontend can restore them on open
    (Req. 7.5, 8.5). A missing state yields the default view (``exists=False``);
    an unreadable state yields ``restore_failed=True`` without discarding the
    persisted row.
    """
    view = SessionStateService(db).get_session_state()
    view.preferencias = _load_preferencias(db)
    return view
