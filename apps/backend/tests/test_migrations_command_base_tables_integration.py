"""Integration test for the batch of three ``Command_Base``/provisioning table
migrations of Phase 3.2 (stix-db-single-source-ai-pipeline task 9.4).

# Feature: stix-db-single-source-ai-pipeline — the three chained migrations
# (tasks 9.1/9.2/9.3) land the schema half of Phase 3.2: in order they create
# ``technique_commands`` (``f7a8b9c0d1e2``), ``image_catalog``
# (``a8b9c0d1e2f3``) and ``provisioning_map`` (``b9c0d1e2f3a4``), each chaining
# onto the previous one on top of the embedding-dim head ``e6f7a8b9c0d1``. This
# test drives the batch's REAL ``upgrade()`` / ``downgrade()`` in-process and
# asserts the behaviours task 9.4 requires:
#   * the chain is well formed: ``f7a8b9c0d1e2`` -> ``a8b9c0d1e2f3`` ->
#     ``b9c0d1e2f3a4`` (each ``down_revision`` points at the previous revision),
#     anchored on the embedding-dim head ``e6f7a8b9c0d1`` (Req. 9.4);
#   * upgrading the batch (``f7a8b9c0d1e2`` then ``a8b9c0d1e2f3`` then
#     ``b9c0d1e2f3a4``) creates exactly the three tables ``technique_commands``,
#     ``image_catalog`` and ``provisioning_map`` with their UNIQUE constraints
#     (and the ``provisioning_map -> campaigns`` FK) (Req. 7.2, 8.2, 14.4); and
#   * downgrading the batch in reverse drops all three, restoring the exact
#     pre-upgrade schema — a clean up∘down identity (Req. 9.4).

Validates: Requirements 7.2, 8.2, 14.4, 9.4

This is a *feature* spec (``stix-db-single-source-ai-pipeline``), so the
behaviours under test are expected to HOLD on the migrations as written.

Why a single SQLite in-process backend
---------------------------------------
All three migrations are *purely portable*: they use only generic types
(``String`` / ``Text`` / ``Integer`` / ``JSON`` / ``DateTime``) and portable
``create_table`` / ``drop_table`` operations, so they compile identically on
PostgreSQL (the product database) and the ephemeral SQLite the test suite runs
on. The only non-portable migrations in the surrounding chain — the embedding
realignment ``e6f7a8b9c0d1`` and the pgvector enablement — are PostgreSQL-gated
and are NOT part of this batch, so they are never driven here. The
``provisioning_map -> campaigns`` foreign key is declared at CREATE time but
SQLite does not enforce FK targets at ``create_table`` time, so the three
tables round-trip cleanly against an empty database (same precedent as
``test_migration_stix_package_persistence_integration.py``).

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

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# Load the three SHIPPED migration modules (tasks 9.1/9.2/9.3) by path so we
# drive their REAL upgrade()/downgrade() logic rather than a reimplementation.
# The filenames are not valid identifiers, so load them via importlib — the
# same convention as every sibling migration test in this directory.
# ---------------------------------------------------------------------------

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration(module_name: str, filename: str) -> ModuleType:
    """Import a shipped migration module by its on-disk path."""
    spec = importlib.util.spec_from_file_location(module_name, _VERSIONS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION_TECHNIQUE_COMMANDS = _load_migration(
    "_migration_f7a8b9c0d1e2",
    "20261024_0100-f7a8b9c0d1e2_create_technique_commands.py",
)
_MIGRATION_IMAGE_CATALOG = _load_migration(
    "_migration_a8b9c0d1e2f3",
    "20261025_0100-a8b9c0d1e2f3_create_image_catalog.py",
)
_MIGRATION_PROVISIONING_MAP = _load_migration(
    "_migration_b9c0d1e2f3a4",
    "20261026_0100-b9c0d1e2f3a4_create_provisioning_map.py",
)

# The batch in UPGRADE order (parents before children), each paired with the
# table it owns. ``downgrade`` runs in the exact reverse order.
_BATCH_UPGRADE_ORDER: tuple[tuple[ModuleType, str], ...] = (
    (_MIGRATION_TECHNIQUE_COMMANDS, "technique_commands"),
    (_MIGRATION_IMAGE_CATALOG, "image_catalog"),
    (_MIGRATION_PROVISIONING_MAP, "provisioning_map"),
)

# The three tables this batch owns.
_BATCH_TABLES: frozenset[str] = frozenset(
    table_name for _module, table_name in _BATCH_UPGRADE_ORDER
)

# The chain anchor: the embedding-dim head the batch builds on (Req. 9.4).
_EMBEDDING_DIM_HEAD = "e6f7a8b9c0d1"

# The UNIQUE constraint each table's upgrade must create, as (table, columns).
_EXPECTED_UNIQUE_CONSTRAINTS: tuple[tuple[str, list[str]], ...] = (
    ("technique_commands", ["technique_id", "command_hash", "source"]),
    ("image_catalog", ["image", "target_os"]),
    ("provisioning_map", ["campaign_id"]),
)


# ---------------------------------------------------------------------------
# SQLite in-process helpers — drive each migration's REAL upgrade/downgrade
# through Alembic's Operations proxy bound to an ISOLATED temp-file engine
# (mirrors test_migration_stix_package_persistence_integration.py).
# ---------------------------------------------------------------------------


def _build_empty_sqlite_engine() -> tuple[Engine, Path]:
    """Create a fresh temp-file SQLite engine with an empty schema.

    Each migration is purely additive (it creates its own table; the
    ``provisioning_map`` FK to ``campaigns`` is not enforced at CREATE time on
    SQLite), so the three tables round-trip cleanly against an empty database.
    Returns ``(engine, db_path)``; the caller disposes the engine and removes
    the file.
    """
    tmp_dir = tempfile.mkdtemp(prefix="command_base_mig_")
    db_path = Path(tmp_dir) / "command_base.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    return engine, db_path


def _run_migration_step(engine: Engine, module: ModuleType, step: str) -> None:
    """Drive a migration module's REAL ``upgrade``/``downgrade`` through
    Alembic's Operations proxy bound to the ISOLATED temp engine.

    Each migration module does ``from alembic import op`` (the global proxy).
    We temporarily rebind ``module.op`` to an ``Operations`` instance tied to a
    :class:`~alembic.migration.MigrationContext` on our temp connection so every
    ``op.*`` call (``op.create_table`` / ``op.drop_table``) resolves against the
    temp SQLite connection, never any shared DB.
    """
    func = getattr(module, step)
    with engine.connect() as conn, conn.begin():
        ctx = MigrationContext.configure(connection=conn)
        op_proxy = Operations(ctx)
        original_op = module.op
        module.op = op_proxy
        try:
            func()
        finally:
            module.op = original_op


def _upgrade_batch(engine: Engine) -> None:
    """Run the batch's three ``upgrade()``s in chain (CREATE) order."""
    for module, _table_name in _BATCH_UPGRADE_ORDER:
        _run_migration_step(engine, module, "upgrade")


