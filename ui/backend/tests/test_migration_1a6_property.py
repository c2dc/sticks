"""Property-based test for migration ``e907e49a84c5`` (task 10.3 of the
``pipeline-seis-estagios`` spec).

# Feature: pipeline-seis-estagios, Property 11: Ampliar o range para 6 não
# quebra a derivação de estado/progresso. Aplicar o ``upgrade()`` da migração
# ``e907e49a84c5`` (que evolui o pipeline de cinco para seis Estágios)
# remapeia os ``StageRun`` do esquema antigo (1..5) para o novo (2..6) com o
# deslocamento uniforme ``n → n+1``, preservando ``estado``/``progresso``/
# ``mensagem_erro``/``etapa_falha``/``caso_id``/``dag_escolhido_id`` de cada
# linha; cria EXATAMENTE uma linha de Seleção da base STIX (NEW estagio=1,
# ``concluido``/``progresso=100``) para cada Caso cujo ANTIGO Estágio 1
# (Modelagem) estava ``concluido``; o round-trip ``up ∘ down`` é a identidade
# sobre ``execucoes_estagio``; e a guarda do ``upgrade()`` aborta com
# ``RuntimeError`` SEM alterar dados quando existe alguma linha com ``estagio``
# fora de 1..5.

Validates: Requirements 1.2, 1.5

This is a normal ``feature`` property test (``pipeline-seis-estagios`` is a
feature spec): the property is expected to HOLD on the migration as written.

Isolation (CRITICAL)
--------------------
Mirrors the in-process isolation pattern of the sibling 1a5 migration tests
(``test_migration_1a5_renumber_property.py`` /
``test_migration_1a5_roundtrip_property.py`` /
``test_migration_1a5_guard_property.py``). The test NEVER runs
``alembic upgrade head`` against the shared project PostgreSQL database. Instead
each Hypothesis example builds its own **ephemeral, in-process** database: a
fresh temp-file SQLite engine with the *pre-migration* schema (``casos`` +
``dags`` + ``execucoes_estagio`` WITH ``dag_escolhido_id``, the state left by
the previous migration ``1cc7b8b24bd2``) created directly, seeds arbitrary
``execucoes_estagio`` rows, and drives the migration's REAL :func:`upgrade` /
:func:`downgrade` logic through Alembic's own
:class:`~alembic.operations.Operations` proxy bound to that isolated engine.
The migration module does ``from alembic import op`` (the global proxy); we
temporarily rebind ``_migration.op`` to an ``Operations`` instance tied to our
isolated :class:`~alembic.migration.MigrationContext` so every ``op.*`` call
(``op.get_bind()`` for the data remap and ``op.create_table`` /
``op.drop_table`` for the three new tables) resolves against the temp SQLite
connection, never the project DB. The temp database is disposed and deleted
after every example, so there is no state leak and no shared-DB write.

Driving the real migration keeps the test honest: it exercises the migration's
own guard, the temporary-offset remap (OLD n → NEW n+1), the derived creation
of the Seleção da base STIX row, the ``CREATE TABLE`` of the three new tables
and the symmetric ``downgrade`` — rather than a reimplementation. A regression
in the shipped ``upgrade()``/``downgrade()`` is caught here.

Everything is in-process — NO Docker, NO network, NO shared DB — safe on any
dev environment. At least 100 iterations run per property (``max_examples=150``).
"""

from __future__ import annotations

import importlib.util
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

