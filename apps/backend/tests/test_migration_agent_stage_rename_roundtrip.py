"""Deterministic round-trip test for the AgentStage rename migration
``c0d1e2f3a4b5`` (agentic-phase-model-refactor task 2.2).

# Feature: agentic-phase-model-refactor — task 2.1 ships the reversible enum
# migration ``c0d1e2f3a4b5`` that rewrites every AgentStage-backed column from
# the Legacy_Stage_Numbering values (``stage_3_dag`` / ``stage_4_operationalization``
# / ``stage_5_environment``) to the semantic, number-free phase names
# (``dag`` / ``operationalization`` / ``environment``) and regenerates each
# column's named CHECK to the full five-member semantic set (adding the new own
# phases ``execution`` / ``evaluation``). ``downgrade`` applies the exact inverse.
#
# Property 2: Migração reversível e preservadora — ``upgrade`` maps each legacy
# value to its semantic form and ``downgrade`` inverts it losslessly.

Validates: Requirements 1.3, 9.3

This is a *feature* spec, so the behaviour under test is expected to HOLD on the
migration as written. Per the spec Testing Strategy this is ONE deterministic
round-trip test (NOT a property/Hypothesis test): seed a ``stage_3_dag`` row,
drive the REAL ``upgrade()`` and assert it became ``dag``; drive the REAL
``downgrade()`` and assert it reverts to ``stage_3_dag``.

Isolation
---------
Everything is in-process — NO Docker, NO network, NO shared DB. Each run builds
its own ephemeral temp-file SQLite engine carrying the PRE-migration (legacy)
schema: the three AgentStage-backed columns with their legacy named CHECKs. The
migration's REAL ``upgrade()`` / ``downgrade()`` are driven through Alembic's
own ``Operations`` proxy bound to that isolated engine (``render_as_batch=True``
so SQLite rebuilds the tables, matching the project's ``env.py`` contract). The
temp DB is disposed and deleted after the test, so there is no state leak. This
follows the convention of the sibling migration tests in this directory, which
never run ``alembic upgrade head`` against the shared project PostgreSQL.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import tempfile
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
)
from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# Load the SHIPPED migration module (task 2.1) by path so we drive its REAL
# upgrade()/downgrade() logic AND read its authoritative column driver / value
# map rather than reimplementing them. The filename is not a valid identifier,
# so load it via importlib — the same convention as every sibling migration
# test in this directory.
# ---------------------------------------------------------------------------

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration(module_name: str, filename: str) -> ModuleType:
    """Import a shipped migration module by its on-disk path."""
    spec = importlib.util.spec_from_file_location(module_name, _VERSIONS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION = _load_migration(
    "_migration_c0d1e2f3a4b5",
    "20261027_0100-c0d1e2f3a4b5_rename_agent_stage_to_semantic_values.py",
)

# The authoritative column driver and value map, straight from the migration,
# so the test stays in lockstep with the migration under test.
#   _COLUMNS : (table, column, nullable, check_name)
#   _STAGE_MAP : (legacy_value, semantic_value)
_COLUMNS = _MIGRATION._COLUMNS
_STAGE_MAP = _MIGRATION._STAGE_MAP

# The three legacy values and the three semantic values they map to.
_LEGACY_VALUES: tuple[str, ...] = tuple(legacy for legacy, _sem in _STAGE_MAP)
_SEMANTIC_OF: dict[str, str] = {legacy: sem for legacy, sem in _STAGE_MAP}

# A fixed naive-UTC timestamp for every NOT-NULL ``*_at`` column.
_TS = dt.datetime(2026, 10, 27, 1, 0, 0)


# ---------------------------------------------------------------------------
# PRE-migration (legacy) schema — exactly the three AgentStage-backed columns
# the migration regenerates, with their legacy named CHECKs (the set migration
# ``b2c3d4e5f6a7`` installed). The FK chain agent_runs -> agent_iterations and
# the experience_memory.campaign_id FK are modelled so every seeded row resolves
# and the CHECK drop/recreate finds the named constraints by name.
# ---------------------------------------------------------------------------

# Per-table facts: primary key and the minimum NOT-NULL non-enum scaffolding so
# inserts succeed and FKs resolve.
_PK: dict[str, str] = {
    "agent_iterations": "id",
    "agent_stage_outputs": "id",
    "experience_memory": "id",
}

_CAMPAIGN_ID = "shadowray"


def _legacy_check(column: str, name: str) -> CheckConstraint:
    """A named legacy ``CHECK (column IN (...))`` over the three legacy values,
    matching the pre-migration form the migration's ``drop_constraint`` targets."""
    in_list = ", ".join(f"'{v}'" for v in _LEGACY_VALUES)
    return CheckConstraint(f"{column} IN ({in_list})", name=name)


def _nullable_for(table: str, column: str) -> bool:
    for tbl, col, nullable, _cn in _COLUMNS:
        if tbl == table and col == column:
            return nullable
    raise KeyError(f"{table}.{column} not in migration _COLUMNS")


def _check_name_for(table: str, column: str) -> str:
    for tbl, col, _nullable, check_name in _COLUMNS:
        if tbl == table and col == column:
            return check_name
    raise KeyError(f"{table}.{column} not in migration _COLUMNS")


