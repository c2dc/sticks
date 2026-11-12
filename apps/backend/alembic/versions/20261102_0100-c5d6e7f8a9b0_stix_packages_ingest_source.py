"""stix_packages ingest source and drop stix_packages.source_file (task 21.3)

Migration M5 of the ``agentic-only-flow-redesign`` spec. Chained onto
``b4c5d6e7f8a9`` (M4), the previous head.

The user decision is firm: no information lives in files on disk, the database is
the single source of truth. The STIX bundle is now downloaded in memory and
persisted by the ``stix_packages`` ingestion source, so:

* ``upgrade`` drops ``stix_packages.source_file`` (it only stored the on-disk
  bundle path), widens the ``ck_ingest_source_state_source`` CHECK to admit the
  new ``stix_packages`` source and seeds its ``pending`` row in
  ``ingest_source_state``. Existing rows are untouched.
* ``downgrade`` deletes the ``stix_packages`` state row, restores the previous
  CHECK and re-creates ``source_file`` as a nullable ``String`` (the dropped path
  values are not recoverable, which is acceptable: they were informative only).

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-11-02 01:00:00.000000+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK = "ck_ingest_source_state_source"
_OLD_SOURCES: tuple[str, ...] = ("atomic", "docker_catalog", "attack_flow")
_NEW_SOURCES: tuple[str, ...] = _OLD_SOURCES + ("stix_packages",)
_NEW_SOURCE = "stix_packages"


def _in_list(column: str, values: Sequence[str]) -> str:
    """Render ``column IN ('v1', ...)`` (fixed internal literals, no user input)."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_source_check(values: Sequence[str]) -> None:
    with op.batch_alter_table("ingest_source_state") as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, _in_list("source", values))


def upgrade() -> None:
    with op.batch_alter_table("stix_packages") as batch_op:
        batch_op.drop_column("source_file")

    _replace_source_check(_NEW_SOURCES)
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT 1 FROM ingest_source_state WHERE source = :source"),
        {"source": _NEW_SOURCE},
    ).first()
    if exists is None:
        bind.execute(
            sa.text("INSERT INTO ingest_source_state (source, status) VALUES (:source, 'pending')"),
            {"source": _NEW_SOURCE},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM ingest_source_state WHERE source = :source"),
        {"source": _NEW_SOURCE},
    )
    _replace_source_check(_OLD_SOURCES)

    with op.batch_alter_table("stix_packages") as batch_op:
        batch_op.add_column(sa.Column("source_file", sa.String(), nullable=True))
