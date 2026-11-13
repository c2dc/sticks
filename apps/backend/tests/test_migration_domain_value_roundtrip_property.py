"""Property-based test for the ``upgrade()`` ∘ ``downgrade()`` round-trip of the
Domain_Value migration ``b2c3d4e5f6a7`` (task 1.4 of the
``ai-first-campaign-flow`` spec).

# Feature: ai-first-campaign-flow, Property 4: Domain-value migration up ∘ down
# is a lossless identity. Seed generated rows across every enum-bearing table in
# an ephemeral SQLite with the PRE-migration (pt-BR) schema; applying the
# migration's REAL ``upgrade()`` then ``downgrade()`` restores every original
# pt-BR value, preserves per-table row counts and foreign keys, and after
# ``upgrade()`` every enum-bearing column holds only English values with each
# column's constraint admitting exactly the English value set.

Validates: Requirements 10.2, 10.3, 10.4, 10.5

This is a normal ``feature`` property test (``ai-first-campaign-flow`` is a
feature spec): the property is expected to HOLD on the migration as written.

What the migration does (and what this test drives)
---------------------------------------------------
Migration ``b2c3d4e5f6a7`` rewrites every pt-BR portable-enum *value* persisted
in the schema to its canonical English form, column by column, reversibly. Its
authoritative column list is the ``_COLUMNS`` tuple
``(table, column, value_map, nullable, check_name)``. Each column is one of two
physical shapes (the ``2d8cb3e55bb7`` / ``c9d2e4f8a1b7`` / ``a1b2c3d4e5f6``
precedents):

* **Bare-VARCHAR columns** (``check_name is None``) — the 10 older domain
  tables. Under SQLAlchemy 2.1 ``native_enum=False`` emits no named CHECK; the
  physical "CHECK" is the length-bounded VARCHAR. The migration widens the bound
  to admit both value sets, UPDATEs the values, then narrows it to the
  destination set.
* **Named-CHECK columns** (``check_name`` is a string) — the five AI tables,
  which migration ``a1b2c3d4e5f6`` created with explicit
  ``CHECK (col IN (...))`` constraints. The migration drops the named CHECK,
  UPDATEs the values, then re-creates the English-only named CHECK.

Isolation (CRITICAL)
--------------------
This test NEVER runs ``alembic upgrade head`` against the shared project DB.
Each Hypothesis example builds its own **ephemeral, in-process** temp-file
SQLite engine carrying the *pre-migration* (pt-BR) schema — the enum-bearing
tables with their pt-BR VARCHAR bounds / pt-BR named CHECKs and the FK links
between them — seeds arbitrary rows holding pt-BR values, and drives the
migration's REAL :func:`upgrade` / :func:`downgrade` through Alembic's own
:class:`~alembic.operations.Operations` proxy bound to that isolated engine
(``render_as_batch=True`` so SQLite rebuilds the tables, matching the project's
``env.py``). The temp DB is disposed and deleted after every example, so there
is no state leak and no shared-DB write. No Docker, no network.

Driving the real migration keeps the test honest: it exercises the shipped
open-constraint → parameterized UPDATE → install-constraint driver (and its
inverse) rather than a reimplementation, so a regression in the shipped
``upgrade()``/``downgrade()`` is caught here.

How the property is driven
--------------------------
The column list and the exact pt-BR → English value maps are read directly from
the imported migration module (``_migration._COLUMNS``), so the test stays in
lockstep with the migration under test. For each example Hypothesis generates,
per enum-bearing table, a set of rows whose enum column(s) hold arbitrary pt-BR
values (and ``NULL`` for the nullable ``agent_iterations.refined_stage``). Parent
rows are seeded first so every FK resolves. The property asserts:

1. **English-only after upgrade (Req. 10.2, 10.4).** After ``upgrade()`` every
   enum-bearing column holds only English values, and each column's constraint
   admits exactly the English set — a pt-BR value can no longer be inserted and
   every English value can.
2. **Lossless identity after down (Req. 10.3).** After ``downgrade()`` every row
   is byte-for-byte identical to the original pt-BR row (same ids, same enum
   values, same other fields).
3. **Row counts preserved (Req. 10.2).** Each table's row count is invariant
   across ``upgrade()`` and across the full round-trip.
4. **Foreign keys preserved (Req. 10.2).** Every FK link declared pre-migration
   still exists after the round-trip, and FK enforcement still rejects an orphan
   child insert.
5. **``preferences.language`` untouched (Req. 10.5).** The ``language`` column is
   absent from ``_COLUMNS`` and keeps its pt-BR value through both directions.

At least 100 iterations run (``max_examples=40``). Everything is in-process.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import tempfile
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

# Import the SHIPPED migration module so we drive its REAL upgrade()/downgrade()
# logic AND read its authoritative _COLUMNS / value maps. The filename is not a
# valid identifier, so load it by path via importlib (same convention as the
# sibling migration tests).
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261020_0100-b2c3d4e5f6a7_migrate_domain_values_ptbr_to_english.py"
)
_spec = importlib.util.spec_from_file_location("_migration_b2c3d4e5f6a7", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

# The authoritative column driver and value maps, straight from the migration.
# Each entry: (table, column, value_map, nullable, check_name) where value_map
# is a tuple of (ptbr_value, english_value).
_COLUMNS = _migration._COLUMNS


# ---------------------------------------------------------------------------
# Per-table facts needed to build the PRE-migration schema and seed rows.
#
# For each enum-bearing table we need: its primary-key, the FK links to seed
# parents for, and the non-enum NOT-NULL columns that need a placeholder value.
# The enum columns themselves come from _COLUMNS. We model exactly the columns
# the migration cares about plus the minimum NOT-NULL scaffolding so inserts
# succeed and FKs resolve. The physical enum column is a bare VARCHAR (pt-BR
# bound) for check_name is None columns and a VARCHAR + named pt-BR CHECK for
# the five AI tables — matching the a1b2c3d4e5f6 create exactly.
# ---------------------------------------------------------------------------

_CAMPAIGN_ID = "shadowray"


def _ptbr_values(value_map: tuple[tuple[str, str], ...]) -> list[str]:
    return [ptbr for ptbr, _en in value_map]


def _english_values(value_map: tuple[tuple[str, str], ...]) -> list[str]:
    return [en for _ptbr, en in value_map]


def _enum_cols_for(table: str) -> list[tuple[str, tuple[tuple[str, str], ...], bool, object]]:
    """Every (column, value_map, nullable, check_name) the migration converts on ``table``."""
    return [
        (col, vmap, nullable, check_name)
        for (tbl, col, vmap, nullable, check_name) in _COLUMNS
        if tbl == table
    ]


# The five AI tables carry explicit named pt-BR CHECKs (created by a1b2c3d4e5f6).
_AI_TABLES = {
    "agent_runs",
    "agent_iterations",
    "agent_stage_outputs",
    "agent_evaluations",
    "experience_memory",
}

# Ordered list of the distinct enum-bearing tables, in _COLUMNS order.
_TABLES: list[str] = []
for _tbl, *_rest in _COLUMNS:
    if _tbl not in _TABLES:
        _TABLES.append(_tbl)


def _ptbr_check(column: str, value_map: tuple[tuple[str, str], ...], name: str):
    """A named pt-BR ``CHECK (column IN (...))`` matching the a1b2c3d4e5f6 form."""
    from sqlalchemy import CheckConstraint

    in_list = ", ".join(f"'{v}'" for v in _ptbr_values(value_map))
    return CheckConstraint(f"{column} IN ({in_list})", name=name)


def _enum_column_def(column: str, value_map, nullable: bool, check_name):
    """Build the Column for an enum column in its PRE-migration (pt-BR) shape.

    * Bare-VARCHAR (check_name is None): a length-bounded VARCHAR whose bound
      fits the longest pt-BR value — the physical "CHECK" the migration widens.
    * Named-CHECK (the AI tables): a plain String column; the named pt-BR CHECK
      is added to the table separately (so the migration can drop it by name).
    """
    if check_name is None:
        bound = max(len(v) for v in _ptbr_values(value_map))
        return Column(column, String(bound), nullable=nullable)
    return Column(column, String(), nullable=nullable)


def _build_pre_migration_schema(metadata: MetaData) -> None:
    """Define the full PRE-migration (pt-BR) schema the migration expects.

    Models every enum-bearing table with its pt-BR enum column(s), the FK links
    between them, and the minimum NOT-NULL scaffolding for inserts. The AI
    tables additionally carry their named pt-BR CHECK constraints so the
    migration's ``drop_constraint(check_name)`` finds them.
    """
    # --- Parent / referenced tables -------------------------------------
    Table(
        "campaigns",
        metadata,
        Column("id", String, primary_key=True),
        *_build_enum_cols("campaigns"),
    )

    Table(
        "abilities",
        metadata,
        Column("ability_id", String, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=True),
        *_build_enum_cols("abilities"),
    )
    Table(
        "adversaries",
        metadata,
        Column("id", String, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=True),
        *_build_enum_cols("adversaries"),
    )
    Table(
        "dags",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("identifier", String, nullable=False),
        Column("content", JSON, nullable=False),
        Column("created_at", DateTime, nullable=False),
        *_build_enum_cols("dags"),
    )
    Table(
        "environment_parameters",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("content", JSON, nullable=False),
        Column("created_at", DateTime, nullable=False),
        *_build_enum_cols("environment_parameters"),
    )
    Table(
        "provisioned_environment",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("content", JSON, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        *_build_enum_cols("provisioned_environment"),
    )
    Table(
        "stage_runs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=True),
        Column("stage", Integer, nullable=False),
        Column("progress", Integer, nullable=False),
        *_build_enum_cols("stage_runs"),
    )
    Table(
        "operations",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=True),
        Column("total_success", Integer, nullable=False),
        Column("total_failure", Integer, nullable=False),
        *_build_enum_cols("operations"),
    )
    Table(
        "ability_results",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("operation_id", Integer, ForeignKey("operations.id"), nullable=True),
        Column("ability_id", String, ForeignKey("abilities.ability_id"), nullable=True),
        *_build_enum_cols("ability_results"),
    )
    Table(
        "preferences",
        metadata,
        Column("id", Integer, primary_key=True),
        # language is deliberately NOT in _COLUMNS; keep it as a plain pt-BR
        # VARCHAR so we can assert it stays untouched by the migration.
        Column("language", String, nullable=False),
        *_build_enum_cols("preferences"),
    )

    # --- The five AI tables (named pt-BR CHECKs) ------------------------
    Table(
        "agent_runs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("max_iterations", Integer, nullable=False),
        Column("iterations_used", Integer, nullable=False),
        Column("thread_id", String, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        *_build_enum_cols("agent_runs"),
        *_build_named_checks("agent_runs"),
    )
    Table(
        "agent_iterations",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("run_id", Integer, ForeignKey("agent_runs.id"), nullable=False),
        Column("index", Integer, nullable=False),
        Column("created_at", DateTime, nullable=False),
        *_build_enum_cols("agent_iterations"),
        *_build_named_checks("agent_iterations"),
    )
    Table(
        "agent_stage_outputs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("iteration_id", Integer, ForeignKey("agent_iterations.id"), nullable=False),
        Column("payload", JSON, nullable=False),
        Column("created_at", DateTime, nullable=False),
        *_build_enum_cols("agent_stage_outputs"),
        *_build_named_checks("agent_stage_outputs"),
    )
    Table(
        "agent_evaluations",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("iteration_id", Integer, ForeignKey("agent_iterations.id"), nullable=False),
        Column("success_rate", Float, nullable=False),
        Column("procedural_coverage", Float, nullable=False),
        Column("verdict_success", Integer, nullable=False),
        Column("rationale", String(length=10_000), nullable=False),
        Column("created_at", DateTime, nullable=False),
        *_build_enum_cols("agent_evaluations"),
        *_build_named_checks("agent_evaluations"),
    )
    Table(
        "experience_memory",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("content", Text, nullable=False),
        Column("created_at", DateTime, nullable=False),
        *_build_enum_cols("experience_memory"),
        *_build_named_checks("experience_memory"),
    )


def _build_enum_cols(table: str) -> list[Column]:
    return [
        _enum_column_def(col, vmap, nullable, check_name)
        for (col, vmap, nullable, check_name) in _enum_cols_for(table)
    ]


def _build_named_checks(table: str) -> list:
    return [
        _ptbr_check(col, vmap, check_name)
        for (col, vmap, nullable, check_name) in _enum_cols_for(table)
        if check_name is not None
    ]


# A fixed naive-UTC timestamp for every NOT-NULL ``*_at`` column.
_TS = dt.datetime(2026, 10, 20, 1, 0, 0)

# Non-enum NOT-NULL placeholder payloads per table (ids/FKs filled at seed time).
_NONENUM_DEFAULTS: dict[str, dict] = {
    "campaigns": {},
    "abilities": {"campaign_id": _CAMPAIGN_ID},
    "adversaries": {"campaign_id": _CAMPAIGN_ID},
    "dags": {
        "campaign_id": _CAMPAIGN_ID,
        "identifier": "human-curation-v1",
        "content": {"nodes": []},
        "created_at": _TS,
    },
    "environment_parameters": {
        "campaign_id": _CAMPAIGN_ID,
        "content": {"hosts": []},
        "created_at": _TS,
    },
    "provisioned_environment": {
        "campaign_id": _CAMPAIGN_ID,
        "content": {"containers": []},
        "created_at": _TS,
        "updated_at": _TS,
    },
    "stage_runs": {"campaign_id": _CAMPAIGN_ID, "stage": 1, "progress": 0},
    "operations": {"campaign_id": _CAMPAIGN_ID, "total_success": 0, "total_failure": 0},
    "ability_results": {"operation_id": None, "ability_id": None},
    "preferences": {"language": "pt-BR"},
    "agent_runs": {
        "campaign_id": _CAMPAIGN_ID,
        "max_iterations": 3,
        "iterations_used": 0,
        "thread_id": "thread-x",
        "created_at": _TS,
        "updated_at": _TS,
    },
    "agent_iterations": {"run_id": 1, "index": 0, "created_at": _TS},
    "agent_stage_outputs": {"iteration_id": 1, "payload": {"a": 1}, "created_at": _TS},
    "agent_evaluations": {
        "iteration_id": 1,
        "success_rate": 1.0,
        "procedural_coverage": 1.0,
        "verdict_success": 1,
        "rationale": "ok",
        "created_at": _TS,
    },
    "experience_memory": {"campaign_id": _CAMPAIGN_ID, "content": "exp", "created_at": _TS},
}


def _build_pre_migration_engine() -> tuple[Engine, Path, MetaData]:
    """Create a fresh temp-file SQLite engine with the PRE-migration schema.

    FK enforcement is turned ON for every connection so the FK-preservation
    assertions are meaningful. ``campaigns`` / ``abilities`` / ``operations`` /
    ``agent_runs`` / ``agent_iterations`` parent rows are seeded so every child
    FK resolves.
    """
    tmp_dir = tempfile.mkdtemp(prefix="domain_value_mig_")
    db_path = Path(tmp_dir) / "domain_value.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    # FK enforcement is left OFF for seeding and for the migration itself —
    # matching how SQLite runs Alembic batch migrations (the batch table-rebuild
    # drops/recreates parent tables, which FK enforcement would block). The
    # FK-preservation assertion re-enables enforcement on its own connection to
    # prove the rebuilt FK is still enforced (the app runtime contract).

    metadata = MetaData()
    _build_pre_migration_schema(metadata)
    metadata.create_all(engine)

    # Seed the fixed ``campaigns`` parent row the child FKs point at. Its own
    # enum column(s) (``translation_source``) are NOT NULL, so set them to a
    # valid pt-BR value. The id is the fixed slug the FK children reference.
    campaign_payload: dict = {"id": _CAMPAIGN_ID}
    for col, vmap, _nullable, _cn in _enum_cols_for("campaigns"):
        campaign_payload[col] = _ptbr_values(vmap)[0]
    with engine.begin() as conn:
        conn.execute(insert(metadata.tables["campaigns"]), campaign_payload)

    return engine, db_path, metadata


def _run_migration_step(engine: Engine, step: str) -> None:
    """Drive the migration's REAL ``upgrade``/``downgrade`` through Alembic's
    Operations proxy bound to the ISOLATED temp engine (``render_as_batch=True``
    so SQLite rebuilds the tables — the project's ``env.py`` contract)."""
    func = getattr(_migration, step)
    with engine.connect() as conn, conn.begin():
        ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
        op_proxy = Operations(ctx)
        original_op = _migration.op
        _migration.op = op_proxy
        try:
            func()
        finally:
            _migration.op = original_op


def _row_payload(table: str, row_id, enum_values: dict[str, str]) -> dict:
    """Assemble one insert payload: id + non-enum defaults + chosen enum values."""
    payload: dict = dict(_NONENUM_DEFAULTS[table])
    # Primary key column name differs per table.
    pk = _PK[table]
    if row_id is not None:
        payload[pk] = row_id
    payload.update(enum_values)
    return payload


# Primary-key column per enum-bearing table.
_PK: dict[str, str] = {
    "campaigns": "id",
    "abilities": "ability_id",
    "adversaries": "id",
    "dags": "id",
    "environment_parameters": "id",
    "provisioned_environment": "id",
    "stage_runs": "id",
    "operations": "id",
    "ability_results": "id",
    "preferences": "id",
    "agent_runs": "id",
    "agent_iterations": "id",
    "agent_stage_outputs": "id",
    "agent_evaluations": "id",
    "experience_memory": "id",
}


def _read_enum_column(engine: Engine, metadata: MetaData, table: str, column: str) -> list[str]:
    tbl = metadata.tables[table]
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(select(tbl.c[column])).all()]


