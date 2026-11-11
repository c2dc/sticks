"""rename AgentStage from Legacy_Stage_Numbering to semantic values (agentic-phase-model-refactor task 2.1)

Chained onto ``b9c0d1e2f3a4`` (``create_provisioning_map``), the current head.
This migration lands the data/schema half of the ``agentic-phase-model-refactor``
spec's §1 rename (Req. 1.3, 9.3): task 1 already rewrote ``AgentStage`` in
``app/models/enums.py`` from the Legacy_Stage_Numbering values
(``stage_3_dag`` / ``stage_4_operationalization`` / ``stage_5_environment``) to
the **semantic, number-free** phase names (``dag`` / ``operationalization`` /
``environment``) and declared the two new own-phase members ``execution`` and
``evaluation``; this migration brings the live schema and seeded rows in line
with that ORM.

What "the CHECK" is for these columns (the ``b2c3d4e5f6a7`` precedent)
----------------------------------------------------------------------
Every column backed by :class:`AgentStage` persists with ``native_enum=False``
(``_portable_enum`` in ``app/models/domain.py``). On this tree those three
columns carry **explicit named** ``CHECK`` constraints created by migration
``a1b2c3d4e5f6`` and last rewritten (pt-BR → English) by ``b2c3d4e5f6a7``:

* ``agent_iterations.refined_stage`` (nullable) — ``ck_agent_iterations_refined_stage``
* ``agent_stage_outputs.stage`` (NOT NULL) — ``ck_agent_stage_outputs_stage``
* ``experience_memory.stage`` (NOT NULL) — ``ck_experience_memory_stage``

So regenerating "the CHECK" to the semantic value set means, per column, the
same faithful three-step dance ``b2c3d4e5f6a7`` established:

1. **Open the constraint.** Drop the named ``CHECK`` inside a batch block so the
   column temporarily admits any value and the in-place ``UPDATE`` can never be
   rejected while rows still hold legacy values.
2. **Convert the data.** ``UPDATE <table> SET <col> = :semantic WHERE <col> =
   :legacy`` per mapped member, via a **parameterized** statement (``sa.text``
   bind params — plain VARCHAR values, so this is a lossless rewrite that
   preserves every row and FK; per-table row counts are invariant). A NULL
   ``refined_stage`` is untouched by any ``UPDATE`` and stays valid.
3. **Install the new CHECK.** Re-create the named ``CHECK (col IN (<semantic>))``
   (the full five-member set ``dag``/``operationalization``/``environment``/
   ``execution``/``evaluation``) inside the batch block, so the column now
   admits exactly the semantic values the ORM expects — including the two new
   own phases ``execution`` and ``evaluation`` that have no legacy predecessor.

Every step runs inside ``op.batch_alter_table`` so SQLite rebuilds the table
(dropping/creating the named CHECK) and PostgreSQL emits the plain ``ALTER``
equivalents (``render_as_batch`` is enabled for SQLite in ``env.py``) — the
SQLite-safe / batch-mode precedent of ``b2c3d4e5f6a7``.

Columns regenerated (table → column), per design §Data Models
--------------------------------------------------------------
``agent_iterations.refined_stage`` (nullable), ``agent_stage_outputs.stage``,
``experience_memory.stage``.

Symmetry (Req. 9.3)
-------------------
``downgrade`` applies the exact inverse: per column it drops the semantic
``CHECK``, applies the inverse semantic → legacy map (via the same parameterized
``UPDATE``), and re-installs the legacy ``CHECK`` over the three
Legacy_Stage_Numbering values. The three mapped members round-trip losslessly
(``stage_3_dag`` → ``dag`` → ``stage_3_dag``), so ``upgrade`` ∘ ``downgrade`` is
an identity on both data and schema for every row seeded under the legacy enum.
The two new own-phase values (``execution`` / ``evaluation``) have no legacy
predecessor and only enter the data once the later execution/evaluation
separation lands; before that, no row can hold them, so the inverse map covers
every pre-existing row.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-10-27 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, None] = "b9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# The Legacy_Stage_Numbering → semantic value map (design §1 "Legado / Semântico"
# table). Each tuple is ``(legacy_value, semantic_value)``: only the three
# numbered members map to a semantic predecessor. ``execution`` and
# ``evaluation`` are NEW own phases with no legacy value, so they appear only in
# the installed CHECK value set, never in the data conversion pairs.
# ---------------------------------------------------------------------------
_STAGE_MAP: tuple[tuple[str, str], ...] = (
    ("stage_3_dag", "dag"),
    ("stage_4_operationalization", "operationalization"),
    ("stage_5_environment", "environment"),
)

# The full semantic value set installed by ``upgrade`` — the five number-free
# phases of the agentic-centric model. Order follows the Phase_Order
# (``dag → operationalization → environment → execution → evaluation``).
_SEMANTIC_VALUES: tuple[str, ...] = (
    "dag",
    "operationalization",
    "environment",
    "execution",
    "evaluation",
)

# The legacy value set restored by ``downgrade`` — exactly the three
# Legacy_Stage_Numbering values the columns held before this migration (the set
# migration ``b2c3d4e5f6a7`` installed as the English CHECK).
_LEGACY_VALUES: tuple[str, ...] = tuple(legacy for legacy, _sem in _STAGE_MAP)


# ---------------------------------------------------------------------------
# The three columns backed by ``AgentStage``, each bound to its named CHECK and
# nullability so the same driver regenerates every column in both directions.
# Each entry is ``(table, column, nullable, check_name)``.
# ---------------------------------------------------------------------------
_COLUMNS: tuple[tuple[str, str, bool, str], ...] = (
    (
        "agent_iterations",
        "refined_stage",
        True,
        "ck_agent_iterations_refined_stage",
    ),
    (
        "agent_stage_outputs",
        "stage",
        False,
        "ck_agent_stage_outputs_stage",
    ),
    (
        "experience_memory",
        "stage",
        False,
        "ck_experience_memory_stage",
    ),
)


def _check_expr(column: str, values: Sequence[str]) -> str:
    """Render ``column IN ('v1', 'v2', ...)`` for a named CHECK constraint.

    The enum values are fixed internal literals (no user input), so single-quote
    wrapping is portable and safe — identical to the ``_enum_check`` /
    ``_check_expr`` form migrations ``a1b2c3d4e5f6`` and ``b2c3d4e5f6a7`` used to
    create/recreate these constraints, so the installed body matches.
    """
    in_list = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({in_list})"


def _convert(table: str, column: str, pairs: Sequence[tuple[str, str]]) -> None:
    """Rewrite every ``src`` value of ``table.column`` to ``dst`` in place.

    Uses a **parameterized** UPDATE (``sa.text`` bind params) per member so the
    literal values are never interpolated into the SQL string. Plain VARCHAR
    rewrite: every row/FK is preserved, the per-table row count is invariant,
    and a NULL value matches no ``WHERE`` clause so it is left untouched.
    """
    bind = op.get_bind()
    for src, dst in pairs:
        bind.execute(
            sa.text(f"UPDATE {table} SET {column} = :dst WHERE {column} = :src"),
            {"dst": dst, "src": src},
        )


def _regenerate(
    table: str,
    column: str,
    check_name: str,
    pairs: Sequence[tuple[str, str]],
    dst_check_values: Sequence[str],
) -> None:
    """Open → convert → install, for one column, in one direction.

    (1) Drop the named CHECK so the in-place rewrite is never rejected; (2) apply
    the ``pairs`` value map (parameterized, lossless, row-count invariant);
    (3) re-create the named CHECK over ``dst_check_values``. All inside a batch
    block so SQLite rebuilds the table and PostgreSQL emits plain ``ALTER`` — the
    ``b2c3d4e5f6a7`` precedent. The column type stays a portable VARCHAR
    throughout; only the named CHECK value set changes.
    """
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_constraint(check_name, type_="check")

    _convert(table, column, pairs)

    with op.batch_alter_table(table) as batch_op:
        batch_op.create_check_constraint(
            check_name, _check_expr(column, dst_check_values)
        )


def upgrade() -> None:
    # Legacy_Stage_Numbering → semantic across every AgentStage-backed column.
    # Each column's CHECK is regenerated to the full five-member semantic set
    # (including the new own phases ``execution``/``evaluation``) and its legacy
    # rows are converted in place (``stage_3_dag → dag`` …).
    upgrade_pairs = list(_STAGE_MAP)  # (legacy, semantic)
    for table, column, _nullable, check_name in _COLUMNS:
        _regenerate(table, column, check_name, upgrade_pairs, _SEMANTIC_VALUES)


def downgrade() -> None:
    # Exact inverse: semantic → Legacy_Stage_Numbering, restoring each column's
    # original legacy value and its legacy CHECK. The three mapped members
    # round-trip losslessly; no pre-existing row can hold ``execution`` /
    # ``evaluation`` (those own phases enter only after the later separation),
    # so the inverse map covers every seeded row.
    downgrade_pairs = [(sem, legacy) for legacy, sem in _STAGE_MAP]
    for table, column, _nullable, check_name in _COLUMNS:
        _regenerate(table, column, check_name, downgrade_pairs, _LEGACY_VALUES)