def _downgrade_batch(engine: Engine) -> None:
    """Run the batch's three ``downgrade()``s in reverse (DROP) order."""
    for module, _table_name in reversed(_BATCH_UPGRADE_ORDER):
        _run_migration_step(engine, module, "downgrade")


# ===========================================================================
# (A) Chain wiring: the three migrations chain in order on top of the
#     embedding-dim head ``e6f7a8b9c0d1`` (Req. 9.4).
# ===========================================================================


def test_batch_migrations_chain_in_order_on_embedding_dim_head() -> None:
    """The three migrations form the chain ``e6f7a8b9c0d1`` -> ``f7a8b9c0d1e2``
    -> ``a8b9c0d1e2f3`` -> ``b9c0d1e2f3a4``: each ``down_revision`` points at the
    previous revision and the first anchors on the embedding-dim head.

    # Feature: stix-db-single-source-ai-pipeline — batch of tasks 9.1/9.2/9.3.
    Validates: Requirements 9.4
    """
    assert _MIGRATION_TECHNIQUE_COMMANDS.revision == "f7a8b9c0d1e2"
    assert _MIGRATION_TECHNIQUE_COMMANDS.down_revision == _EMBEDDING_DIM_HEAD, (
        "create_technique_commands must chain onto the embedding-dim head "
        f"{_EMBEDDING_DIM_HEAD!r}, found "
        f"{_MIGRATION_TECHNIQUE_COMMANDS.down_revision!r}"
    )

    assert _MIGRATION_IMAGE_CATALOG.revision == "a8b9c0d1e2f3"
    assert _MIGRATION_IMAGE_CATALOG.down_revision == "f7a8b9c0d1e2", (
        "create_image_catalog must chain onto create_technique_commands, found "
        f"{_MIGRATION_IMAGE_CATALOG.down_revision!r}"
    )

    assert _MIGRATION_PROVISIONING_MAP.revision == "b9c0d1e2f3a4"
    assert _MIGRATION_PROVISIONING_MAP.down_revision == "a8b9c0d1e2f3", (
        "create_provisioning_map must chain onto create_image_catalog, found "
        f"{_MIGRATION_PROVISIONING_MAP.down_revision!r}"
    )