# Import the SHIPPED migration module (task 10.2) so we drive its REAL
# upgrade()/downgrade() logic. The filename is not a valid identifier, so load
# it by path via importlib (same convention as the sibling 1a5 tests).
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261013_0300-e907e49a84c5_renumerar_estagios_1a6_e_criar_esquema_seis_estagios.py"
)
_spec = importlib.util.spec_from_file_location("_migration_e907e49a84c5", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

# The upgrade's remap (OLD → NEW), imported from the migration so the sanity
# check tracks the shipped mapping. The INDEPENDENT oracle below recomputes the
# expected n→n+1 mapping itself and asserts it equals this, so a wrong shipped
# map would be caught rather than trusted.
_UPGRADE_MAP: dict[int, int] = _migration._UPGRADE_MAP


# ---------------------------------------------------------------------------
# Pre-migration schema (down_revision = 1cc7b8b24bd2): the state the migration
# e907e49a84c5 expects to find — casos + dags + execucoes_estagio WITH
# dag_escolhido_id (so the preservation of dag_escolhido_id is a real column,
# not a no-op). Defined with a dedicated MetaData, independent of the ORM
# models (which already carry the post-migration numbering/tables).
# ---------------------------------------------------------------------------

_STAGE_STATES: tuple[str, ...] = (
    "nao_iniciado",
    "em_andamento",
    "concluido",
    "erro",
)

# A small, fixed pool of case slugs so the FK (caso_id → casos.id) resolves and
# so the oracle groups several rows per caso_id.
_CASE_IDS: tuple[str, ...] = tuple(f"caso-mig1a6-{i}" for i in range(4))

# A couple of seeded dags so dag_escolhido_id can reference a real row (FK must
# resolve) — exercising non-NULL preservation of that column.
_DAG_IDS: tuple[int, ...] = (1, 2)


def _build_pre_migration_engine() -> tuple[Engine, Path, Table]:
    """Create a fresh temp-file SQLite engine with the PRE-migration schema.

    Returns ``(engine, db_path, execucoes_estagio_table)``. The caller disposes
    the engine and removes the file. ``casos`` and ``dags`` are seeded so the
    ``caso_id`` and ``dag_escolhido_id`` FKs resolve for any generated row.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline6_mig_")
    db_path = Path(tmp_dir) / "mig_1a6.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    metadata = MetaData()
    casos = Table(
        "casos",
        metadata,
        Column("id", String, primary_key=True),
    )
    dags = Table(
        "dags",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("caso_id", String, ForeignKey("casos.id"), nullable=False),
        Column("origem", String, nullable=False),
        Column("identificador", String, nullable=False),
        Column("rotulo", String, nullable=True),
        Column("conteudo", JSON, nullable=False),
        Column("criado_em", DateTime, nullable=False),
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
        Column("dag_escolhido_id", Integer, ForeignKey("dags.id"), nullable=True),
    )
    metadata.create_all(engine)

    import datetime as _dt

    with engine.begin() as conn:
        conn.execute(insert(casos), [{"id": slug} for slug in _CASE_IDS])
        conn.execute(
            insert(dags),
            [
                {
                    "id": did,
                    "caso_id": _CASE_IDS[0],
                    "origem": "curadoria_humana",
                    "identificador": f"dag-{did}",
                    "rotulo": None,
                    "conteudo": {},
                    "criado_em": _dt.datetime(2026, 1, 1),
                }
                for did in _DAG_IDS
            ],
        )

    return engine, db_path, execucoes_estagio


def _run_migration_step(engine: Engine, step: str) -> None:
    """Drive the migration's REAL ``upgrade``/``downgrade`` through Alembic's
    Operations proxy bound to the ISOLATED temp engine.

    The migration module does ``from alembic import op`` (the global proxy).
    We temporarily rebind ``_migration.op`` to an Operations instance tied to a
    MigrationContext on our temp connection so every ``op.*`` call resolves
    against the temp connection. ``op.get_bind()`` (and the ``create_table`` /
    ``drop_table`` ops) target the isolated SQLite DB, never the project
    PostgreSQL.
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
                execucoes.c.dag_escolhido_id,
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
            "dag_escolhido_id": r.dag_escolhido_id,
        }
        for r in result
    }


# ---------------------------------------------------------------------------
# Generators — an arbitrary set of VALID pre-migration execucoes_estagio rows
# (estagio in 1..5, the OLD range the guard admits).
#
# To make sure every example exercises BOTH the Seleção da base STIX branch and
# its complement, rows are built from three independent pools and concatenated:
#   * ``_modelagem_concluida`` — OLD estagio=1 rows forced to estado='concluido'
#     (a caso with such a row must gain exactly one NEW estagio=1 base row);
#   * ``_modelagem_outra`` — OLD estagio=1 rows forced to a NON-'concluido'
#     estado (a caso with only these must NOT gain a base-selection row);
#   * ``_outros_estagios`` — rows with estagio in 2..5 and any estado.
# All three draw caso_id from the same small pool so a single caso can mix OLD-1
# concluido and non-concluido rows (the DISTINCT dedup is exercised).
# ---------------------------------------------------------------------------

