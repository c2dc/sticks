"""Property test for portable-enum CHECK rejection on the AI bookkeeping enums (task 2.3).

# Feature: ai-curation-agents, Property 1 (support): origin stamping / portable
# enum integrity — the portable enums ``AgentRunStatus``, ``AgentStage`` and
# ``EvaluatorDecision`` persist as a VARCHAR bounded to exactly their defined
# members. For ANY value *outside* an enum's members, writing it to that enum's
# column is REJECTED and the pre-existing target row is left unchanged.

Validates: Requirements 11.2, 11.3

This is a *feature* property test (``ai-curation-agents`` is a feature spec, not
a bugfix): the property is expected to **hold** for the current definition of
:func:`app.models.domain._portable_enum` together with the enums
:class:`~app.models.enums.AgentRunStatus`,
:class:`~app.models.enums.AgentStage` and
:class:`~app.models.enums.EvaluatorDecision` (added in task 2.1).

What the property guarantees
----------------------------
Requirement 11.2 requires each of the three enums to be stored as a portable
VARCHAR column constrained so that any value outside the enum's defined members
is rejected. Requirement 11.3 requires that, IF an out-of-member value is
written to such a column, the write is rejected and the target row is left
unchanged.

The project builds these columns with :func:`app.models.domain._portable_enum`
(``native_enum=False``, ``validate_strings=True``, pt-BR *values* persisted via
``values_callable``). That portable mechanism bounds the value set to exactly
the enum members on every backend the project runs on:

* on PostgreSQL (the product database) the non-native ``Enum`` is a VARCHAR
  guarded by a CHECK over the member values;
* on the ephemeral SQLite used by the unit tests the same portable column
  rejects an out-of-member value at the typed-column write path
  (``validate_strings=True``) before any row is inserted or updated.

Either way the out-of-member write never lands and the existing row keeps its
original value, which is exactly Requirements 11.2 / 11.3.

How the property is driven
--------------------------
For each of the three enums the test builds a **minimal probe table** on a
private declarative base, binding the column via the real
:func:`app.models.domain._portable_enum` (so the production portable-enum
mechanism is exercised, not a copy). The AI persistence table models
(``agent_runs`` etc.) are defined concurrently in task 3.1; binding a minimal
probe table keeps this property independent of that work while still testing the
exact column type those tables will use.

Hypothesis generates strings that are guaranteed to be **outside** the enum's
members (any member value is rejected by ``assume``). For each example a fresh
temp-file SQLite database is built by the ORM (same pattern as
``test_flow_entity_property.py`` / ``test_origin_persisted_extensible_property.py``),
a single valid row is seeded, and then the out-of-member value is written both
as an INSERT of a new row and as an UPDATE of the seeded row. The property
asserts that:

1. both writes are rejected (a ``StatementError``/``LookupError``/``ValueError``
   is raised — the portable guard firing); and
2. the seeded row is unchanged (its value is still the one seeded) and no
   extra row was persisted.

The assertions never trust the implementation to decide the expected outcome.
Each example builds and discards its own database (no ``.db`` state leaks),
offline, with no network/Docker.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import Settings
from app.db.session import create_db_engine
from app.models.domain import _portable_enum
from app.models.enums import AgentRunStatus, AgentStage, EvaluatorDecision

# The three portable enums under test (Req. 11.2) and their column names.
_ENUMS = {
    "agent_run_status": AgentRunStatus,
    "agent_stage": AgentStage,
    "evaluator_decision": EvaluatorDecision,
}

# Every valid member *value* across the three enums. A generated string is only
# an "out-of-member" value when it is not one of these (and not a member *name*,
# which validate_strings also accepts via the enum class).
_ALL_VALID_VALUES = {m.value for e in _ENUMS.values() for m in e}
_ALL_VALID_NAMES = {m.name for e in _ENUMS.values() for m in e}

# A valid seed member for each enum (first declared member).
_SEED_MEMBER = {name: next(iter(e)) for name, e in _ENUMS.items()}


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example),
# mirroring test_flow_entity_property.py.
# ---------------------------------------------------------------------------


def _build_ephemeral_session(base: type[DeclarativeBase]) -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + probe schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward (no ``.db`` state leaks between examples).
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_portable_enum_check_")
    db_path = Path(tmp_dir) / "portable_enum_check.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


def _make_probe_model(column_name: str, enum_cls: type) -> tuple[type, type]:
    """Build a private ``Base`` + minimal probe table binding the portable enum.

    A dedicated declarative base per call keeps each enum's probe table isolated
    (no table-name clashes on the shared metadata) and independent of the AI
    persistence tables defined concurrently in task 3.1.
    """

    class Base(DeclarativeBase):
        pass

    # Classic (non-annotated) column mapping: the column's SQLAlchemy type is the
    # runtime-built portable enum, so a static ``Mapped[...]`` annotation cannot
    # name it. This still produces a fully mapped ORM class whose ``status``
    # attribute writes go through the portable-enum guard.
    class Probe(Base):
        __tablename__ = "probe"

        id = Column(Integer, primary_key=True)
        label = Column(String, nullable=False)
        status = Column(_portable_enum(enum_cls, column_name), nullable=False)

    return Base, Probe


# ---------------------------------------------------------------------------
# Property 1 (support): a value outside an enum's members is rejected on write
# and the target row is left unchanged (Req. 11.2, 11.3).
#
# ``deadline=None``: each example builds and discards a temp-file SQLite DB,
# whose I/O may exceed Hypothesis' default deadline. The per-example DB is
# intentional (no state leak), so we disable the deadline rather than weaken the
# isolation.
# ---------------------------------------------------------------------------
# Feature: ai-curation-agents, Property 1 (support): origin stamping / portable
# enum integrity — out-of-member values are rejected on write.
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    column_name=st.sampled_from(sorted(_ENUMS.keys())),
    bad_value=st.text(min_size=1, max_size=32),
)
def test_property1_portable_enum_rejects_out_of_member_values(
    column_name: str, bad_value: str
) -> None:
    """Writing a value outside the enum's members is rejected; the row is unchanged.

    # Feature: ai-curation-agents, Property 1 (support): origin stamping /
    # portable enum integrity.
    Validates: Requirements 11.2, 11.3
    """
    # Only exercise strings that are genuinely outside the enum's accepted set.
    # ``validate_strings`` accepts both member values and member names, so both
    # are excluded here to keep ``bad_value`` strictly out-of-member.
    assume(bad_value not in _ALL_VALID_VALUES)
    assume(bad_value not in _ALL_VALID_NAMES)

    enum_cls = _ENUMS[column_name]
    seed_member = _SEED_MEMBER[column_name]
    base, probe_cls = _make_probe_model(column_name, enum_cls)

    session, db_path, engine = _build_ephemeral_session(base)
    try:
        # Seed one valid row — the "target row" whose integrity Req. 11.3 protects.
        session.add(probe_cls(id=1, label="seed", status=seed_member))
        session.commit()
        session.expunge_all()

        # (1) INSERT of a NEW row carrying the out-of-member value must be rejected.
        insert_rejected = False
        try:
            session.add(probe_cls(id=2, label="bad-insert", status=bad_value))
            session.commit()
        except (StatementError, LookupError, ValueError):
            insert_rejected = True
        finally:
            session.rollback()
        assert insert_rejected, (
            f"out-of-member INSERT {bad_value!r} into {column_name} was not rejected"
        )

        # (2) UPDATE of the seeded row to the out-of-member value must be rejected.
        update_rejected = False
        try:
            target = session.get(probe_cls, 1)
            assert target is not None
            target.status = bad_value  # type: ignore[assignment] -- deliberately invalid enum value to prove the CHECK constraint rejects it
            session.commit()
        except (StatementError, LookupError, ValueError):
            update_rejected = True
        finally:
            session.rollback()
        assert update_rejected, (
            f"out-of-member UPDATE {bad_value!r} on {column_name} was not rejected"
        )

        # The target row is unchanged: still the seeded member, via the ORM...
        session.expunge_all()
        reloaded = session.get(probe_cls, 1)
        assert reloaded is not None
        assert reloaded.status is seed_member

        # ...and the raw value on disk is the seeded pt-BR value, with exactly
        # one row persisted (the rejected INSERT never landed) — Req. 11.3.
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT status FROM probe WHERE id = 1")).scalar_one()
            count = conn.execute(text("SELECT COUNT(*) FROM probe")).scalar_one()
        assert stored == seed_member.value
        assert count == 1
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
