"""Test helper: seed a curated campaign as ORM rows in a temporary SQLite database.

The database is the single source of truth for curated cases, so the one test
that needs a case persists it as rows (``campaigns`` with ``curated=True``,
``abilities``, ``adversaries`` and an ``attack_flows`` row).
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Ability, Adversary, AttackFlowRow, Case
from app.models.enums import FlowOrigin, TranslationSource


class CaseDb:
    """An ephemeral SQLite database with the full schema (``factory`` yields sessions)."""

    def __init__(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="sticks_case_db_"))
        db_path = self._tmp / "case.db"
        self._engine = create_db_engine(Settings(database_url=f"sqlite:///{db_path.as_posix()}"))
        import_models()
        Base.metadata.create_all(bind=self._engine)
        self.factory: sessionmaker[Session] = sessionmaker(
            bind=self._engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            future=True,
            class_=Session,
        )

    def dispose(self) -> None:
        self._engine.dispose()
        shutil.rmtree(self._tmp, ignore_errors=True)


def _ensure_case_row(session: Session, slug: str) -> None:
    if session.get(Case, slug) is None:
        session.add(
            Case(
                id=slug,
                name=slug,
                translation_source=TranslationSource.HUMAN_CURATION,
                curated=True,
            )
        )
        session.flush()


def db_put(target: CaseDb, kind: str, slug: str, payload: Any) -> None:
    """Persist one piece of a curated case: ``ability`` | ``adversary`` | ``flow``.

    ``payload`` is a list of ability dicts, the adversary dict (empty/``None`` =
    no adversary) or the flow dict. The ``campaigns`` row is created on first use.
    """
    session = target.factory()
    try:
        _ensure_case_row(session, slug)
        if kind == "ability":
            for ability in payload or []:
                session.add(
                    Ability(
                        campaign_id=slug,
                        translation_source=TranslationSource.HUMAN_CURATION,
                        **dict(ability),
                    )
                )
        elif kind == "adversary":
            if payload:
                session.add(
                    Adversary(
                        campaign_id=slug,
                        translation_source=TranslationSource.HUMAN_CURATION,
                        **dict(payload),
                    )
                )
        elif kind == "flow":
            if payload is not None:
                session.add(_flow_row(slug, payload))
        else:
            raise ValueError(f"unknown kind: {kind!r}")
        session.commit()
    finally:
        session.close()


def _flow_row(slug: str, content: Mapping[str, Any]) -> AttackFlowRow:
    return AttackFlowRow(
        campaign_id=slug,
        origin=FlowOrigin.HUMAN_CURATION,
        identifier="human-curation-v1",
        label=f"{slug} flow",
        content=dict(content),
        created_at=dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )
