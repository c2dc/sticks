"""add additive enrichment columns to ability_results (Stage 6, Req. 2.4)

Chained onto ``2d8cb3e55bb7`` (``rename_portuguese_schema_to_english``), the
current head. This migration implements the design's "``AbilityResult``
enriquecido" section of the ``stage6-execution-redesign`` spec: for the live
technique-by-technique tracking of Stage 6 (Req. 2.4), the per-Ability result
gains three **additive, nullable** columns mapped by
``OperationRunner._reconcile`` from the Caldera ``OperationLink`` (``command``,
``paw`` and ``output``):

* ``command`` (``Text``) — the concrete command of the link.
* ``target_container`` (``String``) — e.g. ``"nginx (192.168.20.30)"``.
* ``attacker_host`` (``String``) — the attacker host (e.g. ``kali``), via
  ``link.paw`` / the agent.

Decisions (following the precedent of the prior migrations in this tree):

* **Purely additive.** The three columns are ``nullable=True`` with no server
  default, so no data is moved and existing rows need no backfill — the upgrade
  only adds columns, the downgrade only drops them.
* **Portability.** The product database is PostgreSQL; the unit tests run
  against an ephemeral SQLite file. SQLite before 3.35 cannot ``DROP COLUMN``
  and has limited ``ALTER TABLE``, so both ``upgrade`` and ``downgrade`` run
  inside ``op.batch_alter_table`` (a thin ``ALTER`` on PostgreSQL; a table
  rebuild on SQLite — ``render_as_batch`` is enabled for SQLite in ``env.py``).
* **Symmetric downgrade.** ``downgrade`` drops exactly the three columns added,
  so ``upgrade`` ∘ ``downgrade`` is a lossless identity on the schema.

Revision ID: b7f4a9c21d3e
Revises: 2d8cb3e55bb7
Create Date: 2026-10-16 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7f4a9c21d3e"
down_revision: Union[str, None] = "2d8cb3e55bb7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The three additive, nullable enrichment columns (Req. 2.4). ``command`` is a
# portable TEXT (like ``command_output``); the other two are portable VARCHAR.
# Column objects are built fresh per operation (not reused) to stay compatible
# with SQLAlchemy 2.0, which rejects re-adding an already-attached Column.
_NEW_COLUMN_SPECS: list[tuple[str, sa.types.TypeEngine]] = [
    ("command", sa.Text()),
    ("target_container", sa.String()),
    ("attacker_host", sa.String()),
]


def upgrade() -> None:
    # Add the three nullable columns in one batch block (portable on both
    # PostgreSQL and the test SQLite). Purely additive: existing rows get NULLs.
    with op.batch_alter_table("ability_results") as batch_op:
        for name, column_type in _NEW_COLUMN_SPECS:
            batch_op.add_column(sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    # Exact inverse of ``upgrade``: drop the three columns added above.
    with op.batch_alter_table("ability_results") as batch_op:
        for name, _column_type in reversed(_NEW_COLUMN_SPECS):
            batch_op.drop_column(name)