def _build_pre_migration_schema(metadata: MetaData) -> None:
    """Define the PRE-migration (legacy) schema the migration expects.

    Models the three AgentStage-backed columns with their legacy named CHECKs,
    the FK chain between the AI tables, and the minimum NOT-NULL scaffolding for
    inserts. ``campaigns`` and ``agent_runs`` parent rows are seeded so every
    child FK resolves.
    """
    Table("campaigns", metadata, Column("id", String, primary_key=True))

    Table(
        "agent_runs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("created_at", DateTime, nullable=False),
    )

    Table(
        "agent_iterations",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("run_id", Integer, ForeignKey("agent_runs.id"), nullable=False),
        Column("index", Integer, nullable=False),
        Column(
            "refined_stage",
            String(),
            nullable=_nullable_for("agent_iterations", "refined_stage"),
        ),
        Column("created_at", DateTime, nullable=False),
        _legacy_check("refined_stage", _check_name_for("agent_iterations", "refined_stage")),
    )

    Table(
        "agent_stage_outputs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("iteration_id", Integer, ForeignKey("agent_iterations.id"), nullable=False),
        Column(
            "stage",
            String(),
            nullable=_nullable_for("agent_stage_outputs", "stage"),
        ),
        Column("payload", JSON, nullable=False),
        Column("created_at", DateTime, nullable=False),
        _legacy_check("stage", _check_name_for("agent_stage_outputs", "stage")),
    )

    Table(
        "experience_memory",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column(
            "stage",
            String(),
            nullable=_nullable_for("experience_memory", "stage"),
        ),
        Column("content", Text, nullable=False),
        Column("created_at", DateTime, nullable=False),
        _legacy_check("stage", _check_name_for("experience_memory", "stage")),
    )


def _build_pre_migration_engine() -> tuple[Engine, Path, MetaData]:
    """Create a fresh temp-file SQLite engine with the PRE-migration schema and
    seed the parent rows (``campaigns`` / ``agent_runs`` / ``agent_iterations``)
    the stage rows reference."""
    tmp_dir = tempfile.mkdtemp(prefix="agent_stage_rename_mig_")
    db_path = Path(tmp_dir) / "agent_stage_rename.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    metadata = MetaData()
    _build_pre_migration_schema(metadata)
    metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(insert(metadata.tables["campaigns"]), {"id": _CAMPAIGN_ID})
        conn.execute(
            insert(metadata.tables["agent_runs"]),
            {"id": 1, "campaign_id": _CAMPAIGN_ID, "created_at": _TS},
        )
        conn.execute(
            insert(metadata.tables["agent_iterations"]),
            {
                "id": 1,
                "run_id": 1,
                "index": 0,
                "refined_stage": "stage_3_dag",
                "created_at": _TS,
            },
        )
        conn.execute(
            insert(metadata.tables["agent_stage_outputs"]),
            {
                "id": 1,
                "iteration_id": 1,
                "stage": "stage_3_dag",
                "payload": {"note": "legacy dag output"},
                "created_at": _TS,
            },
        )
        conn.execute(
            insert(metadata.tables["experience_memory"]),
            {
                "id": 1,
                "campaign_id": _CAMPAIGN_ID,
                "stage": "stage_3_dag",
                "content": "legacy dag experience",
                "created_at": _TS,
            },
        )

    return engine, db_path, metadata


def _run_migration_step(engine: Engine, step: str) -> None:
    """Drive the migration's REAL ``upgrade``/``downgrade`` through Alembic's
    Operations proxy bound to the ISOLATED temp engine (``render_as_batch=True``
    so SQLite rebuilds the tables — the project's ``env.py`` contract)."""
    func = getattr(_MIGRATION, step)
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            op_proxy = Operations(ctx)
            original_op = _MIGRATION.op
            _MIGRATION.op = op_proxy
            try:
                func()
            finally:
                _MIGRATION.op = original_op


def _read_stage(engine: Engine, metadata: MetaData, table: str, column: str) -> str:
    """Read the single stage value from the one seeded row of ``table.column``."""
    tbl = metadata.tables[table]
    with engine.connect() as conn:
        return conn.execute(select(tbl.c[column]).where(tbl.c[_PK[table]] == 1)).scalar_one()


# ===========================================================================
# Property 2 (task 2.2): deterministic migration round-trip.
# ===========================================================================


def test_agent_stage_rename_roundtrip_is_lossless() -> None:
    """``upgrade`` rewrites a seeded ``stage_3_dag`` row to ``dag`` on every
    AgentStage-backed column; ``downgrade`` reverts it to ``stage_3_dag`` — a
    lossless round-trip.

    # Feature: agentic-phase-model-refactor — task 2.2, Property 2: Migração
    # reversível e preservadora.
    Validates: Requirements 1.3, 9.3
    """
    engine, db_path, metadata = _build_pre_migration_engine()
    try:
        stage_columns = [
            (table, column) for (table, column, _nullable, _cn) in _COLUMNS
        ]

        # Precondition: every AgentStage-backed row holds the legacy value.
        for table, column in stage_columns:
            assert _read_stage(engine, metadata, table, column) == "stage_3_dag"

        # --- upgrade(): legacy -> semantic ------------------------------
        _run_migration_step(engine, "upgrade")

        for table, column in stage_columns:
            assert _read_stage(engine, metadata, table, column) == _SEMANTIC_OF["stage_3_dag"], (
                f"{table}.{column} was not rewritten stage_3_dag -> dag on upgrade"
            )
        # The mapping under test is exactly stage_3_dag -> dag.
        assert _SEMANTIC_OF["stage_3_dag"] == "dag"

        # --- downgrade(): semantic -> legacy ----------------------------
        _run_migration_step(engine, "downgrade")

        for table, column in stage_columns:
            assert _read_stage(engine, metadata, table, column) == "stage_3_dag", (
                f"{table}.{column} was not reverted dag -> stage_3_dag on downgrade"
            )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
