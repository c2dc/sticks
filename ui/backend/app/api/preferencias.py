"""REST endpoint for user preferences — theme + language (task 11.1).

``PUT /api/preferencias`` updates and persists the Pesquisador's theme and
language on the single ``preferencias`` row (``UserPreferences``). The defaults
are Modo_Claro (Req. 7.6) and pt-BR (Req. 8.6); this endpoint lets the frontend
persist a change so it can be restored on open.

Handler stays thin: it upserts the single preferences row within the
request-scoped session and returns the persisted values.

_Requisitos: 7.4, 8.4_
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.domain import UserPreferences
from app.models.enums import Language, Theme

router = APIRouter(prefix="/api", tags=["preferencias"])


class PreferencesUpdate(BaseModel):
    """Body of ``PUT /api/preferencias`` (Req. 7.4, 8.4).

    Both fields are optional so the frontend can update theme and language
    independently; omitted fields keep their persisted value.
    """

    tema: Optional[Theme] = None
    idioma: Optional[Language] = None


class PreferencesResponse(BaseModel):
    """The persisted preferences after the update (Req. 7.4, 8.4)."""

    tema: Theme
    idioma: Language


def _get_or_create_preferences(db: Session) -> UserPreferences:
    """Return the single ``preferencias`` row, creating it with defaults if absent.

    One preferences row is kept (lowest ``id`` wins for determinism), mirroring
    the single-row upsert contract used for the session state.
    """
    prefs = db.execute(
        select(UserPreferences).order_by(UserPreferences.id.asc())
    ).scalars().first()
    if prefs is None:
        prefs = UserPreferences(tema=Theme.LIGHT, idioma=Language.PT_BR)
        db.add(prefs)
    return prefs


@router.put(
    "/preferencias",
    response_model=PreferencesResponse,
    summary="Atualiza tema e idioma",
)
def atualizar_preferencias(
    body: PreferencesUpdate,
    db: Session = Depends(get_db),
) -> PreferencesResponse:
    """Persist the theme and/or language preference (Req. 7.4, 8.4).

    Upserts the single preferences row; omitted body fields keep their current
    persisted value. Returns the persisted values.
    """
    prefs = _get_or_create_preferences(db)
    if body.tema is not None:
        prefs.tema = body.tema
    if body.idioma is not None:
        prefs.idioma = body.idioma
    db.commit()
    db.refresh(prefs)
    return PreferencesResponse(tema=prefs.tema, idioma=prefs.idioma)
