"""add teardown columns to execution_reports (Stage 6, Req. 7.3)

Chained onto ``c9d2e4f8a1b7``
(``widen_operation_state_check_incomplete_timeout``), the current head. This
migration implements the schema half of the ``stage6-screen-redesign`` spec's
teardown persistence (task 1.1 / 1.2, Req. 7.3): the Stage 6 report gains a
structured record of the ``DeprovisioningResult`` produced at completion so the
``GET /report`` payload can expose whether the environment was removed and, on
failure, which resources remained.

Two **additive, nullable** columns are added to ``execution_reports``, mirroring
the ORM in ``app/models/domain.py``:

* ``deprovisioned`` (``Boolean``) — whether the environment was torn down.
* ``remaining_resources`` (``JSON``) — the resources still present when the
  teardown failed (portable JSON column, like ``metrics``).

Both are ``nullable=True`` with no server default, so legacy/pre-teardown rows
(and reports persisted before the environment was torn down) stay valid without
a backfill. The legacy boolean ``environment_deprovisioned`` is left untouched.

Decisions (following the precedent of the prior migrations in this tree):

* **Purely additive.** The two columns are ``nullable=True`` with no server
  default, so no data is moved and existing rows need no backfill — the upgrade
  only adds columns, the downgrade only drops them.
* **Portability.** The product database is PostgreSQL; the unit tests run
  against an ephemeral SQLite file. SQLite before 3.35 cannot ``DROP COLUMN``
  and has limited ``ALTER TABLE``, so both ``upgrade`` and ``downgrade`` run
  inside ``op.batch_alter_table`` (a thin ``ALTER`` on PostgreSQL; a table
  rebuild on SQLite — ``render_as_batch`` is enabled for SQLite in ``env.py``).
* **Symmetric downgrade.** ``downgrade`` drops exactly the two columns added,
  so ``upgrade`` ∘ ``downgrade`` is a lossless identity on the schema.

Revision ID: d4e1f6a2c3b9
Revises: c9d2e4f8a1b7
Create Date: 2026-10-18 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e1f6a2c3b9"
down_revision: Union[str, None] = "c9d2e4f8a1b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The two additive, nullable teardown columns (Req. 7.3). ``remaining_resources``
# is a portable JSON column (like ``metrics``). Column objects are built fresh
# per operation (not reused) to stay compatible with SQLAlchemy 2.x, which
# rejects re-adding an already-attached Column.
_NEW_COLUMN_SPECS: list[tuple[str, sa.types.TypeEngine]] = [
    ("deprovisioned", sa.Boolean()),
    ("remaining_resources", sa.JSON()),
]


def upgrade() -> None:
    # Add the two nullable columns in one batch block (portable on both
    # PostgreSQL and the test SQLite). Purely additive: existing rows get NULLs.
    with op.batch_alter_table("execution_reports") as batch_op:
        for name, column_type in _NEW_COLUMN_SPECS:
            batch_op.add_column(sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    # Exact inverse of ``upgrade``: drop the two columns added above.
    with op.batch_alter_table("execution_reports") as batch_op:
        for name, _column_type in reversed(_NEW_COLUMN_SPECS):
            batch_op.drop_column(name)
