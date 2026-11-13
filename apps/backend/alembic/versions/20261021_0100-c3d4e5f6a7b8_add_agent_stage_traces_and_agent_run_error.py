"""add the agent_stage_traces audit-trace table + agent_runs.error (ai-first-campaign-flow task 3.3)

Chained onto ``b2c3d4e5f6a7``
(``migrate every Domain_Value from pt-BR to English``, the Domain_Value
migration of this spec, task 1.3), the live DB head. This migration lands the
schema half of Axis 2's Audit_Trace persistence: task 3.2 defined the
:class:`AgentStageTrace` ORM model (table ``agent_stage_traces``) and the new
nullable ``agent_runs.error`` column in ``app/models/domain.py``; this task 3.3
brings the live schema in line with that ORM.

What this migration does (Req. 2.7)
-----------------------------------
* **Creates ``agent_stage_traces`` (1:1 with ``agent_stage_outputs``).** One
  trace row per stage output, in a strict 1:1 relationship via the non-null
  ``stage_output_id`` FK carrying a UNIQUE constraint (so each stage output has
  at most one trace). The trace content lives in two portable JSON columns —
  ``agent_input`` (prior artifacts, campaign context, retrieved experience,
  ATT&CK/STIX attack context) and ``processing`` (llm_prompt, llm_response,
  reasoning, tools, repair attempts, and for the Environment agent the
  fixed-vs-agent-decided split + gate verdicts) — plus the ``created_at``
  timestamp, mirroring the ORM exactly.
* **Adds the nullable ``agent_runs.error`` column.** A ``Text`` column written
  by ``_fail_run`` with the failing-stage + absent-input cause and surfaced by
  the read endpoint (Req. 2.6, 2.7). It is ``nullable=True`` with no server
  default, so existing ``agent_runs`` rows need no backfill.

Purely additive — no data touched (Req. 2.7)
--------------------------------------------
The migration only CREATEs a new table and ADDs a nullable column; it moves no
data, backfills nothing, and alters no existing enum, CHECK or column. Following
the precedent of the prior migrations in this tree:

* **Portability.** The product database is PostgreSQL; the unit tests run
  against an ephemeral SQLite file. SQLite before 3.35 cannot ``DROP COLUMN``
  and has limited ``ALTER TABLE``, so the ``add_column`` / ``drop_column`` on
  ``agent_runs`` both run inside ``op.batch_alter_table`` (a thin ``ALTER`` on
  PostgreSQL; a table rebuild on SQLite — ``render_as_batch`` is enabled for
  SQLite in ``env.py``). ``create_table`` / ``drop_table`` are backend-portable
  as-is.
* **Symmetric downgrade.** ``downgrade`` drops exactly the objects ``upgrade``
  created: it first drops the ``agent_runs.error`` column (in batch mode for
  SQLite safety) and then drops the ``agent_stage_traces`` table — the exact
  inverse of the upgrade order (the column is added last, so it is dropped
  first), so ``upgrade`` ∘ ``downgrade`` is a lossless identity on the schema.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-21 01:00:00.000000+00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Name of the new audit-trace table (1:1 with ``agent_stage_outputs``).
_TRACE_TABLE = "agent_stage_traces"
# The nullable failure-cause column added to ``agent_runs``.
_RUN_ERROR_COLUMN = "error"


def upgrade() -> None:
    # (1) Create the agent_stage_traces table (Req. 2.7). Mirrors the ORM
    # AgentStageTrace: integer PK, a non-null ``stage_output_id`` FK to
    # ``agent_stage_outputs.id`` carrying a UNIQUE constraint (strict 1:1), two
    # non-null portable JSON columns (``agent_input`` + ``processing``), and the
    # non-null ``created_at`` timestamp. Portable across PostgreSQL and SQLite.
    op.create_table(
        _TRACE_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stage_output_id", sa.Integer(), nullable=False),
        sa.Column("agent_input", sa.JSON(), nullable=False),
        sa.Column("processing", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["stage_output_id"], ["agent_stage_outputs.id"]),
        sa.PrimaryKeyConstraint("id"),
        # 1:1: each stage output has at most one trace row.
        sa.UniqueConstraint("stage_output_id", name="uq_agent_stage_traces_stage_output_id"),
    )

    # (2) Add the nullable ``agent_runs.error`` column (Req. 2.6, 2.7). Additive
    # and nullable with no server default, so existing rows get NULL and need no
    # backfill. Batch mode keeps the ALTER portable on SQLite.
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(sa.Column(_RUN_ERROR_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    # Exact inverse of ``upgrade`` (the column is added last, so drop it first).
    # (1) Drop the ``agent_runs.error`` column in batch mode (SQLite cannot
    # DROP COLUMN natively on older versions; render_as_batch rebuilds the
    # table there, while PostgreSQL emits a plain ALTER).
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_column(_RUN_ERROR_COLUMN)

    # (2) Drop the agent_stage_traces table (the first object created).
    op.drop_table(_TRACE_TABLE)
