"""drop campaigns file columns (agentic-only-flow-redesign task 21.2)

Migration M4 of the ``agentic-only-flow-redesign`` spec. Chained onto
``a3b4c5d6e7f8`` (M3), the previous head.

The user decision is firm: no information lives in JSON files on disk, the
database is the single source of truth. The ``campaigns.ability_file``,
``campaigns.adversary_file`` and ``campaigns.flow_file`` columns only stored the
on-disk paths of the removed curated JSON files, so ``upgrade`` drops them
inside a ``batch_alter_table`` block (portable to SQLite). The columns have no
index, FK or constraint.

``downgrade`` re-creates the three columns as nullable ``String`` (the original
type and nullability). The dropped path values are not recoverable, which is
acceptable: they were informative placeholders without runtime use.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-11-01 01:00:00.000000+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "campaigns"
_COLUMNS = ("ability_file", "adversary_file", "flow_file")


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for column in _COLUMNS:
            batch_op.drop_column(column)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for column in _COLUMNS:
            batch_op.add_column(sa.Column(column, sa.String(), nullable=True))
