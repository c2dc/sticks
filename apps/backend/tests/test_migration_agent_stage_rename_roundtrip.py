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

import pytest
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


# ===========================================================================
# Migration M1 ``e1f2a3b4c5d6`` (agentic-only-flow-redesign task 2.2).
#
# Property 1: Migration M1 round trip preserves data and maps dag to flow.
# Property 2: The curated flag is derived from curated flows.
# Validates: Requirements 1.5, 1.6, 5.3, 5.4
#
# Deterministic (no Hypothesis): a small fixed seed is driven through the REAL
# M1 ``upgrade()`` / ``downgrade()`` on an isolated temp SQLite database that
# carries the PRE-M1 schema (``dags``, ``stage_runs.chosen_dag_id`` with its
# named FK, ``campaigns`` without ``curated`` and the three AgentStage CHECKs
# over the dag-era value set).
# ===========================================================================

from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import Boolean, ForeignKeyConstraint, func, inspect, text  # noqa: E402

_M1 = _load_migration(
    "_migration_e1f2a3b4c5d6",
    "20261028_0100-e1f2a3b4c5d6_preserve_curated_and_rename_flow.py",
)

_M1_STAGE_TABLES: tuple[tuple[str, str], ...] = tuple(
    (table, column) for (table, column, _cn) in _M1._COLUMNS
)

# campaign id -> expected ``curated`` after upgrade.
_EXPECTED_CURATED: dict[str, bool] = {
    "curated_only": True,  # owns a human_curation flow
    "mixed": True,  # human_curation + ai_generated flows
    "ai_only": False,  # only an ai_generated flow
    "no_flows": False,  # no flow at all
}

_M1_TABLES_COUNTED = (
    "campaigns",
    "dags",
    "stage_runs",
    "agent_iterations",
    "agent_stage_outputs",
    "experience_memory",
)


def _build_pre_m1_engine() -> tuple[Engine, Path]:
    """Temp-file SQLite engine with the PRE-M1 schema and a fixed seed."""
    tmp_dir = tempfile.mkdtemp(prefix="flow_rename_m1_")
    db_path = Path(tmp_dir) / "flow_rename_m1.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    md = MetaData()
    Table("campaigns", md, Column("id", String, primary_key=True))
    Table(
        "dags",
        md,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("origin", String, nullable=False),
        Column("content", JSON, nullable=False),
    )
    Table(
        "stage_runs",
        md,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("chosen_dag_id", Integer, nullable=True),
        ForeignKeyConstraint(
            ["chosen_dag_id"], ["dags.id"], name=_M1._FK_OLD_NAME
        ),
    )
    Table(
        "agent_runs",
        md,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("created_at", DateTime, nullable=False),
    )

    def _dag_check(column: str, name: str) -> CheckConstraint:
        return CheckConstraint(_M1._check_expr(column, _M1._DAG_VALUES), name=name)

    check_names = {(t, c): n for (t, c, n) in _M1._COLUMNS}
    Table(
        "agent_iterations",
        md,
        Column("id", Integer, primary_key=True),
        Column("run_id", Integer, ForeignKey("agent_runs.id"), nullable=False),
        Column("index", Integer, nullable=False),
        Column("refined_stage", String(), nullable=True),
        Column("created_at", DateTime, nullable=False),
        _dag_check(
            "refined_stage", check_names[("agent_iterations", "refined_stage")]
        ),
    )
    Table(
        "agent_stage_outputs",
        md,
        Column("id", Integer, primary_key=True),
        Column("iteration_id", Integer, ForeignKey("agent_iterations.id"), nullable=False),
        Column("stage", String(), nullable=False),
        Column("payload", JSON, nullable=False),
        Column("created_at", DateTime, nullable=False),
        _dag_check("stage", check_names[("agent_stage_outputs", "stage")]),
    )
    Table(
        "experience_memory",
        md,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("stage", String(), nullable=False),
        Column("content", Text, nullable=False),
        Column("created_at", DateTime, nullable=False),
        _dag_check("stage", check_names[("experience_memory", "stage")]),
    )
    md.create_all(engine)

    with engine.begin() as conn:
        for cid in _EXPECTED_CURATED:
            conn.execute(insert(md.tables["campaigns"]), {"id": cid})
        flows = [
            (1, "curated_only", "human_curation"),
            (2, "mixed", "human_curation"),
            (3, "mixed", "ai_generated"),
            (4, "ai_only", "ai_generated"),
        ]
        for fid, cid, origin in flows:
            conn.execute(
                insert(md.tables["dags"]),
                {"id": fid, "campaign_id": cid, "origin": origin, "content": {"n": fid}},
            )
        conn.execute(
            insert(md.tables["stage_runs"]),
            [
                {"id": 1, "campaign_id": "curated_only", "chosen_dag_id": 1},
                {"id": 2, "campaign_id": "ai_only", "chosen_dag_id": None},
            ],
        )
        conn.execute(
            insert(md.tables["agent_runs"]),
            {"id": 1, "campaign_id": "ai_only", "created_at": _TS},
        )
        # Row 1 carries ``dag``; row 2 a non-dag stage; row 3 a NULL refined_stage.
        conn.execute(
            insert(md.tables["agent_iterations"]),
            [
                {"id": 1, "run_id": 1, "index": 0, "refined_stage": "dag", "created_at": _TS},
                {"id": 2, "run_id": 1, "index": 1, "refined_stage": "environment", "created_at": _TS},
                {"id": 3, "run_id": 1, "index": 2, "refined_stage": None, "created_at": _TS},
            ],
        )
        conn.execute(
            insert(md.tables["agent_stage_outputs"]),
            [
                {"id": 1, "iteration_id": 1, "stage": "dag", "payload": {}, "created_at": _TS},
                {"id": 2, "iteration_id": 1, "stage": "execution", "payload": {}, "created_at": _TS},
            ],
        )
        conn.execute(
            insert(md.tables["experience_memory"]),
            [
                {"id": 1, "campaign_id": "ai_only", "stage": "dag", "content": "a", "created_at": _TS},
                {"id": 2, "campaign_id": "ai_only", "stage": "evaluation", "content": "b", "created_at": _TS},
            ],
        )
    return engine, db_path


def _run_m1(engine: Engine, step: str) -> None:
    func_ = getattr(_M1, step)
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            original_op = _M1.op
            _M1.op = Operations(ctx)
            try:
                func_()
            finally:
                _M1.op = original_op


def _counts(engine: Engine, flow_table: str) -> dict[str, int]:
    """Row counts keyed by the logical (pre-rename) table name."""
    with engine.connect() as conn:
        return {
            t: conn.execute(
                text(f"SELECT COUNT(*) FROM {flow_table if t == 'dags' else t}")
            ).scalar_one()
            for t in _M1_TABLES_COUNTED
        }


def _stage_values(engine: Engine) -> dict[tuple[str, str], list[str | None]]:
    out: dict[tuple[str, str], list[str | None]] = {}
    with engine.connect() as conn:
        for table, column in _M1_STAGE_TABLES:
            rows = conn.execute(text(f"SELECT {column} FROM {table} ORDER BY id")).all()
            out[(table, column)] = [r[0] for r in rows]
    return out


def _cleanup(engine: Engine, db_path: Path) -> None:
    engine.dispose()
    try:
        db_path.unlink()
        db_path.parent.rmdir()
    except OSError:
        pass


def test_m1_roundtrip_maps_dag_to_flow_and_preserves_rows() -> None:
    """Property 1: upgrade maps dag->flow and renames dags/chosen_dag_id;
    downgrade restores everything; row counts never change.

    Validates: Requirements 1.5, 1.6, 5.3
    """
    engine, db_path = _build_pre_m1_engine()
    try:
        before_counts = _counts(engine, "dags")
        before_stages = _stage_values(engine)
        with engine.connect() as conn:
            before_flows = conn.execute(
                text("SELECT id, campaign_id, origin, content FROM dags ORDER BY id")
            ).all()
        assert before_stages[("agent_iterations", "refined_stage")] == ["dag", "environment", None]

        # --- upgrade -----------------------------------------------------
        _run_m1(engine, "upgrade")

        insp = inspect(engine)
        assert "attack_flows" in insp.get_table_names()
        assert "dags" not in insp.get_table_names()
        stage_cols = {c["name"] for c in insp.get_columns("stage_runs")}
        assert "chosen_flow_id" in stage_cols and "chosen_dag_id" not in stage_cols
        fks = {fk["name"]: fk for fk in insp.get_foreign_keys("stage_runs")}
        assert _M1._FK_NEW_NAME in fks and _M1._FK_OLD_NAME not in fks
        assert fks[_M1._FK_NEW_NAME]["referred_table"] == "attack_flows"

        assert _counts(engine, "attack_flows") == before_counts
        after_stages = _stage_values(engine)
        assert after_stages[("agent_iterations", "refined_stage")] == ["flow", "environment", None]
        assert after_stages[("agent_stage_outputs", "stage")] == ["flow", "execution"]
        assert after_stages[("experience_memory", "stage")] == ["flow", "evaluation"]
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT chosen_flow_id FROM stage_runs ORDER BY id")
            ).scalars().all() == [1, None]
            after_flows = conn.execute(
                text("SELECT id, campaign_id, origin, content FROM attack_flows ORDER BY id")
            ).all()
        assert after_flows == before_flows

        # The regenerated CHECK accepts ``flow`` and rejects ``dag``.
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE experience_memory SET stage = 'flow' WHERE id = 2")
            )
        for bad in ("dag",):
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("UPDATE experience_memory SET stage = :s WHERE id = 2"),
                        {"s": bad},
                    )
            except Exception:
                pass
            else:
                raise AssertionError(f"CHECK still accepts {bad!r} after upgrade")
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE experience_memory SET stage = 'evaluation' WHERE id = 2")
            )

        # --- downgrade ---------------------------------------------------
        _run_m1(engine, "downgrade")

        insp = inspect(engine)
        assert "dags" in insp.get_table_names()
        assert "attack_flows" not in insp.get_table_names()
        stage_cols = {c["name"] for c in insp.get_columns("stage_runs")}
        assert "chosen_dag_id" in stage_cols and "chosen_flow_id" not in stage_cols
        assert _M1._FK_OLD_NAME in {fk["name"] for fk in insp.get_foreign_keys("stage_runs")}
        assert "curated" not in {c["name"] for c in insp.get_columns("campaigns")}

        assert _counts(engine, "dags") == before_counts
        assert _stage_values(engine) == before_stages
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT id, campaign_id, origin, content FROM dags ORDER BY id")
            ).all() == before_flows
            assert conn.execute(
                text("SELECT chosen_dag_id FROM stage_runs ORDER BY id")
            ).scalars().all() == [1, None]
    finally:
        _cleanup(engine, db_path)


def test_m1_curated_flag_true_only_for_human_curation_campaigns() -> None:
    """Property 2: ``campaigns.curated`` is true exactly for campaigns owning a
    ``human_curation`` flow; the column is NOT NULL Boolean defaulting to false.

    Validates: Requirements 1.5, 5.4
    """
    engine, db_path = _build_pre_m1_engine()
    try:
        _run_m1(engine, "upgrade")

        col = {c["name"]: c for c in inspect(engine).get_columns("campaigns")}["curated"]
        assert col["nullable"] is False
        assert isinstance(col["type"], Boolean) or "BOOL" in str(col["type"]).upper()

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, curated FROM campaigns")).all()
        assert {r[0]: bool(r[1]) for r in rows} == _EXPECTED_CURATED
        assert sum(1 for r in rows if r[1]) == 2

        # A campaign inserted without the flag defaults to not curated.
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO campaigns (id) VALUES ('fresh')"))
            assert conn.execute(
                text("SELECT curated FROM campaigns WHERE id = 'fresh'")
            ).scalar_one() in (0, False)
    finally:
        _cleanup(engine, db_path)


def test_alembic_has_single_head_and_m1_is_chained_on_previous_head() -> None:
    """The migration graph has exactly one head and M1 hangs off ``d1e2f3a4b5c6``.

    Validates: Requirements 5.3
    """
    script = ScriptDirectory(str(_VERSIONS_DIR.parent))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single Alembic head, got {heads}"
    assert script.get_revision("e1f2a3b4c5d6").down_revision == "d1e2f3a4b5c6"
    assert _M1.revision == "e1f2a3b4c5d6"
    assert script.get_revision("f2a3b4c5d6e7").down_revision == "e1f2a3b4c5d6"
    assert script.get_revision("a3b4c5d6e7f8").down_revision == "f2a3b4c5d6e7"
    assert script.get_revision("b4c5d6e7f8a9").down_revision == "a3b4c5d6e7f8"
    assert script.get_revision("c5d6e7f8a9b0").down_revision == "b4c5d6e7f8a9"
    assert script.get_revision("d6e7f8a9b0c1").down_revision == "c5d6e7f8a9b0"
    assert script.get_revision("e7f8a9b0c1d2").down_revision == "d6e7f8a9b0c1"
    assert script.get_revision("f8a9b0c1d2e3").down_revision == "e7f8a9b0c1d2"
    assert heads == ["f8a9b0c1d2e3"]


# ===========================================================================
# Migration M2 ``f2a3b4c5d6e7`` (agentic-only-flow-redesign task 6.1).
# Round trip on the POST-M1 schema: corpus columns, nullable campaign_id,
# origin CHECK, partial unique index, new tables and seeded ingest state.
# ===========================================================================

_M2 = _load_migration(
    "_migration_f2a3b4c5d6e7",
    "20261029_0100-f2a3b4c5d6e7_create_attack_flow_corpus_and_ingest_state.py",
)


def _run_m2(engine: Engine, step: str) -> None:
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            original_op = _M2.op
            _M2.op = Operations(ctx)
            try:
                getattr(_M2, step)()
            finally:
                _M2.op = original_op