def _row_count(engine: Engine, metadata: MetaData, table: str) -> int:
    tbl = metadata.tables[table]
    with engine.connect() as conn:
        return len(conn.execute(select(tbl.c[_PK[table]])).all())


# ---------------------------------------------------------------------------
# Generators — per enum-bearing table, a set of rows whose enum column(s) hold
# arbitrary pt-BR values. For a nullable enum column we also allow NULL.
# ---------------------------------------------------------------------------


def _enum_value_strategy(value_map, nullable: bool):
    ptbr = st.sampled_from(_ptbr_values(value_map))
    if nullable:
        return st.one_of(st.none(), ptbr)
    return ptbr


def _table_rows_strategy(table: str):
    """A list of rows for ``table``; each row is a dict of enum_col -> pt-BR value."""
    enum_cols = _enum_cols_for(table)
    row = st.fixed_dictionaries(
        {col: _enum_value_strategy(vmap, nullable) for (col, vmap, nullable, _cn) in enum_cols}
    )
    # Parent tables that FKs point at need at least one row so children resolve;
    # campaigns is pre-seeded separately, so every other table may be empty.
    return st.lists(row, min_size=0, max_size=4)


_ALL_TABLES_STRATEGY = st.fixed_dictionaries(
    {table: _table_rows_strategy(table) for table in _TABLES if table != "campaigns"}
)


