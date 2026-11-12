"""rename dag columns to flow (agentic-only-flow-redesign tasks 18.1 and 18.5)

Migration M3 of the ``agentic-only-flow-redesign`` spec. Chained onto
``f2a3b4c5d6e7`` (M2), the previous head.

``upgrade`` renames ``agent_stage_outputs.dag_id`` to ``flow_id`` inside a
``batch_alter_table`` block. The column keeps its type, nullability, data and
its foreign key to ``attack_flows.id``. The table has no secondary indexes.

The FK was created unnamed by ``a1b2c3d4e5f6``. On PostgreSQL it carries a
backend-generated name that embeds the old column name, so it is dropped and
re-created as ``fk_agent_stage_outputs_flow_id_attack_flows`` in the same
migration. On SQLite the FK is anonymous and travels with the batch table
rebuild, which rewrites its constrained column to the new name.

It also renames ``campaigns.dag_file`` to ``flow_file`` (type, nullability and
data preserved; the column has no index, FK or constraint).

``downgrade`` is the exact inverse: the columns go back to ``dag_id`` and
``dag_file`` and, on PostgreSQL, the FK goes back to the default generated
name. No data is lost in either direction.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-10-30 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "agent_stage_outputs"
_OLD_COLUMN = "dag_id"
_NEW_COLUMN = "flow_id"
_REFERRED_TABLE = "attack_flows"

_CAMPAIGNS_TABLE = "campaigns"
_OLD_CAMPAIGN_FILE = "dag_file"
_NEW_CAMPAIGN_FILE = "flow_file"

# PostgreSQL default name of the unnamed FK created by ``a1b2c3d4e5f6``.
_FK_OLD_NAME = "agent_stage_outputs_dag_id_fkey"
_FK_NEW_NAME = "fk_agent_stage_outputs_flow_id_attack_flows"


def _find_fk_name(column: str) -> Optional[str]:
    """Return the reflected name of the FK constrained on ``column`` (or None)."""
    inspector = sa.inspect(op.get_bind())
    for fk in inspector.get_foreign_keys(_TABLE):
        if fk.get("constrained_columns") == [column]:
            return fk.get("name")
    return None


def _rename(old: str, new: str, old_fk: str, new_fk: str) -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"
    existing_fk = _find_fk_name(old) if is_postgres else None
    with op.batch_alter_table(_TABLE) as batch_op:
        if existing_fk:
            batch_op.drop_constraint(existing_fk, type_="foreignkey")
        batch_op.alter_column(old, new_column_name=new)
    if is_postgres:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_foreign_key(new_fk, _REFERRED_TABLE, [new], ["id"])


def _rename_campaign_file(old: str, new: str) -> None:
    with op.batch_alter_table(_CAMPAIGNS_TABLE) as batch_op:
        batch_op.alter_column(old, new_column_name=new)


def upgrade() -> None:
    _rename(_OLD_COLUMN, _NEW_COLUMN, _FK_OLD_NAME, _FK_NEW_NAME)
    _rename_campaign_file(_OLD_CAMPAIGN_FILE, _NEW_CAMPAIGN_FILE)


def downgrade() -> None:
    _rename_campaign_file(_NEW_CAMPAIGN_FILE, _OLD_CAMPAIGN_FILE)
    _rename(_NEW_COLUMN, _OLD_COLUMN, _FK_NEW_NAME, _FK_OLD_NAME)
