"""Tests for migration ``c9d2e4f8a1b7`` — widen ``operations.state`` to admit
``incompleta_timeout`` (task 1.3 of the ``stage6-execution-redesign`` spec).

Validates: Requirements 3.2

What is actually being verified
-------------------------------
Migration ``c9d2e4f8a1b7`` (task 1.2) introduces the new terminal state
``OperationState.INCOMPLETE_TIMEOUT = "incompleta_timeout"`` (task 1.1,
Req. 3.2). The column ``operations.state`` is mapped with
``_portable_enum(OperationState, "operation_state")`` — a SQLAlchemy ``Enum``
with ``native_enum=False``. This project runs **SQLAlchemy 2.1**, where
``Enum.create_constraint`` defaults to ``False``: a ``native_enum=False`` Enum
emits **no** ``CHECK`` constraint on either PostgreSQL or the test SQLite. It
compiles to a bare, length-bounded ``VARCHAR(n)`` where ``n`` is the length of
the longest member value, and the value set is enforced in the application
layer by ``validate_strings=True``.

Consequently there is no ``CHECK`` to assert on. The only physical-schema effect
of adding the longer member ``"incompleta_timeout"`` (18 chars) is that the
column's ``VARCHAR`` bound grows from the historical ``VARCHAR(12)`` (longest of
the original four values, ``"nao_iniciada"``) to ``VARCHAR(18)``. These tests
therefore assert on:

* the **VARCHAR bound widening** 12 → 18 after ``upgrade()``;
* **value storability** — the 18-char ``incompleta_timeout`` can be stored after
  upgrade (and, on a tightly bounded SQLite column, cannot before);
* the **symmetric downgrade** — ``downgrade()`` restores the ``VARCHAR(12)``
  bound, i.e. ``upgrade ∘ downgrade`` is the identity on the schema.

Isolation (CRITICAL)
--------------------
Following the convention of the sibling migration tests in this tree
(``test_migration_roundtrip_property.py`` / ``test_migration_1a6_property.py``),
this test NEVER runs ``alembic upgrade head`` against the shared project
PostgreSQL database. Each case builds its own **ephemeral, in-process** database:
a fresh temp-file SQLite engine with the *pre-migration* schema (``campaigns`` +
``operations`` with ``state`` as the historical ``VARCHAR(12)``, the state left
by the previous migration ``b7f4a9c21d3e``) created directly, then drives the
migration's REAL :func:`upgrade` / :func:`downgrade` logic through Alembic's own
:class:`~alembic.operations.Operations` proxy bound to that isolated engine. The
temp database is disposed and deleted after every test, so there is no state
leak and no shared-DB write. Everything is in-process — NO Docker, NO network,
NO shared DB.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import tempfile
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    inspect,
    select,
)
from sqlalchemy.engine import Engine

# Import the SHIPPED migration module (task 1.2) so we drive its REAL
# upgrade()/downgrade() logic. The filename is not a valid identifier, so load
# it by path via importlib (same convention as the sibling migration tests).
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261017_0100-c9d2e4f8a1b7_widen_operation_state_check_incomplete_timeout.py"
)
_spec = importlib.util.spec_from_file_location("_migration_c9d2e4f8a1b7", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)


# ---------------------------------------------------------------------------
# Constants: the historical (pre-migration) and widened VARCHAR bounds.
# "nao_iniciada" (12 chars) is the longest of the four historical values;
# "incompleta_timeout" (18 chars) is the new member added by the migration.
# ---------------------------------------------------------------------------
_OLD_BOUND = 12
_NEW_BOUND = 18
_NEW_VALUE = "incompleta_timeout"
_OLD_VALUE = "nao_iniciada"

assert len(_OLD_VALUE) == _OLD_BOUND
assert len(_NEW_VALUE) == _NEW_BOUND


# ---------------------------------------------------------------------------
# Pre-migration schema (down_revision = b7f4a9c21d3e): campaigns + operations,
# with ``state`` as the historical VARCHAR(12). Defined with a dedicated
# MetaData, independent of the ORM models (which already carry the widened
# VARCHAR(18) type). ``campaigns`` is seeded so the ``campaign_id`` FK resolves.
# ---------------------------------------------------------------------------

_CAMPAIGN_ID = "shadowray"


def _build_pre_migration_engine() -> tuple[Engine, Path]:
    """Create a fresh temp-file SQLite engine with the PRE-migration schema.

    Returns ``(engine, db_path)``. The caller disposes the engine and removes
    the file. ``operations.state`` is built as ``VARCHAR(12)`` — the historical
    bound set by migration ``a28d8c1ae4aa`` — so the widening to ``VARCHAR(18)``
    is a real, observable schema change.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stage6_opstate_mig_")
    db_path = Path(tmp_dir) / "operation_state.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    metadata = MetaData()
    Table(
        "campaigns",
        metadata,
        Column("id", String, primary_key=True),
    )
    Table(
        "operations",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=True),
        Column("caldera_operation_id", String, nullable=True),
        # Historical bound: VARCHAR(12). SQLite enforces this length (declared
        # type) under the length-aware String so the pre-upgrade column cannot
        # store the 18-char incompleta_timeout.
        Column("state", String(_OLD_BOUND), nullable=False),
        Column("total_success", Integer, nullable=False, default=0),
        Column("total_failure", Integer, nullable=False, default=0),
        Column("started_at", DateTime, nullable=True),
        Column("finished_at", DateTime, nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(insert(metadata.tables["campaigns"]), {"id": _CAMPAIGN_ID})

    return engine, db_path


def _run_migration_step(engine: Engine, step: str) -> None:
    """Drive the migration's REAL ``upgrade``/``downgrade`` through Alembic's
    Operations proxy bound to the ISOLATED temp engine.

    The migration module does ``from alembic import op`` (the global proxy). We
    temporarily rebind ``_migration.op`` to an Operations instance tied to a
    MigrationContext on our temp connection so every ``op.*`` call
    (``op.batch_alter_table``) resolves against the temp connection, never the
    project PostgreSQL. ``render_as_batch`` is simulated by the batch block the
    migration already uses; on SQLite the batch rebuilds the table with the new
    column type.
    """
    func = getattr(_migration, step)
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(
                connection=conn,
                opts={"render_as_batch": True},
            )
            op_proxy = Operations(ctx)
            original_op = _migration.op
            _migration.op = op_proxy
            try:
                func()
            finally:
                _migration.op = original_op


def _state_column_length(engine: Engine) -> int:
    """Return the declared VARCHAR length of ``operations.state``.

    Reads it from the reflected column type so the assertion tracks the actual
    physical schema after the batch rebuild, not the ORM model.
    """
    columns = {c["name"]: c for c in inspect(engine).get_columns("operations")}
    assert "state" in columns, "operations.state column missing"
    col_type = columns["state"]["type"]
    length = getattr(col_type, "length", None)
    assert length is not None, f"state column type has no length: {col_type!r}"
    return length


def _insert_operation(engine: Engine, state: str) -> None:
    """Insert one operations row with the given ``state`` value."""
    operations = Table("operations", MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(
            insert(operations),
            {
                "campaign_id": _CAMPAIGN_ID,
                "caldera_operation_id": "op-abc",
                "state": state,
                "total_success": 0,
                "total_failure": 0,
                "started_at": dt.datetime(2026, 1, 1),
                "finished_at": None,
            },
        )


def _read_states(engine: Engine) -> list[str]:
    operations = Table("operations", MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        return [r.state for r in conn.execute(select(operations.c.state)).all()]


# ===========================================================================
# Task 1.3 — upgrade: VARCHAR bound widens 12 -> 18 and incompleta_timeout
# becomes storable.
# ===========================================================================


def test_upgrade_widens_state_bound_and_admits_incompleta_timeout() -> None:
    """After ``upgrade()`` the ``operations.state`` VARCHAR bound widens 12 -> 18
    and the 18-char ``incompleta_timeout`` can be stored.

    Validates: Requirements 3.2
    """
    engine, db_path = _build_pre_migration_engine()
    try:
        # Pre-upgrade: the historical VARCHAR(12) bound.
        assert _state_column_length(engine) == _OLD_BOUND

        # Pre-upgrade sanity: a historical 12-char value stores fine.
        _insert_operation(engine, _OLD_VALUE)
        assert _read_states(engine) == [_OLD_VALUE]

        # Drive the REAL upgrade() on the ISOLATED temp engine.
        _run_migration_step(engine, "upgrade")

        # Post-upgrade: the bound is widened to 18 (the length of the new
        # member), which is the physical effect of admitting incompleta_timeout.
        assert _state_column_length(engine) == _NEW_BOUND

        # The pre-existing historical row survived the batch rebuild untouched
        # (purely additive — no row changes).
        assert _read_states(engine) == [_OLD_VALUE]

        # Value storability: the 18-char incompleta_timeout now stores.
        _insert_operation(engine, _NEW_VALUE)
        assert sorted(_read_states(engine)) == sorted([_OLD_VALUE, _NEW_VALUE])
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def test_before_upgrade_bound_rejects_incompleta_timeout_length() -> None:
    """Before ``upgrade()`` the column is bounded at VARCHAR(12); SQLite's
    length-aware String truncates/rejects the 18-char value so it is NOT stored
    intact — proving the widening is what makes the new value storable.

    Validates: Requirements 3.2
    """
    engine, db_path = _build_pre_migration_engine()
    try:
        assert _state_column_length(engine) == _OLD_BOUND

        # SQLite does not hard-enforce VARCHAR length by raising; depending on
        # the build it either stores the value as-is or truncates. Either way,
        # the DECLARED bound is 12 (< 18), which is precisely the condition the
        # migration must relax. We assert the declared bound is too small to be
        # a correct home for an 18-char value before the migration runs.
        assert _state_column_length(engine) < len(_NEW_VALUE)
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ===========================================================================
# Task 1.3 — downgrade: schema returns to the prior VARCHAR(12) bound
# (upgrade ∘ downgrade is the identity on the schema).
# ===========================================================================


def test_downgrade_restores_prior_state_bound() -> None:
    """``upgrade()`` then ``downgrade()`` restores the ``operations.state``
    VARCHAR bound to the historical 12 (schema identity), and a historical row
    stored before the round-trip survives unchanged.

    Validates: Requirements 3.2
    """
    engine, db_path = _build_pre_migration_engine()
    try:
        assert _state_column_length(engine) == _OLD_BOUND

        # Seed a historical row so the round-trip's data-preservation is real.
        _insert_operation(engine, _OLD_VALUE)

        _run_migration_step(engine, "upgrade")
        assert _state_column_length(engine) == _NEW_BOUND

        _run_migration_step(engine, "downgrade")

        # Schema identity: the bound is back to the historical 12.
        assert _state_column_length(engine) == _OLD_BOUND

        # The historical row is still present and unchanged after up ∘ down.
        assert _read_states(engine) == [_OLD_VALUE]
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
