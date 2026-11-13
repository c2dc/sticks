"""curated_seed ingest source (task 24.2)

Migration M6 of the ``agentic-only-flow-redesign`` spec. Chained onto
``c5d6e7f8a9b0`` (M5), the previous head.

The curated cases of a fresh installation come from versioned, read-only seed
JSON files (``app/seeds/data/<slug>.json``, loaded through ``importlib.resources``)
applied by the new ``curated_seed`` ingestion source, so:

* ``upgrade`` widens the ``ck_ingest_source_state_source`` CHECK to admit
  ``curated_seed`` and seeds its ``pending`` row in ``ingest_source_state``.
  Existing rows are untouched.
* ``downgrade`` deletes the ``curated_seed`` state row and restores the previous
  CHECK, the exact inverse of ``upgrade``.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-11-03 01:00:00.000000+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK = "ck_ingest_source_state_source"
_OLD_SOURCES: tuple[str, ...] = ("atomic", "docker_catalog", "attack_flow", "stix_packages")
_NEW_SOURCES: tuple[str, ...] = (*_OLD_SOURCES, "curated_seed")
_NEW_SOURCE = "curated_seed"


def _in_list(column: str, values: Sequence[str]) -> str:
    """Render ``column IN ('v1', ...)`` (fixed internal literals, no user input)."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_source_check(values: Sequence[str]) -> None:
    with op.batch_alter_table("ingest_source_state") as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, _in_list("source", values))


def upgrade() -> None:
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
