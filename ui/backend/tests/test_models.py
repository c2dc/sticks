"""Unit tests for the domain models and enums (task 2.3).

These tests exercise three concerns against an ephemeral SQLite database built
via the app's ``Base``/``create_all`` (the same wiring the dev SQLite default
and Alembic target):

1. Column defaults — ``Theme`` defaults to ``LIGHT`` ("claro"), ``Language``
   defaults to ``PT_BR`` ("pt-BR") and ``origem_traducao`` defaults to
   ``HUMAN_CURATION`` ("curadoria_humana") on ``Case``/``Ability``/``Adversary``.
   _Requisitos: 7.6, 8.6, 6.8, 11.1_
2. Nullability constraints — ``AuditLogEntry.comando``, ``container_destino``
   and ``registrado_em`` are NOT NULL: inserting NULL raises ``IntegrityError``.
   _Requisitos: 6.8_
3. Enum persistence — enum columns persist as the Portuguese *string values*
   on SQLite (portable ``native_enum=False`` mapping), not the member names.
   _Requisitos: 11.1, 7.6, 8.6_

Each test uses a temp-file SQLite engine created from a ``Settings`` override
(mirroring ``test_smoke_startup.py``) and removes the file afterward, so no
``.db`` state leaks.

_Requisitos: 7.6, 8.6, 6.8, 11.1_
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    Ability,
    Adversary,
    AuditLogEntry,
    Case,
    UserPreferences,
)
from app.models.enums import Language, Theme, TranslationSource


@pytest.fixture()
def sqlite_engine() -> Iterator[Engine]:
    """Yield an ephemeral temp-file SQLite engine with the full schema built.

    A temp file (not ``:memory:``) is used so ``create_all`` runs against a real
    file engine — the same path the dev SQLite default would take — and NOT NULL
    ``CHECK`` behavior matches production. The file is removed afterward so no
    ``.db`` state leaks.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_models_")
    db_path = Path(tmp_dir) / "models.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
    try:
        import_models()
        Base.metadata.create_all(bind=engine)
        yield engine
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(tmp_dir)

    assert not db_path.exists()


@pytest.fixture()
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    """Yield a session bound to the ephemeral engine, closed after each test."""
    session_factory: sessionmaker[Session] = sessionmaker(
        bind=sqlite_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1. Defaults
# ---------------------------------------------------------------------------


def test_user_preferences_theme_defaults_to_light(db_session: Session) -> None:
    """Theme default is LIGHT ("claro") when not provided. _Requisitos: 7.6_"""
    prefs = UserPreferences()
    db_session.add(prefs)
    db_session.commit()
    db_session.refresh(prefs)

    assert prefs.tema is Theme.LIGHT
    assert prefs.tema.value == "claro"


def test_user_preferences_language_defaults_to_pt_br(db_session: Session) -> None:
    """Language default is PT_BR ("pt-BR") when not provided. _Requisitos: 8.6_"""
    prefs = UserPreferences()
    db_session.add(prefs)
    db_session.commit()
    db_session.refresh(prefs)

    assert prefs.idioma is Language.PT_BR
    assert prefs.idioma.value == "pt-BR"


def test_case_origem_traducao_defaults_to_human_curation(db_session: Session) -> None:
    """Case.origem_traducao default is HUMAN_CURATION. _Requisitos: 11.1_"""
    case = Case(id="shadowray", nome="ShadowRay")
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)

    assert case.origem_traducao is TranslationSource.HUMAN_CURATION
    assert case.origem_traducao.value == "curadoria_humana"


def test_ability_origem_traducao_defaults_to_human_curation(
    db_session: Session,
) -> None:
    """Ability.origem_traducao default is HUMAN_CURATION. _Requisitos: 11.1_"""
    ability = Ability(ability_id="ability-1")
    db_session.add(ability)
    db_session.commit()
    db_session.refresh(ability)

    assert ability.origem_traducao is TranslationSource.HUMAN_CURATION
    assert ability.origem_traducao.value == "curadoria_humana"