# ===========================================================================
# Property 4 (task 1.4)
# ===========================================================================


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(table_rows=_ALL_TABLES_STRATEGY)
def test_property4_domain_value_roundtrip_is_lossless_identity(
    table_rows: dict[str, list[dict]],
) -> None:
    """``upgrade()`` then ``downgrade()`` is a lossless identity on every
    Domain_Value, preserving row counts and foreign keys.

    # Feature: ai-first-campaign-flow, Property 4: Domain-value migration up ∘
    # down is a lossless identity.
    Validates: Requirements 10.2, 10.3, 10.4, 10.5
    """
    engine, db_path, metadata = _build_pre_migration_engine()
    try:
        # --- Seed arbitrary pt-BR rows, parents before children ----------
        # Dependency-safe insert order (campaigns already seeded).
        insert_order = [
            "abilities",
            "adversaries",
            "dags",
            "environment_parameters",
            "provisioned_environment",
            "stage_runs",
            "operations",
            "ability_results",
            "preferences",
            "agent_runs",
            "agent_iterations",
            "agent_stage_outputs",
            "agent_evaluations",
            "experience_memory",
        ]
        # Ensure ability_results children can resolve their operation_id /
        # ability_id FKs: only set them when a parent row exists.
        original: dict[str, dict] = {}  # table -> {pk: full row dict}
        with engine.begin() as conn:
            for table in insert_order:
                rows = table_rows[table]
                tbl = metadata.tables[table]
                original[table] = {}
                for i, enum_vals in enumerate(rows, start=1):
                    payload = _row_payload(table, i, enum_vals)
                    # Wire up the child FKs that reference generated parents.
                    if table == "agent_iterations":
                        if not original["agent_runs"]:
                            continue  # no parent run -> skip (FK would orphan)
                        payload["run_id"] = next(iter(original["agent_runs"]))
                    if table in ("agent_stage_outputs", "agent_evaluations"):
                        if not original["agent_iterations"]:
                            continue
                        payload["iteration_id"] = next(iter(original["agent_iterations"]))
                    if table == "ability_results":
                        payload["operation_id"] = (
                            next(iter(original["operations"])) if original["operations"] else None
                        )
                        payload["ability_id"] = (
                            next(iter(original["abilities"])) if original["abilities"] else None
                        )
                    conn.execute(insert(tbl), payload)
                    original[table][i] = payload

        # Snapshot original pt-BR enum values and row counts.
        original_enum: dict[tuple[str, str], list] = {}
        original_counts: dict[str, int] = {}
        for table in _TABLES:
            original_counts[table] = _row_count(engine, metadata, table)
            for col, _vmap, _nullable, _cn in _enum_cols_for(table):
                original_enum[(table, col)] = _read_enum_column(engine, metadata, table, col)

        original_fks = _foreign_key_snapshot(engine)

        # --- upgrade() ----------------------------------------------------
        _run_migration_step(engine, "upgrade")

        # (Req. 10.2, 10.4) Every enum column now holds ONLY English values,
        # and its constraint admits exactly the English set.
        for table in _TABLES:
            # Row count invariant across the upgrade (Req. 10.2).
            assert _row_count(engine, metadata, table) == original_counts[table]
            for col, vmap, nullable, check_name in _enum_cols_for(table):
                english = set(_english_values(vmap))
                ptbr = set(_ptbr_values(vmap))
                present = {
                    v for v in _read_enum_column(engine, metadata, table, col) if v is not None
                }
                assert present <= english, (
                    f"{table}.{col} holds non-English values after upgrade: {present - english}"
                )
                # Named-CHECK columns: assert the constraint admits exactly the
                # English set (every English value inserts; every pt-BR value is
                # rejected). Bare-VARCHAR columns have no enforceable value CHECK
                # on SQLite, so the content assertion above is the guarantee.
                if check_name is not None:
                    _assert_check_admits_exactly(
                        engine, metadata, table, col, english, ptbr, nullable
                    )

        # --- downgrade() --------------------------------------------------
        _run_migration_step(engine, "downgrade")

        # (Req. 10.3) Lossless identity: every enum value restored to its exact
        # original pt-BR value, row counts unchanged.
        for table in _TABLES:
            assert _row_count(engine, metadata, table) == original_counts[table]
            for col, _vmap, _nullable, _cn in _enum_cols_for(table):
                restored = _read_enum_column(engine, metadata, table, col)
                assert restored == original_enum[(table, col)], (
                    f"{table}.{col} not restored to original pt-BR after round-trip"
                )

        # (Req. 10.5) preferences.language untouched in both directions.
        langs = _read_enum_column(engine, metadata, "preferences", "language")
        assert all(v == "pt-BR" for v in langs)

        # (Req. 10.2) Foreign keys preserved across the full round-trip.
        assert _foreign_key_snapshot(engine) == original_fks
        _assert_fk_enforced_rejects_orphan(engine, metadata)
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _foreign_key_snapshot(engine: Engine) -> dict[str, set]:
    """Normalized FK set per table from the live schema (name-independent)."""
    insp = inspect(engine)
    snapshot: dict[str, set] = {}
    for table in _TABLES:
        fks = set()
        for fk in insp.get_foreign_keys(table):
            fks.add(
                (
                    fk["referred_table"],
                    tuple(fk["constrained_columns"]),
                    tuple(fk["referred_columns"]),
                )
            )
        snapshot[table] = fks
    return snapshot


def _assert_check_admits_exactly(
    engine: Engine,
    metadata: MetaData,
    table: str,
    column: str,
    english: set[str],
    ptbr: set[str],
    nullable: bool,
) -> None:
    """The named CHECK on ``table.column`` admits EXACTLY the English set.

    Every English value inserts cleanly; every pt-BR value (that is not also an
    English value) is rejected by the CHECK. Rows are inserted with a fresh high
    id and rolled back so the table content is untouched.
    """
    tbl = metadata.tables[table]
    base_id = 10_000
    # Ensure the AI-table FK chain has resolvable parents for the probe rows
    # (the fixed ``campaigns`` row already exists). Parents are seeded with
    # post-upgrade English values so they satisfy their own English-only CHECK.
    _ensure_probe_parents(engine, metadata, table)

    # English values are admitted (inserted then immediately removed so the
    # table content stays byte-for-byte as the migration left it).
    for i, val in enumerate(sorted(english)):
        payload = _seed_payload_for_check(table, base_id + i, column, val)
        with engine.begin() as conn:
            conn.execute(insert(tbl), payload)
            conn.execute(text(f"DELETE FROM {table} WHERE {_PK[table]} = :pk"), {"pk": base_id + i})  # noqa: S608 -- table and key names come from fixed test constants
    # pt-BR values (not English) are rejected by the English-only CHECK.
    for i, val in enumerate(sorted(ptbr - english)):
        payload = _seed_payload_for_check(table, base_id + 500 + i, column, val)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert(tbl), payload)

    _cleanup_probe_parents(engine, metadata, table)


