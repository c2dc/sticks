"""Property-based test for the ``upgrade()`` remapping of migration
``1cc7b8b24bd2`` (task 6.2 of the ``pipeline-cinco-estagios`` spec).

# Feature: pipeline-cinco-estagios, Property 4: Remapeamento do upgrade
# preserva e traduz. Aplicar o ``upgrade()`` da migração ``1cc7b8b24bd2``
# remapeia os ``StageRun`` do esquema antigo (1..4) para o novo (1..5) com a
# inversão 1↔2 (OLD 1→NEW 2, OLD 2→NEW 1), Tradução→3 (OLD 3→NEW 3) e
# Emulação→5 (OLD 4→NEW 5), preservando ``estado``/``progresso``/
# ``mensagem_erro``/``etapa_falha``/``caso_id`` de cada linha, e cria a linha de
# Provisionamento (NEW 4) ``concluido`` quando a antiga Emulação (OLD 4) estava
# ``concluido``.

Validates: Requirements 7.2, 7.3

This is a normal ``feature`` property test (the ``pipeline-cinco-estagios`` spec
is a feature spec): the property is expected to HOLD on the migration as
written.

Isolation (CRITICAL)
--------------------
Mirrors the in-process isolation pattern of
``tests/test_migration_renumber_property.py`` (the prior
``dag-estagio-multi-origem`` migration test). The test NEVER runs
``alembic upgrade head`` against the shared project PostgreSQL database. Instead
each Hypothesis example builds its own **ephemeral, in-process** database: a
fresh temp-file SQLite engine with the *pre-migration* schema (``casos`` +
``execucoes_estagio``) created directly, seeds arbitrary ``execucoes_estagio``
rows, and drives the migration's REAL :func:`upgrade` logic through Alembic's
own :class:`~alembic.operations.Operations` proxy bound to that isolated engine.
The migration module does ``from alembic import op`` (the global proxy); we
temporarily rebind ``_migration.op`` to an ``Operations`` instance tied to our
isolated :class:`~alembic.migration.MigrationContext` so every ``op.*`` call
(here ``op.get_bind()``) resolves against the temp SQLite connection, never the
project DB. The temp database is disposed and deleted after every example, so
there is no state leak and no shared-DB write.

Driving the real migration keeps the test honest: it exercises the migration's
own guard, the temporary-offset remap sequence AND the derived
Provisionamento INSERT, rather than a reimplementation.

How the property is driven
--------------------------
For each example Hypothesis generates an arbitrary set of ``execucoes_estagio``
rows, each with:

* ``estagio`` in 1..4 (the range the OLD schema / guard admits);
* a ``caso_id`` drawn from a small pool of seeded ``casos`` (FK must resolve);
* an arbitrary ``estado`` (one of the ``stage_state`` enum values), ``progresso``
  in 0..100, and nullable ``mensagem_erro`` / ``etapa_falha``.

Rows are inserted with explicit ``id`` values so each pre-migration row can be
matched one-to-one with its post-migration counterpart. The generator is biased
to produce BOTH some OLD estagio=4 ``concluido`` rows and some OLD estagio=4
NOT-``concluido`` rows across a small pool of caso_ids, so each example exercises
the Provisionamento branch and its complement.

After driving the real ``upgrade()`` we read every row back and assert, against
an INDEPENDENT oracle (the oracle computes the expected mapping itself; it does
not trust the migration to decide it):

* every ORIGINAL row (matched by id) has ``estagio`` remapped per
  {1→2, 2→1, 3→3, 4→5}, with ``estado``/``progresso``/``mensagem_erro``/
  ``etapa_falha``/``caso_id`` byte-for-byte preserved (Req. 7.2);
* for each distinct ``caso_id`` that had an OLD estagio=4 ``concluido`` row,
  exactly one NEW estagio=4 row exists with ``estado='concluido'`` and
  ``progresso=100`` (Req. 7.3);
* no NEW estagio=4 Provisionamento row exists for any ``caso_id`` without an
  OLD-4-``concluido`` row (Req. 7.3).

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

# Import the SHIPPED migration module so we drive its REAL upgrade() logic.
# The filename is not a valid identifier, so load it by path via importlib.
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


# ---------------------------------------------------------------------------
# Pre-migration schema (down_revision = c348c9e36422): casos +
# execucoes_estagio with exactly the columns the migration reads/writes.
# Mirrors the table definition of the prior migration test.
# ---------------------------------------------------------------------------

_STAGE_STATES: tuple[str, ...] = (
    "nao_iniciado",
    "em_andamento",
    "concluido",
    "erro",
)

# A small, fixed pool of case slugs so the FK (caso_id → casos.id) resolves and
# so the oracle groups several rows per caso_id.
_CASE_IDS: tuple[str, ...] = tuple(f"caso-mig-{i}" for i in range(4))


def _build_pre_migration_engine() -> tuple[Engine, Path, Table]:
    """Create a fresh temp-file SQLite engine with the PRE-migration schema.

    Returns ``(engine, db_path, execucoes_estagio_table)``. The caller disposes
    the engine and removes the file. ``casos`` is seeded with the pool slugs so
    the ``caso_id`` FK resolves for any generated row.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline5_mig_renumber_")
    db_path = Path(tmp_dir) / "renumber_1a5.db"
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
#
# To make sure every example exercises BOTH the Provisionamento branch and its
# complement, we build the row list from three independent pools and
# concatenate them:
#   * ``_emulacao_concluida`` — OLD estagio=4 rows forced to estado='concluido';
#   * ``_emulacao_outra`` — OLD estagio=4 rows forced to a NON-'concluido'
#     estado (so a caso with only these must NOT gain a Provisionamento row);
#   * ``_outros_estagios`` — rows with estagio in 1..3 and any estado.
# All three draw caso_id from the same small pool so a single caso can mix OLD-4
# concluido and non-concluido rows (DISTINCT dedup in the migration is tested).
# ---------------------------------------------------------------------------

