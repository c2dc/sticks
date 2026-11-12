"""drop the dead stix_versions table (task 32.1)

Migration M8 of the ``agentic-only-flow-redesign`` spec. Chained onto
``e7f8a9b0c1d2`` (M7), the previous head.

The 30.3 audit proved there is no live reader and no live writer of
``stix_versions`` (the curated flag lives in code as
``app.services.stix.stix_service.CURATED_VERSION`` and in
``stix_packages``/``stix_campaigns``), and no foreign key points at it. The user
decided to drop it.

``upgrade`` drops the table. As a safeguard it refuses to run if the table holds
anything other than the single seeded ``18.0`` row (``curated = true``), so no
unexpected data can be lost silently.

``downgrade`` re-creates the table exactly as it was after M7 (``version``
``String`` primary key, ``curated`` ``Boolean`` NOT NULL; the
``curated_artifacts_path`` column was already removed by M7) and re-inserts the
seed row ``('18.0', true)`` created by ``e907e49a84c5``.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-11-04 02:00:00.000000+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "stix_versions"
_PK_NAME = "versoes_stix_pkey"
# Seed row inserted by ``e907e49a84c5`` (replicated as a literal so the migration
# does not import application code).
_SEED_VERSION = "18.0"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT version, curated FROM {_TABLE}")).all()
    unexpected = [r for r in rows if not (r[0] == _SEED_VERSION and bool(r[1]))]
    if unexpected:
        raise RuntimeError(
            f"refusing to drop {_TABLE}: it holds {len(unexpected)} row(s) other "
            f"than the seeded {_SEED_VERSION!r} row"
        )
    op.drop_table(_TABLE)


def downgrade() -> None:
    table = op.create_table(
        _TABLE,
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("curated", sa.Boolean(), nullable=False),
        # PostgreSQL kept the PK index name from the pre-rename table
        # (``versoes_stix``) when ``2d8cb3e55bb7`` renamed it; keep that name so
        # the restored schema is identical to the one M7 left behind.
        sa.PrimaryKeyConstraint("version", name=_PK_NAME),
    )
    op.bulk_insert(table, [{"version": _SEED_VERSION, "curated": True}])