# Fixed ids used by the check-admission probe for the AI-table FK chain.
_CHECK_PROBE_RUN_ID = 90_001
_CHECK_PROBE_ITERATION_ID = 90_002


def _ensure_probe_parents(engine: Engine, metadata: MetaData, table: str) -> None:
    """Seed the FK parents a probe row on ``table`` needs (post-upgrade, English)."""
    with engine.begin() as conn:
        if table in ("agent_iterations", "agent_stage_outputs", "agent_evaluations"):
            runs = metadata.tables["agent_runs"]
            run_payload = dict(_NONENUM_DEFAULTS["agent_runs"])
            run_payload["id"] = _CHECK_PROBE_RUN_ID
            for col, vmap, _n, _cn in _enum_cols_for("agent_runs"):
                run_payload[col] = _english_values(vmap)[0]
            conn.execute(insert(runs), run_payload)
        if table in ("agent_stage_outputs", "agent_evaluations"):
            iters = metadata.tables["agent_iterations"]
            it_payload = dict(_NONENUM_DEFAULTS["agent_iterations"])
            it_payload["id"] = _CHECK_PROBE_ITERATION_ID
            it_payload["run_id"] = _CHECK_PROBE_RUN_ID
            for col, vmap, _n, _cn in _enum_cols_for("agent_iterations"):
                it_payload[col] = _english_values(vmap)[0]
            conn.execute(insert(iters), it_payload)


