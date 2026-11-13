"""Integration test for the STIX-package-persistence migration ``d5e6f7a8b9c0``
(task 1.3 of the ``stix-package-persistence-and-overview`` spec).

# Feature: stix-package-persistence-and-overview — migration ``d5e6f7a8b9c0``
# ("create the six STIX package persistence tables", task 1.2) lands the schema
# half of the persistence spec: it creates the six STIX tables
# (``stix_packages``, ``stix_type_counts``, ``stix_campaigns``,
# ``stix_actors``, ``stix_campaign_techniques`` and ``stix_actor_campaigns``)
# in dependency order with a UNIQUE on ``stix_packages.version`` and a btree
# index on every foreign key. This test drives the migration's REAL
# ``upgrade()`` / ``downgrade()`` in-process and asserts the behaviours task 1.3
# requires:
#   * the six tables are created on upgrade and dropped on downgrade (Req. 10.1);
#   * the head chains onto ``c3d4e5f6a7b8`` (Req. 10.2); and
#   * ``upgrade`` followed by ``downgrade`` is an identity over the schema
#     (Req. 10.4).

Validates: Requirements 10.1, 10.2, 10.4

This is a *feature* spec (``stix-package-persistence-and-overview``), so the
behaviours under test are expected to HOLD on the migration as written.

Why a single SQLite in-process backend
---------------------------------------
Unlike the sibling ``test_migration_ai_pgvector_integration.py`` (whose
migration enabled pgvector and emitted PostgreSQL-only effects), this migration
is *purely portable*: it uses only generic types (``String`` / ``Text`` /
``Integer`` / ``Boolean`` / ``DateTime`` / ``JSON``) and portable
``create_table`` / ``create_index`` operations, so it compiles identically on
PostgreSQL (the product database) and the ephemeral SQLite the test suite runs
on. There are no dialect-gated branches to exercise offline, so the real
``upgrade()`` / ``downgrade()`` driven against an isolated temp-file SQLite
engine covers the whole migration.

Everything is in-process — NO Docker, NO network, NO shared DB. The temp SQLite
database is disposed and deleted after use, so there is no state leak. This
follows the convention of the sibling migration tests in this directory, which
never run ``alembic upgrade head`` against the shared project PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

# Import the SHIPPED migration module (task 1.2) so we drive its REAL
# upgrade()/downgrade() logic rather than a reimplementation. The filename is
# not a valid identifier, so load it by path via importlib — the same
# convention as every sibling migration test in this directory.
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261022_0100-d5e6f7a8b9c0_create_stix_package_persistence_tables.py"
)
_spec = importlib.util.spec_from_file_location("_migration_d5e6f7a8b9c0", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)


# The six STIX tables this migration owns, in CREATE order (parents before
# children). ``downgrade`` drops them in the exact reverse order (children
# before parents).
_STIX_TABLES_CREATE_ORDER: tuple[str, ...] = (
    "stix_packages",
    "stix_type_counts",
    "stix_campaigns",
    "stix_actors",
    "stix_campaign_techniques",
    "stix_actor_campaigns",
)
_STIX_TABLES: frozenset[str] = frozenset(_STIX_TABLES_CREATE_ORDER)

# The foreign-key indexes the migration creates, read from the migration so the
# assertions track the shipped names rather than a copy.
_FK_INDEXES: tuple[tuple[str, str, str], ...] = _migration._FK_INDEXES

# The expected head chaining (Req. 10.2).
_EXPECTED_REVISION = "d5e6f7a8b9c0"
_EXPECTED_DOWN_REVISION = "c3d4e5f6a7b8"


# ---------------------------------------------------------------------------
# SQLite in-process helpers — drive the migration's REAL upgrade/downgrade
# through Alembic's Operations proxy bound to an ISOLATED temp-file engine
# (mirrors test_migration_ai_pgvector_integration.py::_run_migration_step).
# ---------------------------------------------------------------------------


def _build_empty_sqlite_engine() -> tuple[Engine, Path]:
    """Create a fresh temp-file SQLite engine with an empty schema.

    The migration is purely additive (it creates its own tables; its FKs to
    pre-existing tables are not enforced at CREATE time on SQLite), so the six
    STIX tables round-trip cleanly against an empty database. Returns
    ``(engine, db_path)``; the caller disposes the engine and removes the file.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stix_persist_mig_")
    db_path = Path(tmp_dir) / "stix_persistence.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    return engine, db_path


def _run_migration_step(engine: Engine, step: str) -> None:
    """Drive the migration's REAL ``upgrade``/``downgrade`` through Alembic's
    Operations proxy bound to the ISOLATED temp engine.

    The migration module does ``from alembic import op`` (the global proxy). We
    temporarily rebind ``_migration.op`` to an ``Operations`` instance tied to a
    :class:`~alembic.migration.MigrationContext` on our temp connection so every
    ``op.*`` call (``op.create_table`` / ``op.create_index`` / ``op.drop_index``
    / ``op.drop_table``) resolves against the temp SQLite connection, never any
    shared DB.
    """
    func = getattr(_migration, step)
    with engine.connect() as conn, conn.begin():
        ctx = MigrationContext.configure(connection=conn)
        op_proxy = Operations(ctx)
        original_op = _migration.op
        _migration.op = op_proxy
        try:
            func()
        finally:
            _migration.op = original_op


