"""drop provably dead ORM columns and the dead provisioned_environment table (task 30.3)

Migration M7 of the ``agentic-only-flow-redesign`` spec. Chained onto
``d6e7f8a9b0c1`` (M6), the previous head.

The 30.1 audit proved there is no live reader and no live writer for:

* ``stage_runs.chosen_flow_id`` (nullable FK to ``attack_flows.id``),
* ``stix_versions.curated_artifacts_path`` (nullable ``String``),
* ``technique_emulability.rationale`` (nullable ``Text``),
* the whole ``provisioned_environment`` table (no ORM use, no FK pointing at it).

``stix_versions`` itself is kept: it holds the seeded ``18.0`` row, and the rule
for this task is to drop a table only when it is empty.

``upgrade`` drops the three columns inside ``batch_alter_table`` blocks
(portable to SQLite) and drops ``provisioned_environment``. As a safeguard it
refuses to drop ``provisioned_environment`` if it unexpectedly holds rows, so no
data can be lost silently.

``downgrade`` re-creates exactly what was removed: the three columns as nullable
with their original types (plus the named FK on ``stage_runs.chosen_flow_id``)
and the ``provisioned_environment`` table with its original schema (PK, FK to
``campaigns.id``, same column types). Dropped column values are not recoverable.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-11-04 01:00:00.000000+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK_NAME = "fk_stage_runs_chosen_flow_id_attack_flows"
_DEAD_TABLE = "provisioned_environment"


def upgrade() -> None:
    bind = op.get_bind()
    row_count = bind.execute(sa.text(f"SELECT count(*) FROM {_DEAD_TABLE}")).scalar()
    if row_count:
        raise RuntimeError(
            f"refusing to drop {_DEAD_TABLE}: it holds {row_count} row(s)"
        )

    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.drop_column("chosen_flow_id")
    with op.batch_alter_table("stix_versions") as batch_op:
        batch_op.drop_column("curated_artifacts_path")
    with op.batch_alter_table("technique_emulability") as batch_op:
        batch_op.drop_column("rationale")
    op.drop_table(_DEAD_TABLE)


def downgrade() -> None:
    op.create_table(
        _DEAD_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("platform", sa.String(length=15), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(length=14), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("technique_emulability") as batch_op:
        batch_op.add_column(sa.Column("rationale", sa.Text(), nullable=True))
    with op.batch_alter_table("stix_versions") as batch_op:
        batch_op.add_column(
            sa.Column("curated_artifacts_path", sa.String(), nullable=True)
        )
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.add_column(sa.Column("chosen_flow_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.create_foreign_key(_FK_NAME, "attack_flows", ["chosen_flow_id"], ["id"])
