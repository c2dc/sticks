"""Shared test helper: seed curated campaigns as ORM rows in a temporary SQLite.

The curated JSON files on disk and the ``CaseService`` that read them were
removed (task 21.2): the database is the single source of truth. Tests that need
a curated case now persist it as ORM rows (``campaigns`` with ``curated=True``,
``abilities``, ``adversaries`` and an ``attack_flows`` row) in an ephemeral
SQLite database, and the code under test reads it back through
:mod:`app.services.curated_case_reader`.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Optional, Union

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Ability, Adversary, AttackFlowRow, Case
from app.models.enums import FlowOrigin, TranslationSource

# The 8 curated campaigns of the product (slug -> display name), as persisted in
# the user's database. Test-only constant: production reads ``campaigns.curated``.
CURATED_NAMES: dict[str, str] = {
    "apt41_dust": "APT41-DUST",
    "c0010": "ATT&CK Campaign C0010",
    "c0026": "ATT&CK Campaign C0026",
    "costaricto": "CostaRicto",
    "operation_midnighteclipse": "Operation MidnightEclipse",
    "outer_space": "Operation Outer Space",
    "salesforce_data_exfiltration": "Salesforce Data Exfiltration",
    "shadowray": "ShadowRay",
}


class CaseDb:
    """An ephemeral SQLite database with the full schema (``factory`` yields sessions)."""

    def __init__(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="sticks_case_db_"))
        db_path = self._tmp / "case.db"
        self._engine = create_db_engine(
            Settings(database_url=f"sqlite:///{db_path.as_posix()}")
        )
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

    def __enter__(self) -> "CaseDb":
        return self

    def __exit__(self, *exc: object) -> None:
        self.dispose()

    def session(self) -> Session:
        return self.factory()


class SessionSeeder:
    """Seeds curated rows into an already-open session (used by session-bound services)."""

    def __init__(self, session: Session) -> None:
        self.session = session


def seed_case_rows(
    session: Session,
    slug: str,
    *,
    name: Optional[str] = None,
    curated: bool = True,
    abilities: Iterable[Mapping[str, Any]] = (),
    adversary: Optional[Mapping[str, Any]] = None,
    flow: Optional[Mapping[str, Any]] = None,
) -> None:
    """Persist one campaign (row, abilities, adversary, flow) into ``session``."""
    ensure_case_row(session, slug, name=name, curated=curated)
    for ability in abilities:
        session.add(
            Ability(
                campaign_id=slug,
                translation_source=TranslationSource.HUMAN_CURATION,
                **dict(ability),
            )
        )
    if adversary is not None:
        session.add(
            Adversary(
                campaign_id=slug,
                translation_source=TranslationSource.HUMAN_CURATION,
                **dict(adversary),
            )
        )
    if flow is not None:
        session.add(_flow_row(slug, flow))
    session.commit()


def ensure_case_row(
    session: Session, slug: str, *, name: Optional[str] = None, curated: bool = True
) -> None:
    """Create the ``campaigns`` row of ``slug`` when it does not exist yet."""
    if session.get(Case, slug) is None:
        session.add(
            Case(
                id=slug,
                name=name or CURATED_NAMES.get(slug, slug),
                translation_source=TranslationSource.HUMAN_CURATION,
                curated=curated,
            )
        )
        session.flush()


def _flow_row(slug: str, content: Mapping[str, Any]) -> AttackFlowRow:
    return AttackFlowRow(
        campaign_id=slug,
        origin=FlowOrigin.HUMAN_CURATION,
        identifier="human-curation-v1",
        label=f"{slug} flow",
        content=dict(content),
        created_at=dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )


def seed_case(db: CaseDb, slug: str, **kwargs: Any) -> None:
    """Open a session on ``db`` and persist one campaign (see :func:`seed_case_rows`)."""
    session = db.factory()
    try:
        seed_case_rows(session, slug, **kwargs)
    finally:
        session.close()


def db_put(
    target: Union[CaseDb, SessionSeeder], kind: str, slug: str, payload: Any
) -> None:
    """Persist one piece of a curated case: ``ability`` | ``adversary`` | ``flow``.

    ``payload`` mirrors the content the removed curated JSON files used to hold:
    a list of ability dicts, the adversary dict (empty/``None`` = no adversary)
    or the flow dict (``structural_nodes`` ...). The ``campaigns`` row is created
    (``curated=True``) on first use.
    """
    own_session = not isinstance(target, SessionSeeder)
    session = target.factory() if own_session else target.session  # type: ignore[union-attr]
    try:
        ensure_case_row(session, slug)
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
        else:  # pragma: no cover - programming error in a test
            raise ValueError(f"unknown kind: {kind!r}")
        session.commit()
    finally:
        if own_session:
            session.close()
