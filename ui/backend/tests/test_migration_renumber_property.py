"""Property-based test for the ``upgrade()`` renumbering of migration
``c348c9e36422`` (task 2.2 of the ``dag-estagio-multi-origem`` spec).

# Feature: dag-estagio-multi-origem, Property 4: Renumeração do upgrade desloca
# e preserva. Para qualquer conjunto de linhas de ``execucoes_estagio`` com
# ``estagio`` em 1..3, aplicar o ``upgrade()`` da migração ``c348c9e36422``
# desloca cada ``estagio`` em uma unidade (1→2, 2→3, 3→4) preservando ``estado``,
# ``progresso``, ``mensagem_erro``, ``etapa_falha`` e ``caso_id`` de cada linha.

Validates: Requirements 2.1, 2.2, 2.3, 8.3

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
rows, and drives the migration's REAL :func:`upgrade` logic through Alembic's
own :class:`~alembic.operations.Operations` proxy bound to that isolated engine
(``op.get_bind()`` and the batch/create-table operations all resolve against the
temp SQLite connection, never the project DB). The temp database is disposed and
deleted after every example, so there is no state leak and no shared-DB write.

Driving the real migration keeps the test honest: it exercises the migration's
own guard + decreasing-order ``UPDATE`` sequence (3→4, 2→3, 1→2) rather than a
reimplementation, so a regression in the shipped ``upgrade()`` is caught here.

How the property is driven
--------------------------
For each example Hypothesis generates an arbitrary set of ``execucoes_estagio``
rows, each with:

* ``estagio`` in 1..3 (the only values the guard admits);
* a ``caso_id`` drawn from a small pool of seeded ``casos`` (FK must resolve) or
  ``None`` (the column is nullable);
* an arbitrary ``estado`` (one of the ``stage_state`` enum values), ``progresso``
  in 0..100, and nullable ``mensagem_erro`` / ``etapa_falha``.

The rows are inserted with explicit ``id`` values so each pre-migration row can
be matched one-to-one with its post-migration counterpart. After driving the
real ``upgrade()``, we read every row back and assert, per row id:

* ``estagio_depois == estagio_antes + 1`` (1→2, 2→3, 3→4 — Req. 2.1, 2.3);
* ``estado``, ``progresso``, ``mensagem_erro``, ``etapa_falha`` and ``caso_id``
  are byte-for-byte unchanged (Req. 2.2);

and globally that the multiset of resulting ``estagio`` values is exactly the
multiset of originals shifted by +1 (no row lost, duplicated or collided — the
decreasing-order shift of Req. 2.3). At least 100 iterations run
(``max_examples=150``). Everything is in-process — NO Docker, NO network, NO
shared DB — safe on any dev environment.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

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
    tmp_dir = tempfile.mkdtemp(prefix="dag_mig_renumber_")
    db_path = Path(tmp_dir) / "renumber.db"
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
# ---------------------------------------------------------------------------

_row_strategy = st.fixed_dictionaries(
    {
        "estagio": st.integers(min_value=1, max_value=3),
        "caso_id": st.one_of(st.none(), st.sampled_from(_CASE_IDS)),
        "estado": st.sampled_from(_STAGE_STATES),
        "progresso": st.integers(min_value=0, max_value=100),
        "mensagem_erro": st.one_of(st.none(), st.text(max_size=40)),
        "etapa_falha": st.one_of(st.none(), st.text(max_size=20)),
    }
)

# A list of rows (possibly empty — renumbering nothing is a valid no-op). ids
# are assigned by position so each row is uniquely identifiable across upgrade.
_rows_strategy = st.lists(_row_strategy, min_size=0, max_size=12)


# ===========================================================================
# Property 4 (task 2.2)
# ===========================================================================


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(rows=_rows_strategy)
def test_property4_upgrade_renumber_shifts_and_preserves(rows: list[dict]) -> None:
    """``upgrade()`` shifts estagio by +1 (1→2/2→3/3→4) preserving all else.

    # Feature: dag-estagio-multi-origem, Property 4: Renumeração do upgrade
    # desloca e preserva.
    Validates: Requirements 2.1, 2.2, 2.3, 8.3
    """
    engine, db_path, execucoes = _build_pre_migration_engine()
    try:
        # Seed the arbitrary rows with explicit ids so each pre-migration row
        # can be matched one-to-one with its post-migration counterpart.
        seeded: dict[int, dict] = {}
        with engine.begin() as conn:
            for i, row in enumerate(rows, start=1):
                payload = {
                    "id": i,
                    "estagio": row["estagio"],
                    "caso_id": row["caso_id"],
                    "estado": row["estado"],
                    "progresso": row["progresso"],
                    "mensagem_erro": row["mensagem_erro"],
                    "etapa_falha": row["etapa_falha"],
                    "atualizado_em": None,
                }
                conn.execute(insert(execucoes), payload)
                seeded[i] = payload

        # Independent oracle: the expected post-upgrade row per id — only
        # ``estagio`` shifts by +1; every other field is untouched (Req. 2.2).
        expected: dict[int, dict] = {
            rid: {**payload, "estagio": payload["estagio"] + 1}
            for rid, payload in seeded.items()
        }

        # Drive the migration's REAL upgrade() through Alembic's Operations proxy
        # bound to the ISOLATED temp engine. op.get_bind() / batch_alter_table /
        # create_table all resolve against this connection, never the project DB.
        #
        # The migration module does ``from alembic import op`` (the global proxy).
        # We temporarily rebind ``_migration.op`` to an Operations instance tied
        # to our isolated MigrationContext so every ``op.*`` call in upgrade()
        # resolves against the temp connection (the global proxy would otherwise
        # have no bound context outside an alembic run).
        with engine.connect() as conn:
            with conn.begin():
                ctx = MigrationContext.configure(connection=conn)
                op_proxy = Operations(ctx)
                original_op = _migration.op
                _migration.op = op_proxy
                try:
                    _migration.upgrade()
                finally:
                    _migration.op = original_op

        # Read every row back and compare to the independent oracle, per id.
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

        got: dict[int, dict] = {
            r.id: {
                "id": r.id,
                "estagio": r.estagio,
                "caso_id": r.caso_id,
                "estado": r.estado,
                "progresso": r.progresso,
                "mensagem_erro": r.mensagem_erro,
                "etapa_falha": r.etapa_falha,
            }
            for r in result
        }

        # Same set of rows survives (none lost, none duplicated).
        assert set(got.keys()) == set(expected.keys())

        for rid, exp in expected.items():
            actual = got[rid]
            # estagio shifted by exactly +1 (Req. 2.1, 2.3).
            assert actual["estagio"] == exp["estagio"]
            assert actual["estagio"] == seeded[rid]["estagio"] + 1
            assert actual["estagio"] in (2, 3, 4)
            # Every other attribute preserved (Req. 2.2).
            assert actual["caso_id"] == exp["caso_id"]
            assert actual["estado"] == exp["estado"]
            assert actual["progresso"] == exp["progresso"]
            assert actual["mensagem_erro"] == exp["mensagem_erro"]
            assert actual["etapa_falha"] == exp["etapa_falha"]

        # Global multiset check: the resulting estagio values are exactly the
        # originals shifted by +1 — the decreasing-order shift collided nothing
        # (Req. 2.3). Compare sorted multisets.
        before = sorted(p["estagio"] for p in seeded.values())
        after = sorted(a["estagio"] for a in got.values())
        assert after == [e + 1 for e in before]
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
