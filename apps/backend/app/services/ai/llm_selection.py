"""Selected language-model profile: persistence and per-run resolution.

The selected profile id lives in ``preferences.llm_profile_id`` (``NULL`` means
the work profile). :func:`resolve_active_profile` is what ``make_chat_model``
uses: the profile frozen for the run in progress, otherwise the persisted
selection read once from the database and kept in memory.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import Language, Theme, UserPreferences
from app.services.ai.llm_profiles import (
    WORK_PROFILE_ID,
    LlmProfile,
    build_work_profile,
    find_profile,
)

__all__ = [
    "load_selected_profile_id",
    "resolve_active_profile",
    "run_profile",
    "save_selected_profile_id",
]

_lock = threading.Lock()
# Selection cached after the first database read; reset when the selection is saved.
_cached_profile_id: str | None = None
# Profile frozen for the run in progress (a plain global: graph nodes may run in other threads).
_frozen_profile: LlmProfile | None = None


def load_selected_profile_id(session: Session) -> str:
    """Return the persisted profile id, or ``work`` when unset or unreadable."""
    try:
        prefs = (
            session.execute(select(UserPreferences).order_by(UserPreferences.id.asc()))
            .scalars()
            .first()
        )
    except SQLAlchemyError:
        # Column or table missing (migration not applied): fall back to the work profile.
        session.rollback()
        return WORK_PROFILE_ID
    if prefs is None or not prefs.llm_profile_id:
        return WORK_PROFILE_ID
    return prefs.llm_profile_id


def save_selected_profile_id(session: Session, profile_id: str) -> None:
    """Persist ``profile_id`` and reset the in-memory cache.

    Creates the preferences row with the default theme and language when absent.
    """
    global _cached_profile_id
    prefs = (
        session.execute(select(UserPreferences).order_by(UserPreferences.id.asc()))
        .scalars()
        .first()
    )
    if prefs is None:
        prefs = UserPreferences(theme=Theme.LIGHT, language=Language.PT_BR)
        session.add(prefs)
    prefs.llm_profile_id = profile_id
    session.commit()
    with _lock:
        _cached_profile_id = None


def _read_selected_profile_id() -> str:
    """Read the selection once through ``SessionLocal`` and keep it in memory."""
    global _cached_profile_id
    with _lock:
        if _cached_profile_id is not None:
            return _cached_profile_id
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        profile_id = load_selected_profile_id(session)
    with _lock:
        _cached_profile_id = profile_id
    return profile_id


def resolve_active_profile() -> LlmProfile:
    """Return the frozen run profile, else the selected profile (work if invalid)."""
    with _lock:
        frozen = _frozen_profile
    if frozen is not None:
        return frozen
    return find_profile(_read_selected_profile_id()) or build_work_profile()


@contextmanager
def run_profile(profile: LlmProfile) -> Iterator[LlmProfile]:
    """Freeze ``profile`` for every model call made inside the block."""
    global _frozen_profile
    with _lock:
        previous = _frozen_profile
        _frozen_profile = profile
    try:
        yield profile
    finally:
        with _lock:
            _frozen_profile = previous