def test_m2_roundtrip_adds_corpus_schema_and_restores_previous_one() -> None:
    from sqlalchemy import inspect, text

    tmp_dir = tempfile.mkdtemp(prefix="flow_corpus_m2_")
    db_path = Path(tmp_dir) / "flow_corpus_m2.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    md = MetaData()
    Table("campaigns", md, Column("id", String, primary_key=True))
    Table(
        "attack_flows",
        md,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("origin", String, nullable=False),
        Column("content", JSON, nullable=False),
    )
    Table(
        "stage_runs",
        md,
        Column("id", Integer, primary_key=True),
        Column("chosen_flow_id", Integer, ForeignKey("attack_flows.id"), nullable=True),
    )
    md.create_all(engine)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO campaigns (id) VALUES ('c1')"))
            conn.execute(
                text(
                    "INSERT INTO attack_flows (id, campaign_id, origin, content) "
                    "VALUES (1, 'c1', 'human_curation', '{}'), (2, 'c1', 'ai_generated', '{}')"
                )
            )

        _run_m2(engine, "upgrade")

        insp = inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("attack_flows")}
        for name in ("source_key", "source_sha", "scope", "description", "has_cycles", "provenance"):
            assert name in cols
        assert cols["campaign_id"]["nullable"] is True
        assert {"attack_flow_objects", "attack_flow_edges", "ingest_source_state"} <= set(
            insp.get_table_names()
        )
        with engine.begin() as conn:
            states = conn.execute(
                text("SELECT source, status FROM ingest_source_state ORDER BY source")
            ).all()
            assert [tuple(r) for r in states] == [
                ("atomic", "pending"),
                ("attack_flow", "pending"),
                ("docker_catalog", "pending"),
            ]
            ins = (
                "INSERT INTO attack_flows (id, campaign_id, origin, content, source_key) "
                "VALUES (:i, NULL, 'mitre_corpus', '{}', :k)"
            )
            conn.execute(text(ins), {"i": 10, "k": "a.afb"})
            conn.execute(
                text(
                    "INSERT INTO attack_flow_objects (flow_id, stix_id, type) "
                    "VALUES (10, 'attack-action--1', 'attack-action')"
                )
            )
        # Partial unique index rejects a duplicate source_key.
        try:
            with engine.begin() as conn:
                conn.execute(text(ins), {"i": 11, "k": "a.afb"})
        except Exception:
            pass
        else:
            raise AssertionError("duplicate source_key was accepted")
        # Origin CHECK rejects unknown values.
        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE attack_flows SET origin = 'bogus' WHERE id = 1"))
        except Exception:
            pass
        else:
            raise AssertionError("origin CHECK accepts an unknown value")

        _run_m2(engine, "downgrade")

        insp = inspect(engine)
        assert not {"attack_flow_objects", "attack_flow_edges", "ingest_source_state"} & set(
            insp.get_table_names()
        )
        cols = {c["name"]: c for c in insp.get_columns("attack_flows")}
        assert set(cols) == {"id", "campaign_id", "origin", "content"}
        assert cols["campaign_id"]["nullable"] is False
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, origin FROM attack_flows ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [(1, "human_curation"), (2, "ai_generated")]
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ===========================================================================
# Migration M3 ``a3b4c5d6e7f8`` (agentic-only-flow-redesign tasks 18.1 and 18.5).
# Also covers ``campaigns.dag_file`` <-> ``flow_file``.
# Round trip: ``agent_stage_outputs.dag_id`` <-> ``flow_id`` keeping the FK to
# ``attack_flows.id``, the CHECK constraints and every row.
# ===========================================================================

_M3 = _load_migration(
    "_migration_a3b4c5d6e7f8",
    "20261030_0100-a3b4c5d6e7f8_rename_dag_columns_to_flow.py",
)


def _run_m3(engine: Engine, step: str) -> None:
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            original_op = _M3.op
            _M3.op = Operations(ctx)
            try:
                getattr(_M3, step)()
            finally:
                _M3.op = original_op


