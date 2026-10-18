"""widen operations.state to admit incompleta_timeout (Stage 6, Req. 3.2)

Chained onto ``b7f4a9c21d3e`` (``add_ability_result_enrichment_columns``), the
current head. This migration implements the schema half of the Stage 6
``stage6-execution-redesign`` spec change that introduces the new terminal
state ``OperationState.INCOMPLETE_TIMEOUT = "incompleta_timeout"`` (task 1.1,
Req. 3.2): when the incremental collection loop times out without reaching a
Caldera terminal state, the Operation is persisted as ``incompleta_timeout``
(partials preserved, not counted as success).

What the "CHECK" actually is in this codebase
---------------------------------------------
``operations.state`` is mapped with ``_portable_enum(OperationState,
"operation_state")`` — a SQLAlchemy ``Enum`` with ``native_enum=False``. The
project runs **SQLAlchemy 2.1**, where ``Enum.create_constraint`` defaults to
``False``: a ``native_enum=False`` Enum emits **no** ``CHECK`` constraint on
either PostgreSQL or the test SQLite. It compiles to a bare, length-bounded
``VARCHAR(n)`` where ``n`` is the length of the longest member value, and the
value set is enforced in the application layer by ``validate_strings=True``.

Consequently there is no ``CHECK`` to relax. The *only* physical-schema effect
of adding the longer member ``"incompleta_timeout"`` (18 chars) is that the
column's ``VARCHAR`` bound must grow from the historical ``VARCHAR(12)``
(longest of the original four values, ``"nao_iniciada"``, set by migration
``a28d8c1ae4aa``) to ``VARCHAR(18)``. On PostgreSQL a ``VARCHAR(12)`` column
would reject/truncate the 18-char value; widening the length is what lets the
new value be stored. This migration therefore widens the column type to the
value-set the ORM now expects, which is the additive, forward-compatible
equivalent of "widen the operation_state check".

Decisions (following the precedent of the prior migrations in this tree):

* **Purely additive — no row changes.** Growing a ``VARCHAR`` bound only
  relaxes it; every existing row already holds one of the four historical
  values and stays valid. No data is read, moved, or rewritten.
* **Portability.** The column type is re-stated inside ``op.batch_alter_table``
  so SQLite rebuilds the table with the new column type and PostgreSQL emits a
  plain ``ALTER COLUMN ... TYPE``. ``render_as_batch`` is enabled for SQLite in
  ``env.py``. The type is built from the same ``_portable_enum`` the ORM uses,
  so upgrade leaves the physical schema matching the model exactly.
* **Symmetric downgrade.** ``downgrade`` re-states the column with the four
  original values (``VARCHAR(12)``), so ``upgrade`` ∘ ``downgrade`` is a
  lossless identity on the schema. (Narrowing would only ever lose data if a
  row stored ``incompleta_timeout`` — the expected behavior of narrowing a
  bound and consistent with a clean additive/reversible contract.)

Revision ID: c9d2e4f8a1b7
Revises: b7f4a9c21d3e
Create Date: 2026-10-17 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c9d2e4f8a1b7"
down_revision: Union[str, None] = "b7f4a9c21d3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The enum ``name`` is kept stable ("operation_state") so, were a backend to
# render a CHECK (e.g. a future SQLAlchemy default change), it would target the
# same constraint the column already has.
_ENUM_NAME = "operation_state"

# Historical value set (migration a28d8c1ae4aa) → longest value is 12 chars.
_OLD_VALUES: tuple[str, ...] = (
    "nao_iniciada",
    "em_execucao",
    "finalizada",
    "abortada",
)

# Widened value set: the four historical values plus the new terminal state
# (``incompleta_timeout`` is 18 chars → VARCHAR grows 12 → 18).
_NEW_VALUES: tuple[str, ...] = _OLD_VALUES + ("incompleta_timeout",)


def _enum_type(values: tuple[str, ...]) -> sa.Enum:
    """Build the portable (``native_enum=False``) Enum type for ``state``.

    Mirrors ``_portable_enum`` in ``app/models/domain.py`` so the re-stated
    column type matches the ORM exactly on both backends: a length-bounded
    VARCHAR (no native enum type) whose bound is the longest value in ``values``.
    """
    return sa.Enum(
        *values,
        name=_ENUM_NAME,
        native_enum=False,
        validate_strings=True,
    )


def _restate_state_type(
    old_values: tuple[str, ...], new_values: tuple[str, ...]
) -> None:
    """Re-state the ``operations.state`` column type to the ``new_values`` set.

    Runs inside a batch block so SQLite rebuilds the table with the new column
    type and PostgreSQL emits ``ALTER COLUMN ... TYPE``. Purely additive on the
    data: no row is touched, only the column type (VARCHAR bound) changes.
    """
    with op.batch_alter_table("operations") as batch_op:
        batch_op.alter_column(
            "state",
            existing_type=_enum_type(old_values),
            type_=_enum_type(new_values),
            existing_nullable=False,
        )


def upgrade() -> None:
    # Widen: 4 historical values (VARCHAR(12)) -> + incompleta_timeout
    # (VARCHAR(18)). No row changes.
    _restate_state_type(_OLD_VALUES, _NEW_VALUES)


def downgrade() -> None:
    # Exact inverse of ``upgrade``: narrow back to the four historical values.
    _restate_state_type(_NEW_VALUES, _OLD_VALUES)
