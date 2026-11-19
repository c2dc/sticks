"""Add interactive run states and per-phase approval columns.

Additive, reversible migration for the interactive emulation run:

* ``agent_runs``: widens ``ck_agent_runs_status`` with ``awaiting_approval`` and
  adds the frozen language-model profile id, the current/pending phase, the
  approvals, the attack flow proposal history, the structured failure, the
  notice code, the coverage and the negotiation round counter. Every new column
  is nullable, except ``negotiation_rounds_used`` (INTEGER NOT NULL, default 0).
* ``operations``: adds the nullable ``abort_reason``.

Existing rows need no backfill. The downgrade converts ``awaiting_approval``
rows to ``aborted``, restores the previous CHECK and drops only the columns
added here.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-11-08 01:00:00.000000+00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "agent_runs"
_CHECK = "ck_agent_runs_status"
_OLD_STATUSES: tuple[str, ...] = ("pending", "running", "completed", "failed", "aborted")
_NEW_STATUSES: tuple[str, ...] = (*_OLD_STATUSES, "awaiting_approval")


def _status_check(values: Sequence[str]) -> str:
    """Render the ``status IN (...)`` expression (fixed internal literals)."""
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    """Widen the status CHECK and add the new nullable columns."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CHECK, type_="check")
        batch.create_check_constraint(_CHECK, _status_check(_NEW_STATUSES))
        batch.add_column(sa.Column("llm_profile_id", sa.String(120), nullable=True))
        batch.add_column(sa.Column("current_phase", sa.String(), nullable=True))
        batch.add_column(sa.Column("approvals", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("flow_proposals", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("failure", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("notice_code", sa.String(), nullable=True))
        batch.add_column(sa.Column("coverage", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column(
                "negotiation_rounds_used",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
    op.add_column("operations", sa.Column("abort_reason", sa.String(), nullable=True))


def downgrade() -> None:
    """Drop the added columns and restore the previous status CHECK."""
    with op.batch_alter_table("operations") as batch:
        batch.drop_column("abort_reason")
    # Rows in the new state would violate the restored CHECK.
    op.execute(
        sa.text("UPDATE agent_runs SET status = 'aborted' WHERE status = 'awaiting_approval'")
    )
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("negotiation_rounds_used")
        batch.drop_column("coverage")
        batch.drop_column("notice_code")
        batch.drop_column("failure")
        batch.drop_column("flow_proposals")
        batch.drop_column("approvals")
        batch.drop_column("current_phase")
        batch.drop_column("llm_profile_id")
        batch.drop_constraint(_CHECK, type_="check")
        batch.create_check_constraint(_CHECK, _status_check(_OLD_STATUSES))