_caso_id_strategy = st.sampled_from(_CASE_IDS)
_progresso_strategy = st.integers(min_value=0, max_value=100)
_mensagem_strategy = st.one_of(st.none(), st.text(max_size=40))
_etapa_strategy = st.one_of(st.none(), st.text(max_size=20))

_emulacao_concluida = st.fixed_dictionaries(
    {
        "estagio": st.just(4),
        "caso_id": _caso_id_strategy,
        "estado": st.just("concluido"),
        "progresso": _progresso_strategy,
        "mensagem_erro": _mensagem_strategy,
        "etapa_falha": _etapa_strategy,
    }
)

_emulacao_outra = st.fixed_dictionaries(
    {
        "estagio": st.just(4),
        "caso_id": _caso_id_strategy,
        "estado": st.sampled_from(
            tuple(s for s in _STAGE_STATES if s != "concluido")
        ),
        "progresso": _progresso_strategy,
        "mensagem_erro": _mensagem_strategy,
        "etapa_falha": _etapa_strategy,
    }
)

_outros_estagios = st.fixed_dictionaries(
    {
        "estagio": st.integers(min_value=1, max_value=3),
        "caso_id": _caso_id_strategy,
        "estado": st.sampled_from(_STAGE_STATES),
        "progresso": _progresso_strategy,
        "mensagem_erro": _mensagem_strategy,
        "etapa_falha": _etapa_strategy,
    }
)


@st.composite
def _rows_strategy(draw: st.DrawFn) -> list[dict]:
    """Build an arbitrary set of pre-migration rows mixing all three pools.

    Each pool contributes 0..5 rows; the combined list is shuffled so the
    migration's order-independence is exercised. ``caso_id`` is never ``None``
    here (the FK is seeded) which keeps the oracle's per-caso grouping crisp;
    nullability of ``caso_id`` is covered by the prior migration test.
    """
    conclu = draw(st.lists(_emulacao_concluida, min_size=0, max_size=5))
    outra = draw(st.lists(_emulacao_outra, min_size=0, max_size=5))
    outros = draw(st.lists(_outros_estagios, min_size=0, max_size=6))
    combined = conclu + outra + outros
    return draw(st.permutations(combined))


# ===========================================================================
# Property 4 (task 6.2)
# ===========================================================================

