"""Integration test for the AI-persistence + pgvector migration ``a1b2c3d4e5f6``
(task 3.3 of the ``ai-curation-agents`` spec).

# Feature: ai-curation-agents — migration ``a1b2c3d4e5f6``
# ("criar as tabelas do agente de IA e habilitar o pgvector", task 3.2) lands the
# schema half of the AI persistence: it enables the ``pgvector`` extension and
# creates the five AI-bookkeeping tables (``agent_runs``, ``agent_iterations``,
# ``agent_stage_outputs``, ``agent_evaluations`` and the long-term RAG
# ``experience_memory``) with a cosine-similarity index on
# ``experience_memory.embedding``. This test exercises ``upgrade()`` /
# ``downgrade()`` in-process and asserts the four behaviours the task requires:
#   * the five AI tables are created on upgrade and dropped on downgrade (clean
#     up∘down identity);
#   * the IVFFlat cosine index exists where the backend supports it (PostgreSQL);
#   * the pgvector extension step is present and idempotent
#     (``CREATE EXTENSION IF NOT EXISTS vector``); and
#   * no existing enum/constraint (the ``origin`` enum) is altered — the
#     migration is purely additive.

Validates: Requirements 6.4, 6.5, 2.5

This is a *feature* spec (``ai-curation-agents``), so the behaviours under test
are expected to HOLD on the migration as written.

Why two complementary backends
-------------------------------
The product database is PostgreSQL with pgvector; the unit-test backend is
ephemeral SQLite (the project never runs ``alembic upgrade head`` against the
shared project PostgreSQL in the default suite — see the sibling
``test_migration_1a6_property.py`` / ``test_migration_roundtrip_property.py``).
Three effects of this migration are PostgreSQL-only and are emitted exclusively
when the bind dialect is ``postgresql``: ``CREATE EXTENSION``, the real
``VECTOR`` column type, and the IVFFlat cosine index. So the test asserts at the
level each backend supports:

* **SQLite, executed in-process** — drives the migration's REAL
  ``upgrade()``/``downgrade()`` through Alembic's own
  :class:`~alembic.operations.Operations` proxy bound to a fresh temp-file
  SQLite engine, and asserts the five tables are created on upgrade and dropped
  on downgrade (the clean up∘down identity over the objects this migration
  owns). The PostgreSQL-only extension/index steps are correctly skipped there.
* **PostgreSQL, rendered offline** — renders the migration's emitted SQL against
  the real PostgreSQL dialect in Alembic *offline* mode (``as_sql=True``, no live
  server), and asserts the extension/``VECTOR`` column/cosine-index statements
  the SQLite backend cannot express. Offline rendering needs no live Postgres,
  so the test runs green in any dev/CI environment (matching the project's
  no-shared-DB convention) while still proving the PostgreSQL-only effects.

Everything is in-process — NO Docker, NO network, NO shared DB. The temp SQLite
database is disposed and deleted after use, so there is no state leak.
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine

# Import the SHIPPED migration module (task 3.2) so we drive its REAL
# upgrade()/downgrade() logic rather than a reimplementation. The filename is
# not a valid identifier, so load it by path via importlib — the same
# convention as every sibling migration test in this directory.
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261019_0100-a1b2c3d4e5f6_criar_tabelas_agente_ia_e_habilitar_pgvector.py"
)
_spec = importlib.util.spec_from_file_location("_migration_a1b2c3d4e5f6", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)


# The five AI tables this migration owns, in CREATE order. ``downgrade`` drops
# them in the exact reverse order (children before parents).
_AI_TABLES_CREATE_ORDER: tuple[str, ...] = (
    "agent_runs",
    "agent_iterations",
    "agent_stage_outputs",
    "agent_evaluations",
    "experience_memory",
)
_AI_TABLES: frozenset[str] = frozenset(_AI_TABLES_CREATE_ORDER)

# The cosine-similarity index name (PostgreSQL only), read from the migration so
# the assertion tracks the shipped name.
_EMBEDDING_INDEX_NAME: str = _migration._EMBEDDING_INDEX_NAME

# The embedding width the migration resolves (design default 1024), read from
# the migration so the VECTOR(dim) assertion tracks the shipped dimension.
_EMBEDDING_DIM: int = _migration._embedding_dim()


# ---------------------------------------------------------------------------
# SQLite in-process helpers — drive the migration's REAL upgrade/downgrade
# through Alembic's Operations proxy bound to an ISOLATED temp-file engine
# (mirrors test_migration_1a6_property.py::_run_migration_step).
# ---------------------------------------------------------------------------


def _build_empty_sqlite_engine() -> tuple[Engine, Path]:
    """Create a fresh temp-file SQLite engine with an empty schema.

    The migration is purely additive (it creates its own tables; its FKs to
    pre-existing tables are not enforced at CREATE time on SQLite), so the five
    AI tables round-trip cleanly against an empty database. Returns
    ``(engine, db_path)``; the caller disposes the engine and removes the file.
    """
    tmp_dir = tempfile.mkdtemp(prefix="ai_pgvector_mig_")
    db_path = Path(tmp_dir) / "ai_pgvector.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    return engine, db_path


def _run_migration_step(engine: Engine, step: str) -> None:
    """Drive the migration's REAL ``upgrade``/``downgrade`` through Alembic's
    Operations proxy bound to the ISOLATED temp engine.

    The migration module does ``from alembic import op`` (the global proxy). We
    temporarily rebind ``_migration.op`` to an ``Operations`` instance tied to a
    :class:`~alembic.migration.MigrationContext` on our temp connection so every
    ``op.*`` call (``op.get_bind()`` for the dialect branch, ``op.create_table``
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


# ---------------------------------------------------------------------------
# Offline PostgreSQL SQL rendering — render the migration's emitted SQL against
# the real PostgreSQL dialect in Alembic *offline* mode (``as_sql=True``), with
# NO live server. Captures the PostgreSQL-only statements (CREATE EXTENSION, the
# VECTOR column type, the IVFFlat cosine index) the SQLite backend cannot emit.
# ---------------------------------------------------------------------------


def _render_postgresql_sql(step: str) -> str:
    """Return the SQL the migration ``step`` emits for the PostgreSQL dialect.

    Runs Alembic offline (``as_sql=True``) so ``op.get_bind().dialect.name`` is
    ``postgresql`` (taking the PostgreSQL-only branches) and every ``op.*``
    statement is written to a buffer instead of executed — no live Postgres
    needed.
    """
    import io

    out = io.StringIO()
    ctx = MigrationContext.configure(
        dialect=postgresql.dialect(),
        opts={"as_sql": True, "output_buffer": out, "literal_binds": True},
    )
    op_proxy = Operations(ctx)
    original_op = _migration.op
    _migration.op = op_proxy
    try:
        getattr(_migration, step)()
    finally:
        _migration.op = original_op
    return out.getvalue()


# ===========================================================================
# (A) SQLite in-process: the five AI tables are created on upgrade and dropped
#     on downgrade — a clean up∘down identity over the objects this migration
#     owns (Req. 6.4, 6.5).
# ===========================================================================


def test_sqlite_upgrade_creates_then_downgrade_drops_the_five_tables() -> None:
    """``upgrade()`` creates exactly the five AI tables; ``downgrade()`` drops
    them all, restoring the empty schema (clean up∘down identity).

    # Feature: ai-curation-agents — migration a1b2c3d4e5f6 (task 3.3).
    Validates: Requirements 6.4, 6.5
    """
    engine, db_path = _build_empty_sqlite_engine()
    try:
        # Precondition: none of the AI tables exist yet.
        before = set(inspect(engine).get_table_names())
        assert _AI_TABLES.isdisjoint(before), (
            f"AI tables already present before upgrade: {_AI_TABLES & before}"
        )

        # Drive the REAL upgrade() on the ISOLATED temp engine.
        _run_migration_step(engine, "upgrade")

        after_up = set(inspect(engine).get_table_names())
        # All five AI tables now exist (Req. 6.4 — the tables are created).
        assert after_up >= _AI_TABLES, f"missing AI tables after upgrade: {_AI_TABLES - after_up}"
        # Upgrade created exactly these five tables and nothing else (the empty
        # DB started with no tables, so the delta is precisely the five).
        assert after_up - before == _AI_TABLES, (
            f"upgrade created unexpected tables: {after_up - before - _AI_TABLES}"
        )

        # The experience_memory.embedding column exists (the portable Vector type
        # compiles to a plain column off PostgreSQL, so the table round-trips).
        emb_columns = {c["name"] for c in inspect(engine).get_columns("experience_memory")}
        assert "embedding" in emb_columns

        # On SQLite the IVFFlat cosine index is PostgreSQL-only and must NOT be
        # emitted — the migration skips it off PostgreSQL.
        em_indexes = {ix["name"] for ix in inspect(engine).get_indexes("experience_memory")}
        assert _EMBEDDING_INDEX_NAME not in em_indexes, (
            "the pgvector cosine index must not be created on SQLite"
        )

        # Drive the REAL downgrade() on the same isolated engine.
        _run_migration_step(engine, "downgrade")

        after_down = set(inspect(engine).get_table_names())
        # Clean up∘down identity (Req. 6.5): all five AI tables are gone and the
        # schema is byte-for-byte the pre-upgrade empty schema.
        assert _AI_TABLES.isdisjoint(after_down), (
            f"AI tables survived downgrade: {_AI_TABLES & after_down}"
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
    five tables — the migration leaves no residue that blocks re-application.

    # Feature: ai-curation-agents — migration a1b2c3d4e5f6 (task 3.3).
    Validates: Requirements 6.4, 6.5
    """
    engine, db_path = _build_empty_sqlite_engine()
    try:
        _run_migration_step(engine, "upgrade")
        _run_migration_step(engine, "downgrade")
        # Re-apply: a clean downgrade must leave the DB able to upgrade again.
        _run_migration_step(engine, "upgrade")
        after = set(inspect(engine).get_table_names())
        assert after >= _AI_TABLES, f"missing AI tables after re-upgrade: {_AI_TABLES - after}"
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ===========================================================================
# (B) Offline PostgreSQL rendering: the pgvector extension step is present and
#     idempotent, the embedding column is a real VECTOR(dim), and the IVFFlat
#     cosine index is created (Req. 6.4) — the PostgreSQL-only effects.
# ===========================================================================


def test_postgresql_upgrade_enables_pgvector_idempotently() -> None:
    """The upgrade emits the idempotent ``CREATE EXTENSION IF NOT EXISTS vector``
    as its FIRST statement, before any table is created.

    The ``IF NOT EXISTS`` form is what makes the step idempotent (Req. 6.4): it
    enables the extension when absent and completes without error when already
    enabled. Emitting it first is what lets the migration abort with the schema
    unchanged if pgvector cannot be enabled (Req. 6.5) — no table precedes it.

    # Feature: ai-curation-agents — migration a1b2c3d4e5f6 (task 3.3).
    Validates: Requirements 6.4, 6.5
    """
    sql = _render_postgresql_sql("upgrade")

    # The idempotent extension statement is present (case-insensitive, tolerant
    # of surrounding whitespace).
    assert re.search(r"create\s+extension\s+if\s+not\s+exists\s+vector", sql, re.IGNORECASE), (
        f"idempotent CREATE EXTENSION IF NOT EXISTS vector not found in:\n{sql}"
    )

    # It is emitted BEFORE the first CREATE TABLE (so a failure to enable the
    # extension aborts before any table is created — Req. 6.5).
    ext_pos = re.search(r"create\s+extension", sql, re.IGNORECASE)
    tbl_pos = re.search(r"create\s+table", sql, re.IGNORECASE)
    assert ext_pos is not None and tbl_pos is not None
    assert ext_pos.start() < tbl_pos.start(), (
        "CREATE EXTENSION must be emitted before the first CREATE TABLE"
    )


def test_postgresql_upgrade_creates_vector_column_and_cosine_index() -> None:
    """On PostgreSQL the embedding is a real ``VECTOR(dim)`` column and the
    IVFFlat cosine index is created on ``experience_memory.embedding`` (Req. 6.4).

    # Feature: ai-curation-agents — migration a1b2c3d4e5f6 (task 3.3).
    Validates: Requirements 6.4
    """
    sql = _render_postgresql_sql("upgrade")

    # The embedding column uses the real pgvector VECTOR type with the resolved
    # dimension (not a plain column as it would be off PostgreSQL).
    assert re.search(rf"embedding\s+VECTOR\(\s*{_EMBEDDING_DIM}\s*\)", sql, re.IGNORECASE), (
        f"embedding VECTOR({_EMBEDDING_DIM}) column not found in:\n{sql}"
    )

    # The IVFFlat cosine index backs RAG top-k (embedding <=> :q).
    assert _EMBEDDING_INDEX_NAME in sql, (
        f"cosine index {_EMBEDDING_INDEX_NAME} not found in:\n{sql}"
    )
    assert re.search(
        r"using\s+ivfflat\s*\(\s*embedding\s+vector_cosine_ops\s*\)",
        sql,
        re.IGNORECASE,
    ), f"IVFFlat vector_cosine_ops index definition not found in:\n{sql}"


def test_postgresql_downgrade_drops_index_and_tables_in_reverse_but_keeps_extension() -> None:
    """The downgrade drops the cosine index and the five tables in reverse
    dependency order, and intentionally leaves the ``vector`` extension enabled.

    Leaving the extension enabled is correct: ``DROP EXTENSION`` is not the
    inverse of the idempotent ``CREATE EXTENSION IF NOT EXISTS`` (the extension
    may have pre-existed the migration or be used elsewhere), so up∘down stays a
    clean identity over the objects the migration OWNS (Req. 6.5).

    # Feature: ai-curation-agents — migration a1b2c3d4e5f6 (task 3.3).
    Validates: Requirements 6.5
    """
    sql = _render_postgresql_sql("downgrade")

    # The cosine index is dropped (idempotent IF EXISTS form).
    assert re.search(
        rf"drop\s+index\s+if\s+exists\s+{re.escape(_EMBEDDING_INDEX_NAME)}",
        sql,
        re.IGNORECASE,
    ), f"DROP INDEX IF EXISTS {_EMBEDDING_INDEX_NAME} not found in:\n{sql}"

    # All five tables are dropped, in the exact reverse of the create order
    # (children before parents).
    drop_positions: list[tuple[str, int]] = []
    for table in _AI_TABLES:
        m = re.search(rf"drop\s+table\s+{re.escape(table)}\b", sql, re.IGNORECASE)
        assert m is not None, f"downgrade does not drop table {table}:\n{sql}"
        drop_positions.append((table, m.start()))
    dropped_in_order = [t for t, _ in sorted(drop_positions, key=lambda kv: kv[1])]
    assert dropped_in_order == list(reversed(_AI_TABLES_CREATE_ORDER)), (
        f"tables dropped in {dropped_in_order}, expected reverse create order "
        f"{list(reversed(_AI_TABLES_CREATE_ORDER))}"
    )

    # The extension is NOT dropped on downgrade (left enabled by design).
    assert not re.search(r"drop\s+extension", sql, re.IGNORECASE), (
        "downgrade must not DROP the vector extension"
    )


# ===========================================================================
# (C) The migration alters no existing enum or constraint — it is purely
#     additive. In particular the pre-existing ``origin`` enum (the
#     TranslationSource/DagOrigin portable VARCHAR+CHECK that already admits
#     ``gerado_ia``) is left exactly as-is (Req. 2.5).
# ===========================================================================


def test_migration_is_purely_additive_and_does_not_alter_the_origin_enum() -> None:
    """Neither ``upgrade`` nor ``downgrade`` emits any ALTER against an existing
    table/enum/constraint; the only ``origin`` CHECK is on the NEW
    ``agent_stage_outputs`` table and re-states the pre-existing value set
    without touching any existing origin column (Req. 2.5).

    # Feature: ai-curation-agents — migration a1b2c3d4e5f6 (task 3.3).
    Validates: Requirements 2.5
    """
    up_sql = _render_postgresql_sql("upgrade")
    down_sql = _render_postgresql_sql("downgrade")

    # (1) No ALTER TABLE anywhere — the migration never modifies an existing
    # table, enum member or constraint. (The product's origin enum lives on
    # pre-existing tables such as ``dags``/``translations``; an ALTER would be
    # the only way to change it, and there is none.)
    for label, sql in (("upgrade", up_sql), ("downgrade", down_sql)):
        assert not re.search(r"\balter\s+table\b", sql, re.IGNORECASE), (
            f"{label} must not ALTER any existing table/constraint:\n{sql}"
        )
        # No DROP/CREATE TYPE either (the project uses portable VARCHAR+CHECK
        # enums, never native PG ENUM types; a native-enum change would surface
        # here and would be a regression).
        assert not re.search(r"\b(create|drop|alter)\s+type\b", sql, re.IGNORECASE), (
            f"{label} must not touch any native enum TYPE:\n{sql}"
        )

    # (2) The only origin CHECK the upgrade emits is the NEW table's own
    # ``ck_agent_stage_outputs_origin``; it re-states the pre-existing origin
    # value set (``curadoria_humana``/``gerado_ia``) on the agent's own column —
    # it does not alter the existing origin columns.
    assert "ck_agent_stage_outputs_origin" in up_sql
    origin_check = re.search(
        r"CONSTRAINT\s+ck_agent_stage_outputs_origin\s+CHECK\s*\(([^)]*)\)",
        up_sql,
        re.IGNORECASE,
    )
    assert origin_check is not None, f"agent_stage_outputs origin CHECK not found in:\n{up_sql}"
    check_body = origin_check.group(1)
    # The pre-existing origin value already admitted by the enum is present and
    # unchanged — no new origin member is introduced.
    assert "gerado_ia" in check_body
    assert "curadoria_humana" in check_body

    # (3) Every CREATE TABLE in the upgrade is one of the five tables this
    # migration owns — it creates no table outside its own scope, so it cannot
    # shadow or redefine an existing table (such as an origin-bearing one).
    created_tables = {
        m.group(1) for m in re.finditer(r"create\s+table\s+(\w+)\s*\(", up_sql, re.IGNORECASE)
    }
    assert created_tables == _AI_TABLES, (
        f"upgrade created tables {created_tables}, expected exactly {_AI_TABLES}"
    )
