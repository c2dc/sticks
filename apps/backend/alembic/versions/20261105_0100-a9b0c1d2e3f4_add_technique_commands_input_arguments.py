"""add technique_commands.input_arguments (task 44.1)

Migration M9 of the ``agentic-only-flow-redesign`` spec. Chained onto
``f8a9b0c1d2e3`` (M8), the previous head.

Purely additive (Req. 11.1, 11.2): adds a single nullable JSON column that holds
the Atomic Red Team ``input_arguments`` map of each command. No data is deleted
or rewritten; existing rows get ``NULL`` until the Atomic re-ingest fills them.

``downgrade`` drops the column.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-11-05 01:00:00.000000+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "technique_commands"
_COLUMN = "input_arguments"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