_caso_id_strategy = st.sampled_from(_CASE_IDS)
_progresso_strategy = st.integers(min_value=0, max_value=100)
_mensagem_strategy = st.one_of(st.none(), st.text(max_size=40))
_etapa_strategy = st.one_of(st.none(), st.text(max_size=20))
# dag_escolhido_id is nullable; when set it must reference a seeded dag so the
# FK resolves and the non-NULL preservation is meaningful.
_dag_strategy = st.one_of(st.none(), st.sampled_from(_DAG_IDS))

_modelagem_concluida = st.fixed_dictionaries(
    {
        "estagio": st.just(1),
        "caso_id": _caso_id_strategy,
        "estado": st.just("concluido"),
        "progresso": _progresso_strategy,
        "mensagem_erro": _mensagem_strategy,
        "etapa_falha": _etapa_strategy,
        "dag_escolhido_id": _dag_strategy,
    }
)

_modelagem_outra = st.fixed_dictionaries(
    {
        "estagio": st.just(1),
        "caso_id": _caso_id_strategy,
        "estado": st.sampled_from(tuple(s for s in _STAGE_STATES if s != "concluido")),
        "progresso": _progresso_strategy,
        "mensagem_erro": _mensagem_strategy,
        "etapa_falha": _etapa_strategy,
        "dag_escolhido_id": _dag_strategy,
    }
)

_outros_estagios = st.fixed_dictionaries(
    {
        "estagio": st.integers(min_value=2, max_value=5),
        "caso_id": _caso_id_strategy,
        "estado": st.sampled_from(_STAGE_STATES),
        "progresso": _progresso_strategy,
        "mensagem_erro": _mensagem_strategy,
        "etapa_falha": _etapa_strategy,
        "dag_escolhido_id": _dag_strategy,
    }
)


@st.composite
def _valid_rows_strategy(draw: st.DrawFn) -> list[dict]:
    """An arbitrary set of VALID pre-migration rows mixing all three pools.

    Each pool contributes 0..5 rows; the combined list is shuffled so the
    migration's order-independence is exercised. The combined list may be empty
    (a migration over nothing is a valid no-op).
    """
    conclu = draw(st.lists(_modelagem_concluida, min_size=0, max_size=5))
    outra = draw(st.lists(_modelagem_outra, min_size=0, max_size=5))
    outros = draw(st.lists(_outros_estagios, min_size=0, max_size=6))
    combined = conclu + outra + outros
    return draw(st.permutations(combined))


# ---------------------------------------------------------------------------
# Generators for the GUARD property — a set of rows with >= 1 estagio OUTSIDE
# 1..5 so the guard (estagio NOT BETWEEN 1 AND 5) must fire and abort.
# ---------------------------------------------------------------------------

_out_of_range_estagio = st.one_of(
    st.integers(min_value=-1_000_000, max_value=0),
    st.integers(min_value=6, max_value=1_000_000),
)
_any_estagio = st.one_of(st.integers(min_value=1, max_value=5), _out_of_range_estagio)


def _guard_row(estagio_strategy: st.SearchStrategy[int]) -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "estagio": estagio_strategy,
            "caso_id": st.one_of(st.none(), _caso_id_strategy),
            "estado": st.sampled_from(_STAGE_STATES),
            "progresso": _progresso_strategy,
            "mensagem_erro": _mensagem_strategy,
            "etapa_falha": _etapa_strategy,
            "dag_escolhido_id": _dag_strategy,
        }
    )


