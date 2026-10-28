"""criar as tabelas do agente de IA e habilitar o pgvector (Fase 0, task 3.2)

Chained onto ``d4e1f6a2c3b9``
(``add_deprovisioning_columns_to_execution_reports``), the current head. This
migration implements the schema half of the ``ai-curation-agents`` spec's AI
persistence (task 3.1 defined the ORM in ``app/models/domain.py``; this task
3.2 lands the migration): it enables the ``pgvector`` extension in the existing
PostgreSQL and creates the five new AI-bookkeeping tables — ``agent_runs``,
``agent_iterations``, ``agent_stage_outputs``, ``agent_evaluations`` and the
long-term RAG ``experience_memory`` — with a cosine-similarity index on the
``experience_memory.embedding`` vector column.

What this migration does (Req. 6.2, 6.4, 6.5, 2.5, 11.1, 11.2)
--------------------------------------------------------------
* **Enables pgvector first (idempotent).** On PostgreSQL the very first step is
  ``CREATE EXTENSION IF NOT EXISTS vector``: it enables the extension when it is
  not yet enabled and completes without error when it already is (Req. 6.4). The
  extension is enabled *before* any table is created, because the
  ``experience_memory.embedding`` column uses the ``vector`` type the extension
  provides.
* **Aborts cleanly if pgvector cannot be enabled.** The whole ``upgrade`` runs
  inside the transaction Alembic manages, and ``CREATE EXTENSION`` is the first
  statement. If the extension cannot be enabled (e.g. the server lacks the
  pgvector package, or the role is not allowed to create it), the statement
  raises, the transaction rolls back and the migration aborts with a clear
  ``RuntimeError`` that names the pgvector extension — leaving the existing
  schema and data untouched because no table has been created yet (Req. 6.5).
* **Creates the five tables with portable-enum CHECK constraints.** Each enum
  column is a portable ``VARCHAR`` bounded by an explicit ``CHECK`` listing the
  enum's member *values* (mirroring ``_portable_enum`` in the ORM), so the value
  set is enforced on PostgreSQL and the test SQLite alike (Req. 2.5, 11.2). The
  ``agent_evaluations.rationale`` column keeps the ≤10,000-char CHECK and
  ``experience_memory.outcome`` keeps the ``sucesso | falha`` CHECK, matching the
  model's ``__table_args__``.
* **Adds a cosine index on the embedding (PostgreSQL only).** An IVFFlat index
  with ``vector_cosine_ops`` is created on ``experience_memory.embedding`` so RAG
  retrieval does top-k cosine similarity (``embedding <=> :q``) efficiently.
* **Alters no existing enum or constraint.** The migration is purely additive:
  it only enables an extension and creates new tables/index. No existing table,
  enum or CHECK (``translation_source`` / ``dag_origin`` / ... ) is touched, so
  the origin enum that already admits ``gerado_ia`` is left exactly as-is
  (Req. 2.5, 11.2). AI artifacts persist with ``origin = gerado_ia`` using that
  pre-existing portable enum — no migration of the origin itself is required.
* **Clean up∘down.** ``downgrade`` drops the cosine index and the five tables in
  reverse dependency order (children before parents), the exact inverse of the
  objects ``upgrade`` created, so ``upgrade`` ∘ ``downgrade`` is a lossless
  identity on the schema. The ``vector`` extension is intentionally **not**
  dropped on downgrade: ``DROP EXTENSION`` is not the inverse of the idempotent
  ``CREATE EXTENSION IF NOT EXISTS`` (the extension may have pre-existed this
  migration or be used elsewhere), and leaving it enabled keeps the downgrade
  safe and side-effect free on objects this migration does not own.

Portability (Req. 6.2; product is PostgreSQL, tests run on ephemeral SQLite)
----------------------------------------------------------------------------
The product database is PostgreSQL with pgvector; the migration integration
test (task 3.3) exercises ``upgrade()``/``downgrade()`` in-process. The three
PostgreSQL-only effects — ``CREATE EXTENSION``, the real ``vector`` column type
and the IVFFlat cosine index — are emitted only when the bind dialect is
``postgresql``. On any other backend the ``embedding`` column is created with
the portable ``pgvector.sqlalchemy.Vector`` type (which compiles to a plain
column off PostgreSQL) and the extension/index steps are skipped, so the table
set still round-trips cleanly. The ``vector`` dimension is resolved from the
``AI_EMBEDDING_DIM`` setting (design default 1024), reproduced locally here so
the migration stays self-contained and imports no application code (the
precedent set by every prior migration in this tree).

Revision ID: a1b2c3d4e5f6
Revises: d4e1f6a2c3b9
Create Date: 2026-10-19 01:00:00.000000+00:00

"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "d4e1f6a2c3b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Portable-enum value sets (the member *values*, matching app/models/enums.py).
# Each enum column below is a portable VARCHAR bounded by an explicit CHECK over
# these values — mirroring ``_portable_enum`` so the value set is enforced on
# PostgreSQL and the test SQLite alike (Req. 2.5, 11.2).
# ---------------------------------------------------------------------------

# AgentRunStatus
_AGENT_RUN_STATUS_VALUES: tuple[str, ...] = (
    "pendente",
    "em_execucao",
    "concluido",
    "falhou",
    "abortado",
)
# AgentStage
_AGENT_STAGE_VALUES: tuple[str, ...] = (
    "estagio_3_dag",
    "estagio_4_operacionalizacao",
    "estagio_5_ambiente",
)
# TranslationSource (origin) — the pre-existing portable enum; its value set
# already includes ``gerado_ia``. Re-stated here only to bound the NEW
# ``agent_stage_outputs.origin`` column's CHECK; no existing column is altered.
_TRANSLATION_SOURCE_VALUES: tuple[str, ...] = (
    "curadoria_humana",
    "gerado_ia",
)
# EvaluatorDecision
_EVALUATOR_DECISION_VALUES: tuple[str, ...] = (
    "refinar",
    "finalizar",
)
# experience_memory.outcome (bounded to two values by the model's CHECK).
_OUTCOME_VALUES: tuple[str, ...] = (
    "sucesso",
    "falha",
)

# Max length of the evaluator rationale (Req. 11.9), matching the model.
_RATIONALE_MAX_LENGTH = 10_000

# Default pgvector embedding width (``AI_EMBEDDING_DIM``), matching the ORM
# default in ``app/models/domain.py`` / ``SepinfSettings``.
_DEFAULT_EMBEDDING_DIM = 1024

# Name of the cosine-similarity index on ``experience_memory.embedding``.
_EMBEDDING_INDEX_NAME = "ix_experience_memory_embedding_cosine"


def _quote_values(values: tuple[str, ...]) -> str:
    """Render ``values`` as a SQL IN-list of single-quoted literals.

    The enum values are fixed internal literals (no user input), so a simple
    single-quote wrapping is sufficient and portable across PostgreSQL and
    SQLite for the generated CHECK expression.
    """
    return ", ".join(f"'{v}'" for v in values)


def _enum_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    """Build a portable ``CHECK (column IN (...))`` constraint for an enum column."""
    return sa.CheckConstraint(
        f"{column} IN ({_quote_values(values)})",
        name=name,
    )


def _read_root_env_value(key: str) -> Optional[str]:
    """Read ``key`` from the repository-root ``.env`` without extra dependencies.

    Mirrors how ``app/models/domain.py`` locates the root ``.env`` (relative to
    the source tree). Keeps the migration self-contained (no application import).
    parents: [versions] [alembic] [backend] [apps] [repo-root].
    """
    env_path = Path(__file__).resolve().parents[4] / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def _embedding_dim() -> int:
    """Resolve the pgvector embedding dimension (``AI_EMBEDDING_DIM``, default 1024).

    Process environment first, then the repository-root ``.env``, else the
    design default — identical resolution order to ``app/models/domain.py`` so
    the column width matches the ORM exactly.
    """
    raw = os.environ.get("AI_EMBEDDING_DIM")
    if raw is None:
        raw = _read_root_env_value("AI_EMBEDDING_DIM")
    if raw is None or raw.strip() == "":
        return _DEFAULT_EMBEDDING_DIM
    try:
        return int(raw.strip())
    except ValueError:
        return _DEFAULT_EMBEDDING_DIM


def _is_postgresql() -> bool:
    """True when the live bind is PostgreSQL (where pgvector is available)."""
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    is_pg = _is_postgresql()

    # (1) Enable pgvector FIRST (idempotent). On PostgreSQL this runs before any
    # table is created, so if it fails the transaction rolls back with no table
    # created — the schema/data are left unchanged and the error names pgvector
    # (Req. 6.4, 6.5). Skipped off PostgreSQL (the portable Vector type compiles
    # to a plain column there).
    if is_pg:
        try:
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as exc:  # noqa: BLE001 - re-raised with a clear message
            raise RuntimeError(
                "Migration aborted: the 'vector' (pgvector) extension could not "
                "be enabled in the existing PostgreSQL database. The schema and "
                f"data were left unchanged. Underlying error: {exc}"
            ) from exc

    # (2) agent_runs — one row per AI curation run of a campaign (Req. 11.1,
    # 11.4). ``status`` is a portable VARCHAR bounded by the AgentRunStatus CHECK.
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("iterations_used", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("final_verdict_success", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
        _enum_check("status", _AGENT_RUN_STATUS_VALUES, "ck_agent_runs_status"),
    )

    # (3) agent_iterations — one pass through the loop (Req. 11.5).
    # ``refined_stage`` is a nullable portable VARCHAR bounded by AgentStage.
    op.create_table(
        "agent_iterations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("refined_stage", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        _enum_check(
            "refined_stage", _AGENT_STAGE_VALUES, "ck_agent_iterations_refined_stage"
        ),
    )

    # (4) agent_stage_outputs — the per-stage agent artifact of an iteration
    # (Req. 11.5). ``stage`` (AgentStage) and ``origin`` (TranslationSource,
    # always ``gerado_ia``) are portable VARCHARs bounded by their CHECKs; the
    # origin CHECK re-states the pre-existing enum value set WITHOUT touching the
    # existing origin columns.
    op.create_table(
        "agent_stage_outputs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("iteration_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dag_id", sa.Integer(), nullable=True),
        sa.Column("params_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["iteration_id"], ["agent_iterations.id"]),
        sa.ForeignKeyConstraint(["dag_id"], ["dags.id"]),
        sa.ForeignKeyConstraint(["params_id"], ["environment_parameters.id"]),
        sa.PrimaryKeyConstraint("id"),
        _enum_check("stage", _AGENT_STAGE_VALUES, "ck_agent_stage_outputs_stage"),
        _enum_check(
            "origin", _TRANSLATION_SOURCE_VALUES, "ck_agent_stage_outputs_origin"
        ),
    )

    # (5) agent_evaluations — the evaluator's reading of the Stage 6 report
    # (Req. 11.9). ``decision`` (EvaluatorDecision) is a portable VARCHAR bounded
    # by its CHECK; ``rationale`` keeps the ≤10,000-char CHECK of the model.
    op.create_table(
        "agent_evaluations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("iteration_id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=True),
        sa.Column("success_rate", sa.Float(), nullable=False),
        sa.Column("procedural_coverage", sa.Float(), nullable=False),
        sa.Column("verdict_success", sa.Boolean(), nullable=False),
        sa.Column("failed_nodes", sa.JSON(), nullable=True),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("rationale", sa.String(length=_RATIONALE_MAX_LENGTH), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["iteration_id"], ["agent_iterations.id"]),
        sa.ForeignKeyConstraint(["report_id"], ["execution_reports.id"]),
        sa.PrimaryKeyConstraint("id"),
        _enum_check("decision", _EVALUATOR_DECISION_VALUES, "ck_agent_evaluations_decision"),
        sa.CheckConstraint(
            f"length(rationale) <= {_RATIONALE_MAX_LENGTH}",
            name="ck_agent_evaluations_rationale_max_length",
        ),
    )

    # (6) experience_memory — the long-term RAG store (Req. 11.1). ``embedding``
    # is a fixed-width pgvector VECTOR(dim) on PostgreSQL (the portable Vector
    # type compiles to a plain column off PostgreSQL). ``stage`` (AgentStage) and
    # ``outcome`` (sucesso | falha) are portable VARCHARs bounded by CHECKs.
    op.create_table(
        "experience_memory",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(_embedding_dim()), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
        _enum_check("stage", _AGENT_STAGE_VALUES, "ck_experience_memory_stage"),
        _enum_check("outcome", _OUTCOME_VALUES, "ck_experience_memory_outcome"),
    )

    # (7) Cosine-similarity index on the embedding (PostgreSQL only). IVFFlat
    # with ``vector_cosine_ops`` backs the RAG top-k (``embedding <=> :q``).
    # Skipped off PostgreSQL where pgvector operator classes do not exist.
    if is_pg:
        op.execute(
            f"CREATE INDEX {_EMBEDDING_INDEX_NAME} "
            "ON experience_memory USING ivfflat (embedding vector_cosine_ops) "
            "WITH (lists = 100)"
        )


def downgrade() -> None:
    is_pg = _is_postgresql()

    # (1) Drop the cosine index (PostgreSQL only) — inverse of upgrade step (7).
    if is_pg:
        op.execute(f"DROP INDEX IF EXISTS {_EMBEDDING_INDEX_NAME}")

    # (2) Drop the five tables in reverse dependency order (children before
    # parents) — the exact inverse of the create order in upgrade.
    op.drop_table("experience_memory")
    op.drop_table("agent_evaluations")
    op.drop_table("agent_stage_outputs")
    op.drop_table("agent_iterations")
    op.drop_table("agent_runs")

    # (3) The ``vector`` extension is intentionally left enabled: DROP EXTENSION
    # is not the inverse of the idempotent CREATE EXTENSION IF NOT EXISTS (it may
    # have pre-existed this migration or be used elsewhere). up∘down stays a
    # clean identity over the objects this migration owns (the five tables + the
    # cosine index).
