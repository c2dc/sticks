"""Property-based test for the ``upgrade()`` ∘ ``downgrade()`` round-trip of
migration ``1cc7b8b24bd2`` (task 6.3 of the ``pipeline-cinco-estagios`` spec).

# Feature: pipeline-cinco-estagios, Property 5: Round-trip up∘down da migração
# é identidade. Para qualquer conjunto válido de linhas de ``execucoes_estagio``
# (``estagio`` em 1..4, o intervalo do esquema anterior), aplicar ``upgrade()``
# seguido de ``downgrade()`` da migração ``1cc7b8b24bd2`` restaura exatamente o
# esquema de 1..4 sobre os dados remapeáveis — ou seja, as linhas de
# Provisionamento (NEW estagio=4) criadas pelo ``upgrade()`` são removidas e o
# remapeamento (NEW 1→OLD 2, NEW 2→OLD 1, NEW 3→OLD 3, NEW 5→OLD 4) é totalmente
# invertido, deixando as linhas originais byte-a-byte idênticas.

Validates: Requirements 7.4

This is a normal ``feature`` property test (``pipeline-cinco-estagios`` is a
feature spec): the property is expected to HOLD on the migration as written.

Isolation (CRITICAL)
--------------------
Tasks 6.2/6.3/6.4 all drive the SAME migration ``1cc7b8b24bd2``. To avoid any
contention on the shared project PostgreSQL database, this test NEVER runs
``alembic upgrade head`` against it. Instead each Hypothesis example builds its
own **ephemeral, in-process** database: a fresh temp-file SQLite engine with the
*pre-migration* schema (``casos`` + ``execucoes_estagio`` with ``estagio`` in
the OLD 1..4 range) created directly, seeds arbitrary ``execucoes_estagio``
rows, and drives the migration's REAL :func:`upgrade` / :func:`downgrade` logic
through Alembic's own :class:`~alembic.operations.Operations` proxy bound to that
isolated engine (``op.get_bind()`` resolves against the temp SQLite connection,
never the project DB). The temp database is disposed and deleted after every
example, so there is no state leak and no shared-DB write. This mirrors the
sibling round-trip test (``test_migration_roundtrip_property.py``) of the prior
``dag-estagio-multi-origem`` spec, adapted for the 1..4 → 1..5 renumbering.

Driving the real migration keeps the test honest: it exercises the migration's
own temporary-offset remap (OLD 1→2, 2→1, 3→3, 4→5), the derived creation of
the Provisionamento row (NEW estagio=4) for every case whose OLD estagio=4
(Emulação) was ``concluido``, and the real inverse downgrade (delete NEW 4 then
invert the remap) rather than a reimplementation, so a regression in the shipped
``upgrade()``/``downgrade()`` is caught here.

How the property is driven
--------------------------
For each example Hypothesis generates an arbitrary set of ``execucoes_estagio``
rows, each with:

* ``estagio`` in 1..4 (the only values the guard admits), biased so some rows
  land on estagio=4 with ``estado='concluido'`` (the case that creates
  Provisionamento rows on upgrade — the interesting branch for the round-trip);
* a ``caso_id`` drawn from a small pool of seeded ``casos`` (FK must resolve) or
  ``None`` (the column is nullable);
* an arbitrary ``estado`` (one of the ``stage_state`` enum values), ``progresso``
  in 0..100, and nullable ``mensagem_erro`` / ``etapa_falha``.

The rows are inserted with explicit ``id`` values so each original row can be
matched one-to-one with its post-round-trip counterpart. The property asserts:

1. **Sanity — the upgrade did real work.** After ``upgrade()`` alone every
   original row's ``estagio`` equals the migration's remap of its original
   value, and whenever any original row had ``estagio`` in {1, 2, 4} (i.e. a
   value the remap moves) at least one row's ``estagio`` actually changed — so
   the downgrade has something real to undo.
2. **Identity over data (Req. 7.4).** After ``upgrade()`` then ``downgrade()``
   the full set of ``execucoes_estagio`` rows is byte-for-byte identical to the
   original snapshot — same set of ids, same ``estagio`` back to its original
   value, and ``estado``, ``progresso``, ``mensagem_erro``, ``etapa_falha`` and
   ``caso_id`` all unchanged. This proves the Provisionamento rows created by
   the upgrade were removed and the remap was fully inverted.

At least 100 iterations run (``max_examples=150``). Everything is in-process —
NO Docker, NO network, NO shared DB — safe on any dev environment.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

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

# Import the SHIPPED migration module so we drive its REAL upgrade()/downgrade()
# logic. The filename is not a valid identifier, so load it by path via
# importlib (same convention as the sibling round-trip test, task 2.3).
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261012_0150-1cc7b8b24bd2_renumerar_estagios_1a5_e_dividir_emulacao.py"
)
_spec = importlib.util.spec_from_file_location("_migration_1cc7b8b24bd2", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

# The upgrade's final remap (OLD → NEW), reused here to compute the expected
# post-upgrade estagio for the sanity check. Imported from the migration so the
# test tracks the shipped mapping rather than hard-coding a copy.
_UPGRADE_MAP: dict[int, int] = _migration._UPGRADE_MAP


# ---------------------------------------------------------------------------
# Pre-migration schema (down_revision = c348c9e36422): casos + execucoes_estagio
# with estagio in the OLD 1..4 range. Defined with a dedicated MetaData so it
# mirrors the state the migration expects to find, independent of the ORM
# models (which already carry the post-migration 1..5 numbering).
# ---------------------------------------------------------------------------

_STAGE_STATES: tuple[str, ...] = (
    "nao_iniciado",
    "em_andamento",
    "concluido",
    "erro",
)

# A small, fixed pool of case slugs so the FK (caso_id → casos.id) resolves.
_CASE_IDS: tuple[str, ...] = tuple(f"caso-mig1a5-{i}" for i in range(4))


def _build_pre_migration_engine() -> tuple[Engine, Path, Table]:
    """Create a fresh temp-file SQLite engine with the PRE-migration schema.

    Returns ``(engine, db_path, execucoes_estagio_table)``. The caller disposes
    the engine and removes the file. ``casos`` is seeded with the pool slugs so
    the ``caso_id`` FK resolves for any generated row.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline5_mig_roundtrip_")
    db_path = Path(tmp_dir) / "roundtrip.db"
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