def test_m3_roundtrip_renames_stage_output_flow_column_and_keeps_fk_and_rows() -> None:
    from sqlalchemy import CheckConstraint, inspect, text

    tmp_dir = tempfile.mkdtemp(prefix="flow_stage_output_m3_")
    db_path = Path(tmp_dir) / "flow_stage_output_m3.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    md = MetaData()
    Table("attack_flows", md, Column("id", Integer, primary_key=True))
    Table(
        "agent_stage_outputs",
        md,
        Column("id", Integer, primary_key=True),
        Column("stage", String, nullable=False),
        Column("dag_id", Integer, ForeignKey("attack_flows.id"), nullable=True),
        CheckConstraint("stage IN ('flow', 'environment')", name="ck_agent_stage_outputs_stage"),
    )
    Table(
        "campaigns",
        md,
        Column("id", String, primary_key=True),
        Column("dag_file", String, nullable=True),
    )
    md.create_all(engine)

    def _fk_targets() -> list[tuple[list[str], str]]:
        return [
            (fk["constrained_columns"], fk["referred_table"])
            for fk in inspect(engine).get_foreign_keys("agent_stage_outputs")
        ]

    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO attack_flows (id) VALUES (1)"))
            conn.execute(
                text("INSERT INTO campaigns (id, dag_file) VALUES ('a', 'x.json'), ('b', NULL)")
            )
            conn.execute(
                text(
                    "INSERT INTO agent_stage_outputs (id, stage, dag_id) "
                    "VALUES (1, 'flow', 1), (2, 'environment', NULL)"
                )
            )

        _run_m3(engine, "upgrade")

        cols = {c["name"] for c in inspect(engine).get_columns("agent_stage_outputs")}
        assert "flow_id" in cols and "dag_id" not in cols
        assert _fk_targets() == [(["flow_id"], "attack_flows")]
        checks = [c["name"] for c in inspect(engine).get_check_constraints("agent_stage_outputs")]
        assert "ck_agent_stage_outputs_stage" in checks
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, flow_id FROM agent_stage_outputs ORDER BY id")
            ).all()
        assert [tuple(r) for r in rows] == [(1, 1), (2, None)]
        camp_cols = {c["name"] for c in inspect(engine).get_columns("campaigns")}
        assert "flow_file" in camp_cols and "dag_file" not in camp_cols
        with engine.connect() as conn:
            camp = conn.execute(text("SELECT id, flow_file FROM campaigns ORDER BY id")).all()
        assert [tuple(r) for r in camp] == [("a", "x.json"), ("b", None)]

        _run_m3(engine, "downgrade")

        cols = {c["name"] for c in inspect(engine).get_columns("agent_stage_outputs")}
        assert "dag_id" in cols and "flow_id" not in cols
        assert _fk_targets() == [(["dag_id"], "attack_flows")]
        checks = [c["name"] for c in inspect(engine).get_check_constraints("agent_stage_outputs")]
        assert "ck_agent_stage_outputs_stage" in checks
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, dag_id FROM agent_stage_outputs ORDER BY id")
            ).all()
        assert [tuple(r) for r in rows] == [(1, 1), (2, None)]
        camp_cols = {c["name"] for c in inspect(engine).get_columns("campaigns")}
        assert "dag_file" in camp_cols and "flow_file" not in camp_cols
        with engine.connect() as conn:
            camp = conn.execute(text("SELECT id, dag_file FROM campaigns ORDER BY id")).all()
        assert [tuple(r) for r in camp] == [("a", "x.json"), ("b", None)]
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ===========================================================================
# Migration M4 ``b4c5d6e7f8a9`` (agentic-only-flow-redesign task 21.2).
# Drops ``campaigns.ability_file`` / ``adversary_file`` / ``flow_file``;
# the downgrade re-creates them as nullable columns. No other column or row
# is touched.
# ===========================================================================

_M4 = _load_migration(
    "_migration_b4c5d6e7f8a9",
    "20261101_0100-b4c5d6e7f8a9_drop_campaign_file_columns.py",
)


def _run_m4(engine: Engine, step: str) -> None:
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            original_op = _M4.op
            _M4.op = Operations(ctx)
            try:
                getattr(_M4, step)()
            finally:
                _M4.op = original_op


def test_m4_roundtrip_drops_campaign_file_columns_and_recreates_them_nullable() -> None:
    from sqlalchemy import inspect, text

    tmp_dir = tempfile.mkdtemp(prefix="drop_campaign_files_m4_")
    db_path = Path(tmp_dir) / "drop_campaign_files_m4.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    md = MetaData()
    Table(
        "campaigns",
        md,
        Column("id", String, primary_key=True),
        Column("name", String, nullable=False),
        Column("curated", Integer, nullable=False, server_default="0"),
        Column("ability_file", String, nullable=True),
        Column("adversary_file", String, nullable=True),
        Column("flow_file", String, nullable=True),
    )
    md.create_all(engine)

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO campaigns (id, name, curated, ability_file, adversary_file, "
                    "flow_file) VALUES ('a', 'A', 1, 'x.json', 'y.json', 'z.json'), "
                    "('b', 'B', 0, NULL, NULL, NULL)"
                )
            )

        _run_m4(engine, "upgrade")

        cols = {c["name"] for c in inspect(engine).get_columns("campaigns")}
        assert cols == {"id", "name", "curated"}
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, name, curated FROM campaigns ORDER BY id")
            ).all()
        assert [tuple(r) for r in rows] == [("a", "A", 1), ("b", "B", 0)]

        _run_m4(engine, "downgrade")

        by_name = {c["name"]: c for c in inspect(engine).get_columns("campaigns")}
        assert set(by_name) == {
            "id", "name", "curated", "ability_file", "adversary_file", "flow_file",
        }
        for column in ("ability_file", "adversary_file", "flow_file"):
            assert by_name[column]["nullable"] is True
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, name, curated, ability_file FROM campaigns ORDER BY id")
            ).all()
        # The dropped path values are not recoverable; the rest of the row is intact.
        assert [tuple(r) for r in rows] == [("a", "A", 1, None), ("b", "B", 0, None)]
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ===========================================================================
# Migration M5 ``c5d6e7f8a9b0`` (agentic-only-flow-redesign task 21.3).
# Drops ``stix_packages.source_file``, widens the ingest source CHECK with
# ``stix_packages`` and seeds its ``pending`` row; the downgrade reverts all of
# it. No other row is touched.
# ===========================================================================

