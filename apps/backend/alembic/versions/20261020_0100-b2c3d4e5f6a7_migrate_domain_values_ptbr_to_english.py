"""migrate every Domain_Value from pt-BR to English (ai-first-campaign-flow task 1.3)

Chained onto ``a1b2c3d4e5f6``
(``criar as tabelas do agente de IA e habilitar o pgvector``), the live DB head.
This migration implements the design's **§I English Domain_Value map** (§Data
Models — Revision 2): it rewrites every pt-BR portable-enum *value* persisted in
the schema to its canonical English form, column by column, reversibly. Tasks
1.1/1.2 already translated ``app/models/enums.py`` and ``app/models/domain.py``
to the English values (and consolidated ``EnvironmentPlatform``); this migration
brings the live data in line with that ORM so seeded rows convert in place.

What "the CHECK" is in this codebase (two kinds — the ``2d8cb3e55bb7`` /
``c9d2e4f8a1b7`` / ``a1b2c3d4e5f6`` precedents)
---------------------------------------------------------------------------
The project runs **SQLAlchemy 2.1**, where ``Enum.create_constraint`` defaults
to ``False``, so the enum-bearing columns come in two physical shapes:

* **Older domain tables** (``campaigns``, ``abilities``, ``adversaries``,
  ``dags``, ``stage_runs``, ``operations``, ``ability_results``, ``preferences``,
  ``environment_parameters``, ``provisioned_environment``) were created with
  ``sa.Enum(..., native_enum=False)`` — which under SQLAlchemy 2.1 emits **no**
  named ``CHECK`` on either backend. Each compiles to a bare, length-bounded
  ``VARCHAR(n)`` whose bound is the longest member value; the value set is
  enforced in the application layer by ``validate_strings=True`` (the
  ``c9d2e4f8a1b7`` migration's docstring establishes this precedent for this
  tree). For these columns the physical "CHECK" IS the ``VARCHAR`` bound.
* **The five AI tables** (``agent_runs``, ``agent_iterations``,
  ``agent_stage_outputs``, ``agent_evaluations``, ``experience_memory``) were
  created by migration ``a1b2c3d4e5f6`` with **explicit named** ``CHECK``
  constraints (``ck_agent_runs_status``, ``ck_agent_iterations_refined_stage``,
  ``ck_agent_stage_outputs_stage``, ``ck_agent_stage_outputs_origin``,
  ``ck_agent_evaluations_decision``, ``ck_experience_memory_stage``,
  ``ck_experience_memory_outcome``) via that migration's ``_enum_check`` helper.
  These are real, enforced ``CHECK (col IN (...))`` constraints on both
  PostgreSQL and the test SQLite, and they reject the English values until the
  constraint itself is replaced.

So "drop/widen the CHECK → UPDATE values → install the English-only CHECK" maps,
faithfully, onto the physical reality of each column kind:

1. **Open the constraint.** For a named-CHECK column, *drop* the named ``CHECK``
   inside the batch block. For a bare-VARCHAR column, *widen* the ``VARCHAR``
   bound to hold *both* the pt-BR and the English value sets. Either way the
   value set is opened so the in-place ``UPDATE`` can never be rejected while
   rows still hold pt-BR values.
2. **Data conversion.** ``UPDATE <table> SET <col> = :english WHERE <col> =
   :ptbr`` per member, via a **parameterized** statement (``sa.text`` bind
   params — the values are plain VARCHAR, so this is a lossless rewrite that
   preserves every row and FK; per-table row counts are invariant).
3. **Install the English-only CHECK.** For a named-CHECK column, *create* the
   English-only named ``CHECK (col IN (<english>))`` inside the batch block. For
   a bare-VARCHAR column, *narrow* the ``VARCHAR`` bound to the English-only
   value set. The column now admits exactly the English values the ORM expects.

Every step runs inside ``op.batch_alter_table`` so SQLite rebuilds the table
(dropping/creating the named CHECK and restating the column type) and PostgreSQL
emits the plain ``ALTER`` equivalents (``render_as_batch`` is enabled for SQLite
in ``env.py``) — the SQLite-safe / batch-mode precedent of ``2d8cb3e55bb7`` and
``c9d2e4f8a1b7``.

Columns converted (table → column), per §Data Models
-----------------------------------------------------
``campaigns.translation_source``, ``abilities.translation_source``,
``adversaries.translation_source``, ``environment_parameters.platform``,
``environment_parameters.origin``, ``provisioned_environment.platform``,
``provisioned_environment.origin``, ``dags.origin``, ``stage_runs.state``,
``operations.state``, ``ability_results.status``, ``preferences.theme``,
``agent_runs.status``, ``agent_iterations.refined_stage`` (nullable),
``agent_stage_outputs.stage``, ``agent_stage_outputs.origin``,
``agent_evaluations.decision``, ``experience_memory.stage``,
``experience_memory.outcome``.

The ``preferences.language`` column (``pt-BR`` / ``en``) is deliberately
**untouched** in both directions — ``Language`` keeps its values per §I.

Symmetry
--------
``downgrade`` applies the exact inverse: per column it opens the constraint
(drops the named CHECK, or widens the VARCHAR bound), applies the inverse
English → pt-BR map (via the same parameterized ``UPDATE``), and installs the
pt-BR constraint (re-creates the named CHECK over the pt-BR values, or narrows
the VARCHAR bound to the pt-BR set) — restoring every column's original pt-BR
value and its original constraint. ``upgrade`` ∘ ``downgrade`` is therefore a
lossless identity on both the data and the schema (Req. 10.2, 10.3, 10.4).

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-20 01:00:00.000000+00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# The canonical pt-BR → English Domain_Value map (design §I), expressed per
# enum value set. Each tuple is ``(ptbr_value, english_value)``.
# ---------------------------------------------------------------------------

# TranslationSource / DagOrigin (``origin`` columns) — same two members.
_TRANSLATION_SOURCE: tuple[tuple[str, str], ...] = (
    ("curadoria_humana", "human_curation"),
    ("gerado_ia", "ai_generated"),
)
# EnvironmentPlatform (consolidated: container today, VM declared) — ``platform``.
_ENVIRONMENT_PLATFORM: tuple[tuple[str, str], ...] = (
    ("conteiner", "container"),
    ("maquina_virtual", "virtual_machine"),
)
# StageState — ``stage_runs.state``.
_STAGE_STATE: tuple[tuple[str, str], ...] = (
    ("nao_iniciado", "not_started"),
    ("em_andamento", "in_progress"),
    ("concluido", "completed"),
    ("erro", "error"),
)
# OperationState — ``operations.state`` (includes the Stage-6 timeout state).
_OPERATION_STATE: tuple[tuple[str, str], ...] = (
    ("nao_iniciada", "not_started"),
    ("em_execucao", "running"),
    ("finalizada", "finished"),
    ("abortada", "aborted"),
    ("incompleta_timeout", "incomplete_timeout"),
)
# AbilityResultStatus — ``ability_results.status``.
_ABILITY_RESULT_STATUS: tuple[tuple[str, str], ...] = (
    ("pendente", "pending"),
    ("em_execucao", "running"),
    ("sucesso", "success"),
    ("falha", "failure"),
)
# AgentRunStatus — ``agent_runs.status``.
_AGENT_RUN_STATUS: tuple[tuple[str, str], ...] = (
    ("pendente", "pending"),
    ("em_execucao", "running"),
    ("concluido", "completed"),
    ("falhou", "failed"),
    ("abortado", "aborted"),
)
# AgentStage — ``agent_iterations.refined_stage`` / ``agent_stage_outputs.stage``
# / ``experience_memory.stage``.
_AGENT_STAGE: tuple[tuple[str, str], ...] = (
    ("estagio_3_dag", "stage_3_dag"),
    ("estagio_4_operacionalizacao", "stage_4_operationalization"),
    ("estagio_5_ambiente", "stage_5_environment"),
)
# EvaluatorDecision — ``agent_evaluations.decision``.
_EVALUATOR_DECISION: tuple[tuple[str, str], ...] = (
    ("refinar", "refine"),
    ("finalizar", "finish"),
)
# Theme — ``preferences.theme``.
_THEME: tuple[tuple[str, str], ...] = (
    ("claro", "light"),
    ("escuro", "dark"),
)
# experience_memory.outcome (bounded to two values).
_OUTCOME: tuple[tuple[str, str], ...] = (
    ("sucesso", "success"),
    ("falha", "failure"),
)


# ---------------------------------------------------------------------------
# The ordered column list (§Data Models). Each entry binds a physical
# ``(table, column, value_map, nullable, check_name)`` so the same driver
# converts every column in both directions. Order follows the §Data Models
# listing. ``nullable`` only affects the re-stated column type (so a NULL
# ``refined_stage`` stays valid); the data UPDATE is a no-op on NULL regardless.
#
# ``check_name`` is the name of the explicit ``CHECK`` the column carries:
#   * ``None`` — a bare-VARCHAR portable enum (no named CHECK under SQLAlchemy
#     2.1); the physical "CHECK" is the VARCHAR bound, opened/installed by
#     widening/narrowing it.
#   * a string — the named ``CHECK`` the five AI tables got from migration
#     ``a1b2c3d4e5f6``; it is dropped before the UPDATE and re-created
#     (English-only) after, inside the batch block.
# ---------------------------------------------------------------------------
_COLUMNS: tuple[tuple[str, str, tuple[tuple[str, str], ...], bool, Union[str, None]], ...] = (
    ("campaigns", "translation_source", _TRANSLATION_SOURCE, False, None),
    ("abilities", "translation_source", _TRANSLATION_SOURCE, False, None),
    ("adversaries", "translation_source", _TRANSLATION_SOURCE, False, None),
    ("environment_parameters", "platform", _ENVIRONMENT_PLATFORM, False, None),
    ("environment_parameters", "origin", _TRANSLATION_SOURCE, False, None),
    ("provisioned_environment", "platform", _ENVIRONMENT_PLATFORM, False, None),
    ("provisioned_environment", "origin", _TRANSLATION_SOURCE, False, None),
    ("dags", "origin", _TRANSLATION_SOURCE, False, None),
    ("stage_runs", "state", _STAGE_STATE, False, None),
    ("operations", "state", _OPERATION_STATE, False, None),
    ("ability_results", "status", _ABILITY_RESULT_STATUS, False, None),
    ("preferences", "theme", _THEME, False, None),
    ("agent_runs", "status", _AGENT_RUN_STATUS, False, "ck_agent_runs_status"),
    (
        "agent_iterations",
        "refined_stage",
        _AGENT_STAGE,
        True,
        "ck_agent_iterations_refined_stage",
    ),
    (
        "agent_stage_outputs",
        "stage",
        _AGENT_STAGE,
        False,
        "ck_agent_stage_outputs_stage",
    ),
    (
        "agent_stage_outputs",
        "origin",
        _TRANSLATION_SOURCE,
        False,
        "ck_agent_stage_outputs_origin",
    ),
    (
        "agent_evaluations",
        "decision",
        _EVALUATOR_DECISION,
        False,
        "ck_agent_evaluations_decision",
    ),
    ("experience_memory", "stage", _AGENT_STAGE, False, "ck_experience_memory_stage"),
    ("experience_memory", "outcome", _OUTCOME, False, "ck_experience_memory_outcome"),
)


def _varchar_for(values: Sequence[str]) -> sa.String:
    """Portable ``VARCHAR(n)`` whose bound fits the longest of ``values``.

    Mirrors how ``_portable_enum`` (``native_enum=False``) compiles on
    SQLAlchemy 2.1: a bare length-bounded VARCHAR, identical on PostgreSQL and
    the test SQLite. For a bare-VARCHAR column the bound is what constrains the
    value set physically, so widening/narrowing it IS the open/install-the-CHECK
    step.
    """
    return sa.String(length=max(len(v) for v in values))


def _check_expr(column: str, values: Sequence[str]) -> str:
    """Render ``column IN ('v1', 'v2', ...)`` for a named CHECK constraint.

    The enum values are fixed internal literals (no user input), so single-quote
    wrapping is sufficient and portable — identical to the ``_enum_check`` helper
    migration ``a1b2c3d4e5f6`` used to create these constraints, so the installed
    body matches the original form.
    """
    in_list = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({in_list})"


def _open_constraint(
    table: str,
    column: str,
    both_values: Sequence[str],
    nullable: bool,
    check_name: Union[str, None],
) -> None:
    """Open the value-set constraint on ``table.column`` for the in-place UPDATE.

    * Named-CHECK column (the five AI tables): drop the named ``CHECK`` so the
      column temporarily admits any value during the rewrite.
    * Bare-VARCHAR column: widen the ``VARCHAR`` bound to hold BOTH the source and
      destination value sets so no value is rejected/truncated mid-conversion.

    Runs in a batch block (SQLite rebuilds the table; PostgreSQL emits the plain
    ``ALTER``) — the ``2d8cb3e55bb7`` / ``c9d2e4f8a1b7`` precedent.
    """
    with op.batch_alter_table(table) as batch_op:
        if check_name is not None:
            batch_op.drop_constraint(check_name, type_="check")
        else:
            batch_op.alter_column(
                column,
                type_=_varchar_for(both_values),
                existing_nullable=nullable,
            )


def _install_constraint(
    table: str,
    column: str,
    dst_values: Sequence[str],
    nullable: bool,
    check_name: Union[str, None],
) -> None:
    """Install the destination-only value-set constraint on ``table.column``.

    * Named-CHECK column: create the named ``CHECK (column IN (<dst>))`` so the
      column admits exactly the destination value set (re-created under its
      original name so the schema still matches the ORM/precedent).
    * Bare-VARCHAR column: narrow the ``VARCHAR`` bound to the destination value
      set.

    Runs in a batch block — same portability contract as :func:`_open_constraint`.
    """
    with op.batch_alter_table(table) as batch_op:
        if check_name is not None:
            batch_op.create_check_constraint(check_name, _check_expr(column, dst_values))
        else:
            batch_op.alter_column(
                column,
                type_=_varchar_for(dst_values),
                existing_nullable=nullable,
            )


def _convert(table: str, column: str, pairs: Sequence[tuple[str, str]]) -> None:
    """Rewrite every ``src`` value of ``table.column`` to ``dst`` in place.

    Uses a **parameterized** UPDATE (``sa.text`` bind params) per member so the
    literal values are never interpolated into the SQL string. Plain VARCHAR
    rewrite: every row/FK is preserved and the per-table row count is invariant.
    """
    bind = op.get_bind()
    for src, dst in pairs:
        bind.execute(
            sa.text(f"UPDATE {table} SET {column} = :dst WHERE {column} = :src"),  # noqa: S608 -- table and column names come from module constants; values are bound
            {"dst": dst, "src": src},
        )


def _migrate(forward: bool) -> None:
    """Drive every column conversion in one direction.

    For each column, in §Data Models order: (1) open the constraint (drop the
    named CHECK, or widen the VARCHAR bound to admit both value sets), (2) rewrite
    the values in the requested direction, (3) install the destination-only
    constraint (re-create the named CHECK over the destination values, or narrow
    the VARCHAR bound). The ``preferences.language`` column is absent from
    ``_COLUMNS`` and so is left untouched in both directions.
    """
    for table, column, value_map, nullable, check_name in _COLUMNS:
        ptbr_values = [ptbr for ptbr, _en in value_map]
        english_values = [en for _ptbr, en in value_map]

        dst_values = english_values if forward else ptbr_values
        pairs = [(p, e) for p, e in value_map] if forward else [(e, p) for p, e in value_map]

        # (1) Open the constraint (drop named CHECK, or widen the VARCHAR bound
        # to hold BOTH sets during the in-place rewrite).
        both_values = ptbr_values + english_values
        _open_constraint(table, column, both_values, nullable, check_name)

        # (2) Convert the data (parameterized, lossless, row-count invariant).
        _convert(table, column, pairs)

        # (3) Install the destination-only CHECK (re-create named CHECK over the
        # destination values, or narrow the VARCHAR bound).
        _install_constraint(table, column, dst_values, nullable, check_name)


def upgrade() -> None:
    # pt-BR → English across every enum-bearing column (§Data Models), column by
    # column, in one transaction. ``preferences.language`` is left untouched.
    _migrate(forward=True)


def downgrade() -> None:
    # Exact inverse: English → pt-BR, restoring every original value and the
    # pt-BR bound. ``preferences.language`` is left untouched.
    _migrate(forward=False)