# ===========================================================================
# (A) Head chaining: the migration chains onto ``c3d4e5f6a7b8`` (Req. 10.2).
# ===========================================================================


def test_migration_chains_onto_current_head() -> None:
    """The shipped migration is revision ``d5e6f7a8b9c0`` and declares
    ``down_revision = "c3d4e5f6a7b8"`` — it chains onto the current head.

    # Feature: stix-package-persistence-and-overview — migration d5e6f7a8b9c0.
    Validates: Requirements 10.2
    """
    assert _migration.revision == _EXPECTED_REVISION, (
        f"migration revision is {_migration.revision!r}, expected {_EXPECTED_REVISION!r}"
    )
    assert _migration.down_revision == _EXPECTED_DOWN_REVISION, (
        f"migration down_revision is {_migration.down_revision!r}, "
        f"expected {_EXPECTED_DOWN_REVISION!r} (must chain onto the current head)"
    )


# ===========================================================================
# (B) SQLite in-process: the six STIX tables are created on upgrade and dropped
#     on downgrade — a clean up∘down identity over the objects this migration
#     owns (Req. 10.1, 10.4).
# ===========================================================================


def test_sqlite_upgrade_creates_then_downgrade_drops_the_six_tables() -> None:
    """``upgrade()`` creates exactly the six STIX tables (with the FK indexes and
    the UNIQUE on ``stix_packages.version``); ``downgrade()`` drops them all,
    restoring the empty schema (clean up∘down identity).

    # Feature: stix-package-persistence-and-overview — migration d5e6f7a8b9c0.
    Validates: Requirements 10.1, 10.4
    """
    engine, db_path = _build_empty_sqlite_engine()
    try:
        # Precondition: none of the STIX tables exist yet.
        before = set(inspect(engine).get_table_names())
        assert _STIX_TABLES.isdisjoint(before), (
            f"STIX tables already present before upgrade: {_STIX_TABLES & before}"
        )

        # Drive the REAL upgrade() on the ISOLATED temp engine.
        _run_migration_step(engine, "upgrade")

        after_up = set(inspect(engine).get_table_names())
        # All six STIX tables now exist (Req. 10.1 — the tables are created).
        assert after_up >= _STIX_TABLES, (
            f"missing STIX tables after upgrade: {_STIX_TABLES - after_up}"
        )
        # Upgrade created exactly these six tables and nothing else (the empty
        # DB started with no tables, so the delta is precisely the six).
        assert after_up - before == _STIX_TABLES, (
            f"upgrade created unexpected tables: {after_up - before - _STIX_TABLES}"
        )

        inspector = inspect(engine)

        # The idempotency key: ``stix_packages.version`` carries a UNIQUE
        # constraint (Req. 10.1 / design Model 1).
        unique_constraints = inspector.get_unique_constraints("stix_packages")
        version_unique = any(uc["column_names"] == ["version"] for uc in unique_constraints)
        assert version_unique, (
            "stix_packages.version must carry a UNIQUE constraint; "
            f"unique constraints found: {unique_constraints}"
        )

        # Every foreign key the migration indexes has its btree index present on
        # the owning table.
        for index_name, table_name, _column_name in _FK_INDEXES:
            table_indexes = {ix["name"] for ix in inspector.get_indexes(table_name)}
            assert index_name in table_indexes, (
                f"foreign-key index {index_name!r} missing on {table_name!r}; "
                f"indexes present: {table_indexes}"
            )

        # Drive the REAL downgrade() on the same isolated engine.
        _run_migration_step(engine, "downgrade")

        after_down = set(inspect(engine).get_table_names())
        # Clean up∘down identity (Req. 10.4): all six STIX tables are gone and the
        # schema is byte-for-byte the pre-upgrade empty schema.
        assert _STIX_TABLES.isdisjoint(after_down), (
            f"STIX tables survived downgrade: {_STIX_TABLES & after_down}"
        )
        assert after_down == before, (
            "downgrade did not restore the exact pre-upgrade schema "
            f"(residual delta: {after_down ^ before})"
        )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def test_sqlite_upgrade_downgrade_upgrade_is_repeatable() -> None:
    """A second ``upgrade()`` after a ``downgrade()`` succeeds and recreates the
    six tables — the migration leaves no residue that blocks re-application
    (reinforces the clean up∘down identity of Req. 10.4).

    # Feature: stix-package-persistence-and-overview — migration d5e6f7a8b9c0.
    Validates: Requirements 10.1, 10.4
    """
    engine, db_path = _build_empty_sqlite_engine()
    try:
        _run_migration_step(engine, "upgrade")
        _run_migration_step(engine, "downgrade")
        # Re-apply: a clean downgrade must leave the DB able to upgrade again.
        _run_migration_step(engine, "upgrade")
        after = set(inspect(engine).get_table_names())
        assert after >= _STIX_TABLES, (
            f"missing STIX tables after re-upgrade: {_STIX_TABLES - after}"
        )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