_M5 = _load_migration(
    "_migration_c5d6e7f8a9b0",
    "20261102_0100-c5d6e7f8a9b0_stix_packages_ingest_source.py",
)


def _run_m5(engine: Engine, step: str) -> None:
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            original_op = _M5.op
            _M5.op = Operations(ctx)
            try:
                getattr(_M5, step)()
            finally:
                _M5.op = original_op


def test_m5_roundtrip_adds_stix_packages_source_and_drops_source_file() -> None:
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import IntegrityError

    tmp_dir = tempfile.mkdtemp(prefix="stix_ingest_source_m5_")
    db_path = Path(tmp_dir) / "stix_ingest_source_m5.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    md = MetaData()
    Table(
        "stix_packages",
        md,
        Column("id", Integer, primary_key=True),
        Column("version", String, nullable=False, unique=True),
        Column("name", String, nullable=False),
        Column("source_file", String, nullable=True),
    )
    Table(
        "ingest_source_state",
        md,
        Column("source", String, primary_key=True),
        Column("status", String, nullable=False),
        CheckConstraint(
            "source IN ('atomic', 'docker_catalog', 'attack_flow')",
            name="ck_ingest_source_state_source",
        ),
    )
    md.create_all(engine)

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO stix_packages (id, version, name, source_file) VALUES "
                    "(1, '18.0', 'v18', '/data/stix/enterprise-attack-18.0.json')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO ingest_source_state (source, status) VALUES "
                    "('atomic', 'ready'), ('docker_catalog', 'pending'), ('attack_flow', 'failed')"
                )
            )

        _run_m5(engine, "upgrade")

        assert {c["name"] for c in inspect(engine).get_columns("stix_packages")} == {
            "id", "version", "name",
        }
        with engine.connect() as conn:
            assert [tuple(r) for r in conn.execute(text("SELECT id, version, name FROM stix_packages"))] == [
                (1, "18.0", "v18")
            ]
            states = dict(conn.execute(text("SELECT source, status FROM ingest_source_state")).all())
        assert states == {
            "atomic": "ready",
            "docker_catalog": "pending",
            "attack_flow": "failed",
            "stix_packages": "pending",
        }

        _run_m5(engine, "downgrade")

        by_name = {c["name"]: c for c in inspect(engine).get_columns("stix_packages")}
        assert set(by_name) == {"id", "version", "name", "source_file"}
        assert by_name["source_file"]["nullable"] is True
        with engine.connect() as conn:
            states = dict(conn.execute(text("SELECT source, status FROM ingest_source_state")).all())
        assert states == {"atomic": "ready", "docker_catalog": "pending", "attack_flow": "failed"}
        # The previous CHECK is back: the new source is rejected again.
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO ingest_source_state (source, status) VALUES ('stix_packages', 'pending')")
                )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass

# ===========================================================================
# Migration M6 ``d6e7f8a9b0c1`` (agentic-only-flow-redesign task 24.2).
# Widens the ingest source CHECK with ``curated_seed`` and seeds its ``pending``
# row; the downgrade removes the row and restores the previous CHECK.
# ===========================================================================
_M6 = _load_migration(
    "_migration_d6e7f8a9b0c1",
    "20261103_0100-d6e7f8a9b0c1_add_curated_seed_ingest_source.py",
)


def _run_m6(engine: Engine, step: str) -> None:
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            original_op = _M6.op
            _M6.op = Operations(ctx)
            try:
                getattr(_M6, step)()
            finally:
                _M6.op = original_op


def test_m6_roundtrip_adds_curated_seed_source_and_restores_previous_check() -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    tmp_dir = tempfile.mkdtemp(prefix="curated_seed_source_m6_")
    db_path = Path(tmp_dir) / "curated_seed_source_m6.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    md = MetaData()
    Table(
        "ingest_source_state",
        md,
        Column("source", String, primary_key=True),
        Column("status", String, nullable=False),
        CheckConstraint(
            "source IN ('atomic', 'docker_catalog', 'attack_flow', 'stix_packages')",
            name="ck_ingest_source_state_source",
        ),
    )
    md.create_all(engine)
    before = {
        "atomic": "ready",
        "docker_catalog": "pending",
        "attack_flow": "failed",
        "stix_packages": "pending",
    }
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO ingest_source_state (source, status) VALUES (:s, :t)"),
                [{"s": s, "t": t} for s, t in before.items()],
            )
        _run_m6(engine, "upgrade")
        with engine.connect() as conn:
            states = dict(conn.execute(text("SELECT source, status FROM ingest_source_state")).all())
        assert states == {**before, "curated_seed": "pending"}
        _run_m6(engine, "downgrade")
        with engine.connect() as conn:
            states = dict(conn.execute(text("SELECT source, status FROM ingest_source_state")).all())
        assert states == before
        # The previous CHECK is back: the new source is rejected again.
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO ingest_source_state (source, status) VALUES ('curated_seed', 'pending')")
                )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass

# ===========================================================================
# Migration M7 ``e7f8a9b0c1d2`` (agentic-only-flow-redesign task 30.3).
# Drops the dead columns ``stage_runs.chosen_flow_id``,
# ``stix_versions.curated_artifacts_path`` and ``technique_emulability.rationale``
# plus the empty ``provisioned_environment`` table; the downgrade re-creates all
# of them. Rows of the surviving tables are preserved.
# ===========================================================================

_M7 = _load_migration(
    "_migration_e7f8a9b0c1d2",
    "20261104_0100-e7f8a9b0c1d2_drop_dead_columns_and_tables.py",
)


def _run_m7(engine: Engine, step: str) -> None:
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            original_op = _M7.op
            _M7.op = Operations(ctx)
            try:
                getattr(_M7, step)()
            finally:
                _M7.op = original_op


def _build_pre_m7_engine(db_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    md = MetaData()
    Table("campaigns", md, Column("id", String, primary_key=True))
    Table("attack_flows", md, Column("id", Integer, primary_key=True))
    Table(
        "stage_runs",
        md,
        Column("id", Integer, primary_key=True),
        Column("stage", Integer, nullable=False),
        Column(
            "chosen_flow_id",
            Integer,
            ForeignKey("attack_flows.id", name="fk_stage_runs_chosen_flow_id_attack_flows"),
            nullable=True,
        ),
    )
    Table(
        "stix_versions",
        md,
        Column("version", String, primary_key=True),
        Column("curated", Boolean, nullable=False),
        Column("curated_artifacts_path", String, nullable=True),
    )
    Table(
        "technique_emulability",
        md,
        Column("technique_id", String, primary_key=True),
        Column("emulability", String, nullable=False),
        Column("rationale", Text, nullable=True),
    )
    Table(
        "provisioned_environment",
        md,
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, ForeignKey("campaigns.id"), nullable=False),
        Column("platform", String(15), nullable=False),
        Column("content", JSON, nullable=False),
        Column("origin", String(14), nullable=False),
        Column("ready", Boolean, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )
    md.create_all(engine)
    return engine


def test_m7_roundtrip_drops_dead_columns_and_table_and_recreates_them() -> None:
    from sqlalchemy import inspect, text

    tmp_dir = tempfile.mkdtemp(prefix="drop_dead_fields_m7_")
    db_path = Path(tmp_dir) / "drop_dead_fields_m7.db"
    engine = _build_pre_m7_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO campaigns (id) VALUES ('c1')"))
            conn.execute(text("INSERT INTO attack_flows (id) VALUES (1)"))
            conn.execute(
                text("INSERT INTO stage_runs (id, stage, chosen_flow_id) VALUES (1, 1, 1), (2, 2, NULL)")
            )
            conn.execute(
                text("INSERT INTO stix_versions (version, curated, curated_artifacts_path) VALUES ('18.0', 1, NULL)")
            )
            conn.execute(
                text(
                    "INSERT INTO technique_emulability (technique_id, emulability, rationale) "
                    "VALUES ('T1059', 'container', 'note'), ('T1003', 'not_container', NULL)"
                )
            )

        _run_m7(engine, "upgrade")

        insp = inspect(engine)
        assert "provisioned_environment" not in insp.get_table_names()
        assert {c["name"] for c in insp.get_columns("stage_runs")} == {"id", "stage"}
        assert {c["name"] for c in insp.get_columns("stix_versions")} == {"version", "curated"}
        assert {c["name"] for c in insp.get_columns("technique_emulability")} == {
            "technique_id", "emulability",
        }
        with engine.connect() as conn:
            assert [tuple(r) for r in conn.execute(text("SELECT id, stage FROM stage_runs ORDER BY id"))] == [
                (1, 1), (2, 2),
            ]
            assert [tuple(r) for r in conn.execute(text("SELECT version, curated FROM stix_versions"))] == [
                ("18.0", 1),
            ]
            assert [
                tuple(r)
                for r in conn.execute(
                    text("SELECT technique_id, emulability FROM technique_emulability ORDER BY technique_id")
                )
            ] == [("T1003", "not_container"), ("T1059", "container")]

        _run_m7(engine, "downgrade")

        insp = inspect(engine)
        by_name = {c["name"]: c for c in insp.get_columns("stage_runs")}
        assert set(by_name) == {"id", "stage", "chosen_flow_id"}
        assert by_name["chosen_flow_id"]["nullable"] is True
        fks = insp.get_foreign_keys("stage_runs")
        assert [(fk["constrained_columns"], fk["referred_table"]) for fk in fks] == [
            (["chosen_flow_id"], "attack_flows"),
        ]
        assert {c["name"]: c["nullable"] for c in insp.get_columns("stix_versions")} == {
            "version": False, "curated": False, "curated_artifacts_path": True,
        }
        assert {c["name"]: c["nullable"] for c in insp.get_columns("technique_emulability")} == {
            "technique_id": False, "emulability": False, "rationale": True,
        }
        env_cols = {c["name"]: c for c in insp.get_columns("provisioned_environment")}
        assert set(env_cols) == {
            "id", "campaign_id", "platform", "content", "origin", "ready",
            "created_at", "updated_at",
        }
        assert all(c["nullable"] is False for c in env_cols.values())
        assert insp.get_pk_constraint("provisioned_environment")["constrained_columns"] == ["id"]
        assert [
            (fk["constrained_columns"], fk["referred_table"])
            for fk in insp.get_foreign_keys("provisioned_environment")
        ] == [(["campaign_id"], "campaigns")]
        with engine.connect() as conn:
            assert [tuple(r) for r in conn.execute(text("SELECT id, stage FROM stage_runs ORDER BY id"))] == [
                (1, 1), (2, 2),
            ]
            assert conn.execute(text("SELECT count(*) FROM technique_emulability")).scalar() == 2
            assert conn.execute(text("SELECT count(*) FROM stix_versions")).scalar() == 1
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def test_m7_upgrade_refuses_to_drop_a_non_empty_provisioned_environment() -> None:
    from sqlalchemy import text

    tmp_dir = tempfile.mkdtemp(prefix="drop_dead_fields_m7_guard_")
    db_path = Path(tmp_dir) / "drop_dead_fields_m7_guard.db"
    engine = _build_pre_m7_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO campaigns (id) VALUES ('c1')"))
            conn.execute(
                text(
                    "INSERT INTO provisioned_environment (id, campaign_id, platform, content, origin, "
                    "ready, created_at, updated_at) VALUES (1, 'c1', 'container', '{}', "
                    "'human_curation', 1, '2024-01-01 00:00:00', '2024-01-01 00:00:00')"
                )
            )
        with pytest.raises(RuntimeError, match="provisioned_environment"):
            _run_m7(engine, "upgrade")
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ===========================================================================
# Migration M8 ``f8a9b0c1d2e3`` (agentic-only-flow-redesign task 32.1).
# Drops the dead ``stix_versions`` table; the downgrade re-creates it as it was
# after M7 and re-inserts the seeded ``18.0`` row.
# ===========================================================================

_M8 = _load_migration(
    "_migration_f8a9b0c1d2e3",
    "20261104_0200-f8a9b0c1d2e3_drop_dead_stix_versions_table.py",
)


def _run_m8(engine: Engine, step: str) -> None:
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn, opts={"render_as_batch": True})
            original_op = _M8.op
            _M8.op = Operations(ctx)
            try:
                getattr(_M8, step)()
            finally:
                _M8.op = original_op


