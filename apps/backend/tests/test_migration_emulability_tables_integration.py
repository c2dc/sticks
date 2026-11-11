"""Integration test for the emulability-tables migration (agentic-phase-model-refactor task 12.1).

# Feature: agentic-phase-model-refactor — task 12.1 lands the schema for the
# container-emulability knowledge: the migration ``c0d1e2f3a4b5`` creates
# ``technique_emulability`` (per-ATT&CK-technique classification, Req. 6.1) and
# ``container_emulability`` (per-campaign derived verdict, Req. 6.3), chaining
# onto the sibling task-2.1 enum-rename migration ``c0d1e2f3a4b5`` (which itself
# chains onto the ``provisioning_map`` head ``b9c0d1e2f3a4``). This test drives
# the migration's REAL ``upgrade()`` / ``downgrade()`` in-process and asserts:
#   * the migration chains onto the enum-rename head ``c0d1e2f3a4b5`` (Req. 9.3);
#   * upgrading creates exactly ``technique_emulability`` and
#     ``container_emulability`` — the former with the emulability CHECK, the
#     latter with the ``campaign_id`` UNIQUE idempotency key (Req. 6.1, 6.3); and
#   * downgrading drops both, restoring the exact pre-upgrade schema — a clean
#     up∘down identity (Req. 9.3).

Validates: Requirements 6.1, 6.3, 9.3

This is a *feature* spec, so the behaviours under test are expected to HOLD on
the migration as written.

Why a single SQLite in-process backend
---------------------------------------
The migration is purely portable: it uses only generic types (``String`` /
``Text`` / ``Integer`` / ``Boolean`` / ``JSON``) plus a portable
``CHECK (... IN (...))`` on the emulability enum column, so it compiles
identically on PostgreSQL (the product database) and the ephemeral SQLite the
test suite runs on. Neither table carries a foreign key, so the two tables
round-trip cleanly against an empty database.

Everything is in-process — NO Docker, NO network, NO shared DB. The temp SQLite
database is disposed and deleted after use, so there is no state leak. This
follows the convention of the sibling migration tests in this directory, which
never run ``alembic upgrade head`` against the shared project PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# Load the SHIPPED migration module (task 12.1) by path so we drive its REAL
# upgrade()/downgrade() logic rather than a reimplementation. The filename is
# not a valid identifier, so load it via importlib — the same convention as
# every sibling migration test in this directory.
# ---------------------------------------------------------------------------

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration(module_name: str, filename: str) -> ModuleType:
    """Import a shipped migration module by its on-disk path."""
    spec = importlib.util.spec_from_file_location(
        module_name, _VERSIONS_DIR / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION_EMULABILITY = _load_migration(
    "_migration_d1e2f3a4b5c6",
    "20261027_0200-d1e2f3a4b5c6_create_technique_and_container_emulability.py",
)

# The enum-rename head (sibling task 2.1) this migration chains onto (Req. 9.3).
_ENUM_RENAME_HEAD = "c0d1e2f3a4b5"

# The two tables this migration owns.
_EMULABILITY_TABLES: frozenset[str] = frozenset(
    {"technique_emulability", "container_emulability"}
)


# ---------------------------------------------------------------------------
# SQLite in-process helpers — drive the migration's REAL upgrade/downgrade
# through Alembic's Operations proxy bound to an ISOLATED temp-file engine
# (mirrors test_migrations_command_base_tables_integration.py).
# ---------------------------------------------------------------------------


def _build_empty_sqlite_engine() -> tuple[Engine, Path]:
    """Create a fresh temp-file SQLite engine with an empty schema.

    The migration is purely additive and neither table carries a foreign key,
    so the two tables round-trip cleanly against an empty database. Returns
    ``(engine, db_path)``; the caller disposes the engine and removes the file.
    """
    tmp_dir = tempfile.mkdtemp(prefix="emulability_mig_")
    db_path = Path(tmp_dir) / "emulability.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    return engine, db_path


def _run_migration_step(engine: Engine, step: str) -> None:
    """Drive the migration's REAL ``upgrade``/``downgrade`` through Alembic's
    Operations proxy bound to the ISOLATED temp engine.

    The migration module does ``from alembic import op`` (the global proxy). We
    temporarily rebind ``module.op`` to an ``Operations`` instance tied to a
    :class:`~alembic.migration.MigrationContext` on our temp connection so every
    ``op.*`` call resolves against the temp SQLite connection, never any shared
    DB. SQLite CHECK constraints are enforced at write time, so we enable FK/
    integrity pragmas via the standard engine default (SQLite enforces CHECK by
    default).
    """
    func = getattr(_MIGRATION_EMULABILITY, step)
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn)
            op_proxy = Operations(ctx)
            original_op = _MIGRATION_EMULABILITY.op
            _MIGRATION_EMULABILITY.op = op_proxy
            try:
                func()
            finally:
                _MIGRATION_EMULABILITY.op = original_op


# ===========================================================================
# (A) Chain wiring: the migration chains onto the enum-rename head
#     ``c0d1e2f3a4b5`` (Req. 9.3).
# ===========================================================================


def test_emulability_migration_chains_onto_enum_rename_head() -> None:
    """``d1e2f3a4b5c6`` chains onto the enum-rename head ``c0d1e2f3a4b5``: its
    ``down_revision`` points at that head.

    # Feature: agentic-phase-model-refactor — task 12.1.
    Validates: Requirements 9.3
    """
    assert _MIGRATION_EMULABILITY.revision == "d1e2f3a4b5c6"
    assert _MIGRATION_EMULABILITY.down_revision == _ENUM_RENAME_HEAD, (
        "the emulability migration must chain onto the enum-rename head "
        f"{_ENUM_RENAME_HEAD!r}, found "
        f"{_MIGRATION_EMULABILITY.down_revision!r}"
    )


# ===========================================================================
# (B) SQLite in-process: upgrading creates exactly the two tables (with the
#     emulability CHECK and the container_emulability UNIQUE); downgrading
#     drops both, restoring the pre-upgrade schema — a clean up∘down identity
#     (Req. 6.1, 6.3, 9.3).
# ===========================================================================


def test_sqlite_upgrade_creates_then_downgrade_drops_emulability_tables() -> None:
    """Upgrading creates exactly ``technique_emulability`` (with its emulability
    CHECK) and ``container_emulability`` (with its ``campaign_id`` UNIQUE
    idempotency key); downgrading drops both, restoring the empty schema (clean
    up∘down identity).

    # Feature: agentic-phase-model-refactor — task 12.1.
    Validates: Requirements 6.1, 6.3, 9.3
    """
    engine, db_path = _build_empty_sqlite_engine()
    try:
        # Precondition: neither table exists yet.
        before = set(inspect(engine).get_table_names())
        assert _EMULABILITY_TABLES.isdisjoint(before), (
            f"emulability tables already present before upgrade: "
            f"{_EMULABILITY_TABLES & before}"
        )

        # Drive the REAL upgrade.
        _run_migration_step(engine, "upgrade")

        after_up = set(inspect(engine).get_table_names())
        # The migration created exactly these two tables and nothing else.
        assert after_up - before == _EMULABILITY_TABLES, (
            f"upgrade created unexpected tables: "
            f"{after_up - before - _EMULABILITY_TABLES}"
        )

        inspector = inspect(engine)

        # technique_emulability has technique_id as its (single-column) PK.
        technique_pk = inspector.get_pk_constraint("technique_emulability")
        assert technique_pk["constrained_columns"] == ["technique_id"], (
            "technique_emulability PK must be (technique_id); found "
            f"{technique_pk['constrained_columns']}"
        )

        # container_emulability carries the UNIQUE(campaign_id) idempotency key.
        unique_constraints = inspector.get_unique_constraints(
            "container_emulability"
        )
        has_campaign_unique = any(
            uc["column_names"] == ["campaign_id"] for uc in unique_constraints
        )
        assert has_campaign_unique, (
            "container_emulability must carry a UNIQUE constraint on "
            f"(campaign_id); unique constraints found: {unique_constraints}"
        )

        # The emulability CHECK admits the three valid values and rejects any
        # other — SQLite enforces CHECK constraints at write time.
        with engine.begin() as conn:
            for value in ("container", "dispensable", "not_container"):
                conn.execute(
                    text(
                        "INSERT INTO technique_emulability "
                        "(technique_id, emulability) VALUES (:tid, :val)"
                    ),
                    {"tid": f"T{value}", "val": value},
                )
        # An out-of-set value is rejected by the CHECK.
        rejected = False
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO technique_emulability "
                        "(technique_id, emulability) VALUES (:tid, :val)"
                    ),
                    {"tid": "T9999", "val": "virtual_machine"},
                )
        except sa.exc.IntegrityError:
            rejected = True
        assert rejected, (
            "the emulability CHECK must reject values outside "
            "{container, dispensable, not_container}"
        )

        # Drive the REAL downgrade.
        _run_migration_step(engine, "downgrade")

        after_down = set(inspect(engine).get_table_names())
        # Clean up∘down identity (Req. 9.3): both tables are gone and the schema
        # is the pre-upgrade empty schema.
        assert _EMULABILITY_TABLES.isdisjoint(after_down), (
            f"emulability tables survived downgrade: "
            f"{_EMULABILITY_TABLES & after_down}"
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