@st.composite
def _rows_with_at_least_one_out_of_range(draw: st.DrawFn) -> list[dict]:
    """A list guaranteed to contain >= 1 row with estagio outside 1..5.

    One mandatory out-of-range row is always drawn; additional rows (0..11) may
    be in or out of range. The mandatory row is inserted at an arbitrary
    position so the guard cannot rely on ordering.
    """
    offending = draw(_guard_row(_out_of_range_estagio))
    others = draw(st.lists(_guard_row(_any_estagio), min_size=0, max_size=11))
    position = draw(st.integers(min_value=0, max_value=len(others)))
    return others[:position] + [offending] + others[position:]


def _seed_rows(engine: Engine, execucoes: Table, rows: list[dict]) -> dict[int, dict]:
    """Insert ``rows`` with explicit ids 1..N and return the id→snapshot map
    (excluding ``atualizado_em`` which the migration never touches)."""
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
                "dag_escolhido_id": row["dag_escolhido_id"],
            }
            conn.execute(insert(execucoes), payload)
            seeded[i] = {k: v for k, v in payload.items() if k != "atualizado_em"}
    return seeded


# ===========================================================================
# Property 11 (task 10.3) — main property: remap preserves + base row created
# + up∘down identity, all over VALID 1..5 populations.
# ===========================================================================


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(rows=_valid_rows_strategy())
def test_property11_upgrade_remaps_preserves_provisions_and_roundtrips(
    rows: list[dict],
) -> None:
    """``upgrade()`` remaps n→n+1 preserving all else and creates one Seleção da
    base STIX row (NEW 1) per caso with OLD-1 ``concluido``; ``up ∘ down`` is
    the identity over ``execucoes_estagio``.

    # Feature: pipeline-seis-estagios, Property 11: Ampliar o range para 6 não
    # quebra a derivação de estado/progresso.
    Validates: Requirements 1.2, 1.5
    """
    engine, db_path, execucoes = _build_pre_migration_engine()
    try:
        seeded = _seed_rows(engine, execucoes, rows)

        # ---- Independent oracle --------------------------------------------
        # (a) expected n→n+1 remap, computed here — NOT read from the migration.
        oracle_map = {n: n + 1 for n in range(1, 6)}
        # Guard against a wrong shipped map by comparing to the oracle.
        assert _UPGRADE_MAP == oracle_map, (
            f"migração mapeia {_UPGRADE_MAP}, esperado n→n+1 {oracle_map}"
        )
        expected_remap: dict[int, dict] = {
            rid: {**payload, "estagio": oracle_map[payload["estagio"]]}
            for rid, payload in seeded.items()
        }
        # (b) distinct caso_ids whose OLD estagio=1 (Modelagem) row was
        #     'concluido' — each must gain exactly one NEW estagio=1 base row.
        casos_base: set = {
            payload["caso_id"]
            for payload in seeded.values()
            if payload["estagio"] == 1 and payload["estado"] == "concluido"
        }

        # ---- Drive the REAL upgrade() on the ISOLATED temp engine ----------
        _run_migration_step(engine, "upgrade")

        after_upgrade = _read_rows(engine, execucoes)

        # ---- (1) Every ORIGINAL row survives and is remapped n→n+1 ----------
        # (Req. 1.2), with every other attribute byte-for-byte preserved —
        # estado/progresso/mensagem_erro/etapa_falha/caso_id/dag_escolhido_id.
        for rid, exp in expected_remap.items():
            assert rid in after_upgrade, f"linha original id={rid} desapareceu"
            actual = after_upgrade[rid]
            assert actual["estagio"] == exp["estagio"], (
                f"id={rid}: estagio {seeded[rid]['estagio']} deveria mapear "
                f"para {exp['estagio']}, obtido {actual['estagio']}"
            )
            assert actual["caso_id"] == exp["caso_id"]
            assert actual["estado"] == exp["estado"]
            assert actual["progresso"] == exp["progresso"]
            assert actual["mensagem_erro"] == exp["mensagem_erro"]
            assert actual["etapa_falha"] == exp["etapa_falha"]
            assert actual["dag_escolhido_id"] == exp["dag_escolhido_id"]

        # ---- (2) New Seleção da base STIX rows (NEW estagio=1) --------------
        # Rows whose id is not an original id are the genuinely-new ones. The
        # upgrade inserts nothing but base-selection rows at estagio=1.
        new_rows = [r for rid, r in after_upgrade.items() if rid not in seeded]
        base_rows = [r for r in new_rows if r["estagio"] == 1]
        assert new_rows == base_rows, (
            "upgrade criou linha(s) que não são Seleção da base STIX (NEW 1): "
            f"{[r for r in new_rows if r['estagio'] != 1]}"
        )

        base_by_caso: dict = {}
        for r in base_rows:
            assert r["estado"] == "concluido", (
                f"Seleção da base caso={r['caso_id']} deveria ser 'concluido'"
            )
            assert r["progresso"] == 100, (
                f"Seleção da base caso={r['caso_id']} deveria ter progresso=100"
            )
            base_by_caso.setdefault(r["caso_id"], 0)
            base_by_caso[r["caso_id"]] += 1

        # (2a) Exactly one base-selection row per caso with OLD-1 'concluido'
        #      (Req. 1.5), and none for any caso without it.
        assert set(base_by_caso.keys()) == casos_base, (
            f"casos com Seleção da base {set(base_by_caso.keys())} != "
            f"casos esperados {casos_base}"
        )
        for caso_id, count in base_by_caso.items():
            assert count == 1, (
                f"caso={caso_id} deveria ter exatamente 1 Seleção da base, "
                f"tem {count}"
            )
        for caso_id in set(_CASE_IDS) - casos_base:
            assert caso_id not in base_by_caso, (
                f"caso={caso_id} não teve OLD-1 concluido mas ganhou Seleção "
                "da base"
            )

        # ---- (3) up ∘ down is identity over execucoes_estagio (Req. 1.2) ----
        _run_migration_step(engine, "downgrade")
        restored = _read_rows(engine, execucoes)
        # Same set of ids as the original (base rows removed, nothing lost).
        assert set(restored.keys()) == set(seeded.keys())
        for rid, orig in seeded.items():
            got = restored[rid]
            assert got["estagio"] == orig["estagio"]
            assert got["caso_id"] == orig["caso_id"]
            assert got["estado"] == orig["estado"]
            assert got["progresso"] == orig["progresso"]
            assert got["mensagem_erro"] == orig["mensagem_erro"]
            assert got["etapa_falha"] == orig["etapa_falha"]
            assert got["dag_escolhido_id"] == orig["dag_escolhido_id"]
        # Global: the full restored set equals the original — no row lost, no
        # base-selection row left behind, nothing duplicated or mutated.
        assert restored == seeded
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ===========================================================================
# Property 11 (task 10.3) — guard clause: upgrade aborts without mutating when
# any estagio is outside 1..5.
# ===========================================================================


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(rows=_rows_with_at_least_one_out_of_range())
def test_property11_guard_aborts_without_altering_data(rows: list[dict]) -> None:
    """``upgrade()`` aborts with RuntimeError and leaves every row untouched
    (no remap, no Seleção da base row, no new table) when any ``estagio`` is
    outside 1..5.

    # Feature: pipeline-seis-estagios, Property 11: Ampliar o range para 6 não
    # quebra a derivação de estado/progresso (guarda).
    Validates: Requirements 1.2, 1.5
    """
    engine, db_path, execucoes = _build_pre_migration_engine()
    try:
        seeded = _seed_rows(engine, execucoes, rows)
        count_before = len(seeded)

        # Sanity: the generator must have produced >= 1 offending row.
        assert any(
            row["estagio"] < 1 or row["estagio"] > 5 for row in rows
        ), "generator must include at least one estagio outside 1..5"

        before = _read_rows(engine, execucoes)

        # Drive the REAL upgrade() — expect it to raise RuntimeError before any
        # UPDATE/INSERT/CREATE TABLE (the guard runs first — Req. 1.2, 1.5).
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

        # (1) No row altered: the table is identical to the pre-upgrade snapshot
        # (the guard ran before any UPDATE).
        after = _read_rows(engine, execucoes)
        assert after == before

        # (2) No Seleção da base STIX row inserted — row count unchanged (the
        # guard precedes the INSERT of base-selection rows).
        assert len(after) == count_before
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
