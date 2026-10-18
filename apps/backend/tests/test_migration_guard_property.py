"""Property-based test for the ``upgrade()`` GUARD of migration
``c348c9e36422`` (task 2.4 of the ``dag-estagio-multi-origem`` spec).

# Feature: dag-estagio-multi-origem, Property 6: The migration guard aborts
# without altering data. For any initial population of ``execucoes_estagio``
# that contains at least one row with ``estagio`` outside 1..3, the
# ``upgrade()`` of migration ``c348c9e36422`` stops with a descriptive
# ``RuntimeError`` and the persisted state stays identical to before (no row is
# altered and no DDL — ``dags`` / ``dag_escolhido_id`` — is applied, because the
# guard runs before any UPDATE/DDL).

Validates: Requirements 2.6

This is a normal ``feature`` property test (the ``dag-estagio-multi-origem``
spec is a feature spec): the property is expected to HOLD on the migration as
written.

Isolation (CRITICAL)
--------------------
Tasks 2.2/2.3/2.4 all drive the SAME migration ``c348c9e36422``. To avoid any
contention on the shared project PostgreSQL database, this test NEVER runs
``alembic upgrade head`` against it. Instead each Hypothesis example builds its
own **ephemeral, in-process** database: a fresh temp-file SQLite engine with the
*pre-migration* schema (``casos`` + ``execucoes_estagio`` WITHOUT
``dag_escolhido_id``) created directly, seeds arbitrary ``execucoes_estagio``
rows (at least one out of range), and drives the migration's REAL
:func:`upgrade` logic through Alembic's own
:class:`~alembic.operations.Operations` proxy bound to that isolated engine
(``op.get_bind()`` resolves against the temp SQLite connection, never the
project DB). The temp database is disposed and deleted after every example, so
there is no state leak and no shared-DB write.

Driving the real migration keeps the test honest: it exercises the migration's
own guard query (``estagio NOT BETWEEN 1 AND 3``) and its abort rather than a
reimplementation, so a regression in the shipped guard is caught here.

How the property is driven
--------------------------
For each example Hypothesis generates an arbitrary set of ``execucoes_estagio``
rows. At least one row is forced to carry an ``estagio`` OUTSIDE 1..3 (0,
negative, 4, or a large value), so the guard must fire. The rest may be in or
out of range. The rows are inserted with explicit ``id`` values, and a snapshot
of the full table is taken BEFORE calling ``upgrade()``.

We then drive the real ``upgrade()`` and assert:

1. it raises :class:`RuntimeError` (the guard — Req. 2.6);
2. the table ``execucoes_estagio`` is byte-for-byte identical to the pre-upgrade
   snapshot (no ``estagio`` or other attribute altered — the guard runs before
   any ``UPDATE``);
3. neither the ``dags`` table nor the ``execucoes_estagio.dag_escolhido_id``
   column exist afterwards (the guard runs before any DDL in the shipped
   ``upgrade()``: guard → UPDATEs → ``create_table`` → ``add_column``).

At least 100 iterations run (``max_examples=150``). Everything is in-process —
NO Docker, NO network, NO shared DB — safe on any dev environment.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    insert,
    select,
)
from sqlalchemy.engine import Engine

# Import the SHIPPED migration module so we drive its REAL upgrade() logic.
# The filename is not a valid identifier, so load it by path via importlib.
import importlib.util

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261011_2143-c348c9e36422_renumerar_estagios_1a4_e_criar_esquema_.py"
)
_spec = importlib.util.spec_from_file_location("_migration_c348c9e36422", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)


# ---------------------------------------------------------------------------
# Pre-migration schema (down_revision = a28d8c1ae4aa): casos + execucoes_estagio
# WITHOUT dag_escolhido_id. Defined with a dedicated MetaData so it mirrors the
# state the migration expects to find, independent of the ORM models (which
# already carry the post-migration column).
# ---------------------------------------------------------------------------

_STAGE_STATES: tuple[str, ...] = (
    "nao_iniciado",
    "em_andamento",
    "concluido",
    "erro",
)

# A small, fixed pool of case slugs so the FK (caso_id → casos.id) resolves.
_CASE_IDS: tuple[str, ...] = tuple(f"caso-mig-{i}" for i in range(4))


def _build_pre_migration_engine() -> tuple[Engine, Path, Table]:
    """Create a fresh temp-file SQLite engine with the PRE-migration schema.

    Returns ``(engine, db_path, execucoes_estagio_table)``. The caller disposes
    the engine and removes the file. ``casos`` is seeded with the pool slugs so
    the ``caso_id`` FK resolves for any generated row.
    """
    tmp_dir = tempfile.mkdtemp(prefix="dag_mig_guard_")
    db_path = Path(tmp_dir) / "guard.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    metadata = MetaData()
    casos = Table(
        "casos",
        metadata,
        Column("id", String, primary_key=True),
    )
    execucoes_estagio = Table(
        "execucoes_estagio",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("caso_id", String, ForeignKey("casos.id"), nullable=True),
        Column("estagio", Integer, nullable=False),
        Column("estado", String, nullable=False),
        Column("progresso", Integer, nullable=False),
        Column("mensagem_erro", Text, nullable=True),
        Column("etapa_falha", String, nullable=True),
        Column("atualizado_em", DateTime, nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(insert(casos), [{"id": slug} for slug in _CASE_IDS])

    return engine, db_path, execucoes_estagio


# ---------------------------------------------------------------------------
# Generators — an arbitrary set of pre-migration execucoes_estagio rows.
# At least one row carries an estagio OUTSIDE 1..3 so the guard must fire.
# ---------------------------------------------------------------------------

# estagio values that are OUTSIDE the admissible 1..3 range: 0, negatives, 4,
# and large values — all should trip the ``estagio NOT BETWEEN 1 AND 3`` guard.
_out_of_range_estagio = st.one_of(
    st.integers(min_value=-1_000_000, max_value=0),
    st.integers(min_value=4, max_value=1_000_000),
)

# estagio values that may be anything (in OR out of range): the guard only needs
# one offending row, the rest are free. This exercises mixed populations too.
_any_estagio = st.one_of(
    st.integers(min_value=1, max_value=3),
    _out_of_range_estagio,
)


def _row(estagio_strategy: st.SearchStrategy[int]) -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "estagio": estagio_strategy,
            "caso_id": st.one_of(st.none(), st.sampled_from(_CASE_IDS)),
            "estado": st.sampled_from(_STAGE_STATES),
            "progresso": st.integers(min_value=0, max_value=100),
            "mensagem_erro": st.one_of(st.none(), st.text(max_size=40)),
            "etapa_falha": st.one_of(st.none(), st.text(max_size=20)),
        }
    )


@st.composite
def _rows_with_at_least_one_out_of_range(draw: st.DrawFn) -> list[dict]:
    """A list of rows guaranteed to contain >= 1 row with estagio outside 1..3.

    One mandatory out-of-range row is always drawn; additional rows (0..11) may
    be in or out of range. The mandatory row is inserted at an arbitrary
    position so the guard cannot rely on ordering.
    """
    offending = draw(_row(_out_of_range_estagio))
    others = draw(st.lists(_row(_any_estagio), min_size=0, max_size=11))
    position = draw(st.integers(min_value=0, max_value=len(others)))
    rows = others[:position] + [offending] + others[position:]
    return rows


# ===========================================================================
# Property 6 (task 2.4)
# ===========================================================================


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(rows=_rows_with_at_least_one_out_of_range())
def test_property6_guard_aborts_without_altering_data(rows: list[dict]) -> None:
    """``upgrade()`` aborts with RuntimeError and leaves everything untouched.

    # Feature: dag-estagio-multi-origem, Property 6: The migration guard aborts
    # without altering data.
    Validates: Requirements 2.6
    """
    engine, db_path, execucoes = _build_pre_migration_engine()
    try:
        # Seed the arbitrary rows with explicit ids so each pre-upgrade row can
        # be matched one-to-one against its post-abort state.
        with engine.begin() as conn:
            for i, row in enumerate(rows, start=1):
                conn.execute(
                    insert(execucoes),
                    {
                        "id": i,
                        "estagio": row["estagio"],
                        "caso_id": row["caso_id"],
                        "estado": row["estado"],
                        "progresso": row["progresso"],
                        "mensagem_erro": row["mensagem_erro"],
                        "etapa_falha": row["etapa_falha"],
                        "atualizado_em": None,
                    },
                )

        def _snapshot() -> dict[int, dict]:
            with engine.connect() as conn:
                result = conn.execute(
                    select(
                        execucoes.c.id,
                        execucoes.c.estagio,
                        execucoes.c.caso_id,
                        execucoes.c.estado,
                        execucoes.c.progresso,
                        execucoes.c.mensagem_erro,
                        execucoes.c.etapa_falha,
                    ).order_by(execucoes.c.id.asc())
                ).all()
            return {
                r.id: {
                    "estagio": r.estagio,
                    "caso_id": r.caso_id,
                    "estado": r.estado,
                    "progresso": r.progresso,
                    "mensagem_erro": r.mensagem_erro,
                    "etapa_falha": r.etapa_falha,
                }
                for r in result
            }

        # Snapshot of the full table BEFORE the (expected to abort) upgrade.
        before = _snapshot()

        # Sanity: the generator must have produced at least one offending row.
        assert any(
            row["estagio"] < 1 or row["estagio"] > 3 for row in rows
        ), "generator must include at least one estagio outside 1..3"

        # Drive the migration's REAL upgrade() through Alembic's Operations proxy
        # bound to the ISOLATED temp engine. The guard query runs on
        # op.get_bind() against this connection, never the project DB. We expect
        # it to raise RuntimeError before any UPDATE/DDL.
        with engine.connect() as conn:
            with conn.begin():
                ctx = MigrationContext.configure(connection=conn)
                op_proxy = Operations(ctx)
                original_op = _migration.op
                _migration.op = op_proxy
                try:
                    with pytest.raises(RuntimeError):
                        _migration.upgrade()
                finally:
                    _migration.op = original_op

        # (2) No row was altered: the table is identical to the pre-upgrade
        # snapshot (the guard ran before any UPDATE — Req. 2.6).
        after = _snapshot()
        assert after == before

        # (3) No DDL applied: the guard precedes create_table/add_column in the
        # shipped upgrade(), so neither the dags table nor the
        # dag_escolhido_id column exist after the abort.
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "dags" not in table_names
        execucoes_columns = {
            col["name"] for col in inspector.get_columns("execucoes_estagio")
        }
        assert "dag_escolhido_id" not in execucoes_columns
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