# Independent oracle mapping — computed here, NOT read from the migration.
_EXPECTED_MAP: dict[int, int] = {1: 2, 2: 1, 3: 3, 4: 5}


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(rows=_rows_strategy())
def test_property4_upgrade_remaps_preserves_and_provisions(rows: list[dict]) -> None:
    """``upgrade()`` remaps 1→2/2→1/3→3/4→5 preserving all else and inserts a
    Provisionamento (NEW 4) ``concluido`` row per caso with OLD-4 ``concluido``.

    # Feature: pipeline-cinco-estagios, Property 4: Remapeamento do upgrade
    # preserva e traduz.
    Validates: Requirements 7.2, 7.3
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

        # ---- Independent oracle --------------------------------------------
        # (a) expected remapped row per original id — only estagio changes.
        expected_remap: dict[int, dict] = {
            rid: {**payload, "estagio": _EXPECTED_MAP[payload["estagio"]]}
            for rid, payload in seeded.items()
        }
        # (b) distinct caso_ids whose OLD estagio=4 row was 'concluido' — each
        #     must gain exactly one NEW estagio=4 concluido/100 row.
        casos_provisionar: set = {
            payload["caso_id"]
            for payload in seeded.values()
            if payload["estagio"] == 4 and payload["estado"] == "concluido"
        }

        # Drive the migration's REAL upgrade() through Alembic's Operations proxy
        # bound to the ISOLATED temp engine. op.get_bind() resolves against this
        # connection, never the project DB.
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

        # Read every row back.
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

        by_id: dict[int, dict] = {
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

        # ---- (1) Every ORIGINAL row survives and is remapped per the oracle --
        # (Req. 7.2). Original ids are preserved by the in-place UPDATE.
        for rid, exp in expected_remap.items():
            assert rid in by_id, f"linha original id={rid} desapareceu"
            actual = by_id[rid]
            assert actual["estagio"] == exp["estagio"], (
                f"id={rid}: estagio {seeded[rid]['estagio']} deveria mapear para "
                f"{exp['estagio']}, obtido {actual['estagio']}"
            )
            assert actual["caso_id"] == exp["caso_id"]
            assert actual["estado"] == exp["estado"]
            assert actual["progresso"] == exp["progresso"]
            assert actual["mensagem_erro"] == exp["mensagem_erro"]
            assert actual["etapa_falha"] == exp["etapa_falha"]

        # ---- (2) New Provisionamento rows (NEW estagio=4) --------------------
        # Rows that are NOT one of the original (remapped) rows. Since original
        # ids are 1..len(seeded) and INSERTs get fresh autoincrement ids beyond
        # that, the new rows are exactly those whose id is not an original id.
        new_rows = [
            r for rid, r in by_id.items() if rid not in seeded
        ]
        prov_rows = [r for r in new_rows if r["estagio"] == 4]

        # Every genuinely-new row is a Provisionamento row at estagio=4
        # concluido/100 (the upgrade inserts nothing else).
        assert new_rows == prov_rows, (
            "upgrade criou linha(s) que não são Provisionamento (NEW 4): "
            f"{[r for r in new_rows if r['estagio'] != 4]}"
        )

        prov_by_caso: dict = {}
        for r in prov_rows:
            assert r["estado"] == "concluido", (
                f"Provisionamento caso={r['caso_id']} deveria ser 'concluido'"
            )
            assert r["progresso"] == 100, (
                f"Provisionamento caso={r['caso_id']} deveria ter progresso=100"
            )
            prov_by_caso.setdefault(r["caso_id"], 0)
            prov_by_caso[r["caso_id"]] += 1

        # (2a) Exactly one Provisionamento row per caso that had OLD-4 concluido.
        assert set(prov_by_caso.keys()) == casos_provisionar, (
            f"casos com Provisionamento {set(prov_by_caso.keys())} != "
            f"casos esperados {casos_provisionar}"
        )
        for caso_id, count in prov_by_caso.items():
            assert count == 1, (
                f"caso={caso_id} deveria ter exatamente 1 Provisionamento, "
                f"tem {count}"
            )

        # (2b) No Provisionamento row for any caso WITHOUT an OLD-4-concluido
        # row — covered by the set equality above, asserted explicitly here.
        casos_sem_provisionamento = set(_CASE_IDS) - casos_provisionar
        for caso_id in casos_sem_provisionamento:
            assert caso_id not in prov_by_caso, (
                f"caso={caso_id} não teve OLD-4 concluido mas ganhou "
                "Provisionamento"
            )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