def _run_migration_step(engine: Engine, step: str) -> None:
    """Drive the migration's REAL ``upgrade``/``downgrade`` through Alembic's
    Operations proxy bound to the ISOLATED temp engine.

    The migration module does ``from alembic import op`` (the global proxy).
    We temporarily rebind ``_migration.op`` to an Operations instance tied to a
    MigrationContext on our temp connection so every ``op.*`` call resolves
    against the temp connection (the global proxy would otherwise have no bound
    context outside an alembic run). ``op.get_bind()`` targets the isolated
    SQLite DB, never the project PostgreSQL.
    """
    func = getattr(_migration, step)
    with engine.connect() as conn:
        with conn.begin():
            ctx = MigrationContext.configure(connection=conn)
            op_proxy = Operations(ctx)
            original_op = _migration.op
            _migration.op = op_proxy
            try:
                func()
            finally:
                _migration.op = original_op


def _read_rows(engine: Engine, execucoes: Table) -> dict[int, dict]:
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


# ---------------------------------------------------------------------------
# Generators — an arbitrary set of pre-migration execucoes_estagio rows.
# ---------------------------------------------------------------------------

# estagio biased toward 4 so some examples exercise the Provisionamento branch
# (OLD estagio=4 'concluido' → an extra NEW estagio=4 row on upgrade, which the
# downgrade must delete). All values stay within the guard-admitted 1..4 range.
_row_strategy = st.fixed_dictionaries(
    {
        "estagio": st.integers(min_value=1, max_value=4),
        "caso_id": st.one_of(st.none(), st.sampled_from(_CASE_IDS)),
        "estado": st.sampled_from(_STAGE_STATES),
        "progresso": st.integers(min_value=0, max_value=100),
        "mensagem_erro": st.one_of(st.none(), st.text(max_size=40)),
        "etapa_falha": st.one_of(st.none(), st.text(max_size=20)),
    }
)

# A list of rows (possibly empty — a round-trip over nothing is a valid no-op).
# ids are assigned by position so each row is uniquely identifiable.
_rows_strategy = st.lists(_row_strategy, min_size=0, max_size=12)


# ===========================================================================
# Property 5 (task 6.3)
# ===========================================================================


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(rows=_rows_strategy)
def test_property5_roundtrip_up_then_down_is_identity(rows: list[dict]) -> None:
    """``upgrade()`` then ``downgrade()`` restores the original 1..4 data.

    # Feature: pipeline-cinco-estagios, Property 5: Round-trip up∘down da
    # migração é identidade.
    Validates: Requirements 7.4
    """
    engine, db_path, execucoes = _build_pre_migration_engine()
    try:
        # Seed the arbitrary rows with explicit ids so each original row can be
        # matched one-to-one with its post-round-trip counterpart.
        original: dict[int, dict] = {}
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
                original[i] = {k: v for k, v in payload.items() if k != "atualizado_em"}

        # Drive the migration's REAL upgrade() on the ISOLATED temp engine.
        _run_migration_step(engine, "upgrade")

        # Sanity (upgrade did real work): every original row's estagio equals
        # the migration's remap of its original value, and if any original row
        # sat on a value the remap moves ({1, 2, 4}) at least one row changed —
        # so the downgrade has real work to undo.
        after_upgrade = _read_rows(engine, execucoes)
        for rid, orig in original.items():
            assert after_upgrade[rid]["estagio"] == _UPGRADE_MAP[orig["estagio"]]
        if any(orig["estagio"] in (1, 2, 4) for orig in original.values()):
            assert any(
                after_upgrade[rid]["estagio"] != orig["estagio"]
                for rid, orig in original.items()
            )

        # Drive the migration's REAL downgrade() on the same isolated engine.
        _run_migration_step(engine, "downgrade")

        # Identity over data (Req. 7.4): every original row is byte-for-byte
        # identical to its snapshot — same set of ids, estagio back to 1..4, and
        # every preserved attribute unchanged.
        restored = _read_rows(engine, execucoes)
        assert set(restored.keys()) == set(original.keys())
        for rid, orig in original.items():
            got = restored[rid]
            assert got["estagio"] == orig["estagio"]
            assert got["caso_id"] == orig["caso_id"]
            assert got["estado"] == orig["estado"]
            assert got["progresso"] == orig["progresso"]
            assert got["mensagem_erro"] == orig["mensagem_erro"]
            assert got["etapa_falha"] == orig["etapa_falha"]

        # Global check: the full restored row set equals the original row set —
        # no row lost, no Provisionamento row left behind, nothing duplicated or
        # mutated by the up∘down round-trip.
        assert restored == original
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