# ===========================================================================
# (B) SQLite in-process: upgrading the batch creates exactly the three tables
#     (with their UNIQUE constraints and the provisioning_map FK); downgrading
#     the batch in reverse drops them all, restoring the pre-upgrade schema — a
#     clean up∘down identity (Req. 7.2, 8.2, 14.4, 9.4).
# ===========================================================================


def test_sqlite_batch_upgrade_creates_then_downgrade_drops_three_tables() -> None:
    """Upgrading the batch creates exactly ``technique_commands``,
    ``image_catalog`` and ``provisioning_map`` (each with its UNIQUE constraint,
    and ``provisioning_map`` with its FK to ``campaigns``); downgrading the batch
    in reverse drops all three, restoring the empty schema (clean up∘down
    identity).

    # Feature: stix-db-single-source-ai-pipeline — batch of tasks 9.1/9.2/9.3.
    Validates: Requirements 7.2, 8.2, 14.4, 9.4
    """
    engine, db_path = _build_empty_sqlite_engine()
    try:
        # Precondition: none of the three tables exist yet.
        before = set(inspect(engine).get_table_names())
        assert _BATCH_TABLES.isdisjoint(before), (
            f"batch tables already present before upgrade: {_BATCH_TABLES & before}"
        )

        # Drive the REAL upgrades of the whole batch, in chain order.
        _upgrade_batch(engine)

        after_up = set(inspect(engine).get_table_names())
        # All three tables now exist (Req. 7.2 / 14.4 / 8.2 — the tables are
        # created).
        assert after_up >= _BATCH_TABLES, (
            f"missing batch tables after upgrade: {_BATCH_TABLES - after_up}"
        )
        # The batch created exactly these three tables and nothing else (the
        # empty DB started with no tables, so the delta is precisely the three).
        assert after_up - before == _BATCH_TABLES, (
            f"batch upgrade created unexpected tables: {after_up - before - _BATCH_TABLES}"
        )

        inspector = inspect(engine)

        # Each table carries the UNIQUE constraint that is its idempotency key.
        for table_name, expected_columns in _EXPECTED_UNIQUE_CONSTRAINTS:
            unique_constraints = inspector.get_unique_constraints(table_name)
            has_unique = any(uc["column_names"] == expected_columns for uc in unique_constraints)
            assert has_unique, (
                f"{table_name} must carry a UNIQUE constraint on "
                f"{expected_columns}; unique constraints found: "
                f"{unique_constraints}"
            )

        # provisioning_map carries the foreign key campaign_id -> campaigns.id.
        provisioning_fks = inspector.get_foreign_keys("provisioning_map")
        has_campaign_fk = any(
            fk["referred_table"] == "campaigns"
            and fk["constrained_columns"] == ["campaign_id"]
            and fk["referred_columns"] == ["id"]
            for fk in provisioning_fks
        )
        assert has_campaign_fk, (
            "provisioning_map must carry an FK campaign_id -> campaigns.id; "
            f"foreign keys found: {provisioning_fks}"
        )

        # Drive the REAL downgrades of the whole batch, in reverse order.
        _downgrade_batch(engine)

        after_down = set(inspect(engine).get_table_names())
        # Clean up∘down identity (Req. 9.4): all three tables are gone and the
        # schema is the pre-upgrade empty schema.
        assert _BATCH_TABLES.isdisjoint(after_down), (
            f"batch tables survived downgrade: {_BATCH_TABLES & after_down}"
        )
        assert after_down == before, (
            "batch downgrade did not restore the exact pre-upgrade schema "
            f"(residual delta: {after_down ^ before})"
        )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def test_sqlite_batch_upgrade_downgrade_upgrade_is_repeatable() -> None:
    """A second batch ``upgrade`` after a batch ``downgrade`` succeeds and
    recreates the three tables — the batch leaves no residue that blocks
    re-application (reinforces the clean up∘down identity of Req. 9.4).

    # Feature: stix-db-single-source-ai-pipeline — batch of tasks 9.1/9.2/9.3.
    Validates: Requirements 9.4
    """
    engine, db_path = _build_empty_sqlite_engine()
    try:
        _upgrade_batch(engine)
        _downgrade_batch(engine)
        # Re-apply: a clean downgrade must leave the DB able to upgrade again.
        _upgrade_batch(engine)
        after = set(inspect(engine).get_table_names())
        assert after >= _BATCH_TABLES, (
            f"missing batch tables after re-upgrade: {_BATCH_TABLES - after}"
        )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