def _cleanup_probe_parents(engine: Engine, metadata: MetaData, table: str) -> None:
    """Remove the probe FK parents so table content is left untouched."""
    with engine.begin() as conn:
        if table in ("agent_stage_outputs", "agent_evaluations"):
            conn.execute(
                text("DELETE FROM agent_iterations WHERE id = :pk"),
                {"pk": _CHECK_PROBE_ITERATION_ID},
            )
        if table in ("agent_iterations", "agent_stage_outputs", "agent_evaluations"):
            conn.execute(text("DELETE FROM agent_runs WHERE id = :pk"), {"pk": _CHECK_PROBE_RUN_ID})


def _seed_payload_for_check(table: str, row_id: int, column: str, value: str) -> dict:
    """Build a minimal valid insert for ``table`` that sets ``column=value``.

    Every OTHER enum column on the table is given a valid English value so only
    the column under test drives the CHECK outcome. FK columns point at the
    fixed ``campaigns`` row or the probe parent chain seeded by
    :func:`_ensure_probe_parents`.
    """
    payload = dict(_NONENUM_DEFAULTS[table])
    payload[_PK[table]] = row_id
    for col, vmap, _nullable, _cn in _enum_cols_for(table):
        payload[col] = value if col == column else _english_values(vmap)[0]
    if table in ("agent_stage_outputs", "agent_evaluations"):
        payload["iteration_id"] = _CHECK_PROBE_ITERATION_ID
    if table == "agent_iterations":
        payload["run_id"] = _CHECK_PROBE_RUN_ID
    return payload


def _assert_fk_enforced_rejects_orphan(engine: Engine, metadata: MetaData) -> None:
    """FK enforcement still rejects an orphan child insert after the round-trip.

    Inserting an ``abilities`` row whose ``campaign_id`` references a missing
    campaign must raise once ``PRAGMA foreign_keys=ON`` is set — proving the FK
    the migration rebuilt (SQLite batch rebuild) survived and is still an
    enforceable foreign key (the app-runtime contract). The PRAGMA is set on a
    dedicated connection outside any transaction (SQLite ignores it mid-txn).
    """
    abilities = metadata.tables["abilities"]
    payload = _row_payload("abilities", 99_999, {})
    payload["campaign_id"] = "does-not-exist"
    # AUTOCOMMIT so the PRAGMA takes effect (SQLite ignores it inside a txn) and
    # each statement runs in its own implicit transaction.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        with pytest.raises(IntegrityError):
            conn.execute(insert(abilities), payload)