def _build_pre_m8_engine(db_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    md = MetaData()
    Table(
        "stix_versions",
        md,
        Column("version", String, primary_key=True),
        Column("curated", Boolean, nullable=False),
    )
    md.create_all(engine)
    return engine


def test_m8_roundtrip_drops_stix_versions_and_restores_it_with_the_seed_row() -> None:
    from sqlalchemy import inspect, text

    tmp_dir = tempfile.mkdtemp(prefix="drop_stix_versions_m8_")
    db_path = Path(tmp_dir) / "drop_stix_versions_m8.db"
    engine = _build_pre_m8_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO stix_versions (version, curated) VALUES ('18.0', 1)"))

        _run_m8(engine, "upgrade")
        assert "stix_versions" not in inspect(engine).get_table_names()

        _run_m8(engine, "downgrade")
        insp = inspect(engine)
        assert {c["name"]: c["nullable"] for c in insp.get_columns("stix_versions")} == {
            "version": False, "curated": False,
        }
        assert insp.get_pk_constraint("stix_versions")["constrained_columns"] == ["version"]
        with engine.connect() as conn:
            assert [
                tuple(r) for r in conn.execute(text("SELECT version, curated FROM stix_versions"))
            ] == [("18.0", 1)]

        _run_m8(engine, "upgrade")
        assert "stix_versions" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def test_m8_upgrade_refuses_to_drop_a_table_with_unexpected_rows() -> None:
    from sqlalchemy import inspect, text

    tmp_dir = tempfile.mkdtemp(prefix="drop_stix_versions_m8_guard_")
    db_path = Path(tmp_dir) / "drop_stix_versions_m8_guard.db"
    engine = _build_pre_m8_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO stix_versions (version, curated) VALUES ('17.0', 0)"))
        with pytest.raises(RuntimeError, match="stix_versions"):
            _run_m8(engine, "upgrade")
        assert "stix_versions" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