def test_adversary_origem_traducao_defaults_to_human_curation(
    db_session: Session,
) -> None:
    """Adversary.origem_traducao default is HUMAN_CURATION. _Requisitos: 11.1_"""
    adversary = Adversary(id="adversary-1")
    db_session.add(adversary)
    db_session.commit()
    db_session.refresh(adversary)

    assert adversary.origem_traducao is TranslationSource.HUMAN_CURATION
    assert adversary.origem_traducao.value == "curadoria_humana"


# ---------------------------------------------------------------------------
# 2. Nullability constraints on AuditLogEntry (_Requisitos: 6.8_)
# ---------------------------------------------------------------------------


def _valid_audit_kwargs() -> dict[str, object]:
    """Return kwargs for a fully valid AuditLogEntry (all NOT NULL set)."""
    return {
        "comando": "wget http://192.168.20.30/payload",
        "container_destino": "nginx (192.168.20.30)",
        "registrado_em": dt.datetime(2024, 1, 1, 12, 0, 0),
    }


def test_audit_log_entry_accepts_all_required_fields(db_session: Session) -> None:
    """A fully populated AuditLogEntry persists cleanly. _Requisitos: 6.8_"""
    entry = AuditLogEntry(**_valid_audit_kwargs())
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)

    assert entry.id is not None
    assert entry.comando == "wget http://192.168.20.30/payload"
    assert entry.container_destino == "nginx (192.168.20.30)"
    assert entry.registrado_em == dt.datetime(2024, 1, 1, 12, 0, 0)


@pytest.mark.parametrize(
    "missing_field",
    ["comando", "container_destino", "registrado_em"],
)
def test_audit_log_entry_null_required_field_raises(
    db_session: Session, missing_field: str
) -> None:
    """Inserting NULL into a NOT NULL AuditLogEntry column raises
    IntegrityError. _Requisitos: 6.8_"""
    kwargs = _valid_audit_kwargs()
    kwargs[missing_field] = None
    entry = AuditLogEntry(**kwargs)
    db_session.add(entry)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


# ---------------------------------------------------------------------------
# 3. Enum values persist as the Portuguese string values on SQLite
# ---------------------------------------------------------------------------


def test_theme_and_language_persist_as_portuguese_strings(
    db_session: Session, sqlite_engine: Engine
) -> None:
    """UserPreferences enum columns store the Portuguese *values* on disk.

    We read the raw columns back with a plain SQL query (bypassing the ORM's
    enum coercion) to confirm the stored text is ``"escuro"``/``"en"`` — the
    member *values*, not the member names (``DARK``/``EN``).
    _Requisitos: 7.6, 8.6_
    """
    prefs = UserPreferences(tema=Theme.DARK, idioma=Language.EN)
    db_session.add(prefs)
    db_session.commit()
    db_session.refresh(prefs)

    with sqlite_engine.connect() as conn:
        row = conn.execute(
            text("SELECT tema, idioma FROM preferencias WHERE id = :id"),
            {"id": prefs.id},
        ).one()

    assert row.tema == "escuro"
    assert row.idioma == "en"


def test_translation_source_persists_as_portuguese_string(
    db_session: Session, sqlite_engine: Engine
) -> None:
    """Case.origem_traducao stores ``"curadoria_humana"`` on disk (value, not
    the member name ``HUMAN_CURATION``). _Requisitos: 11.1_"""
    case = Case(
        id="apt41_dust",
        nome="APT41-DUST",
        origem_traducao=TranslationSource.HUMAN_CURATION,
    )
    db_session.add(case)
    db_session.commit()

    with sqlite_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT origem_traducao FROM casos WHERE id = :id"),
            {"id": "apt41_dust"},
        ).scalar_one()

    assert stored == "curadoria_humana"


def test_enum_value_roundtrips_back_to_member(db_session: Session) -> None:
    """Reading a persisted enum column returns the proper enum member.
    _Requisitos: 7.6, 8.6_"""
    prefs = UserPreferences(tema=Theme.DARK, idioma=Language.EN)
    db_session.add(prefs)
    db_session.commit()
    prefs_id = prefs.id

    db_session.expunge_all()
    loaded = db_session.get(UserPreferences, prefs_id)

    assert loaded is not None
    assert loaded.tema is Theme.DARK
    assert loaded.idioma is Language.EN
