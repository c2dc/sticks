"""create the image_catalog table (stix-db-single-source-ai-pipeline task 9.2)

Chained onto ``f7a8b9c0d1e2`` (``create_technique_commands``), the current head.
This is the second of the three ``Command_Base``/provisioning table migrations
of Phase 3.2 of the ``stix-db-single-source-ai-pipeline`` spec: task 9.2 landed
the :class:`app.models.domain.ImageCatalog` ORM model; this migration brings the
live schema in line with it.

What this migration does (Req. 14.4)
------------------------------------
* **Creates ``image_catalog``** — the offline fallback catalog for the
  ``ImageResolver``: when Docker Hub is unreachable, the Environment agent
  resolves a container image for a campaign's ``target_os`` (``linux`` |
  ``windows``) from this table instead of the live registry. The table has no
  foreign key — it is keyed by (image, target OS), not by a campaign, so the
  catalog is shared across campaigns.
* **Enforces the idempotency key.** The composite UNIQUE constraint
  ``(image, target_os)`` (named ``uq_image_catalog_image_target_os``, mirroring
  the ORM ``__table_args__``) makes re-seeding the same image for the same
  target OS reuse the existing row rather than duplicate it.

Purely additive — no existing object touched (Req. 9.4)
-------------------------------------------------------
The migration only CREATEs one new table; it moves no data, backfills nothing,
and alters no existing table, enum, CHECK or constraint.

Portability (product is PostgreSQL, tests run on ephemeral SQLite)
------------------------------------------------------------------
Only portable generic types are used — ``String`` / ``Text`` / ``Integer`` /
``JSON`` / ``DateTime`` — so the schema compiles identically on PostgreSQL (the
product database) and the SQLite file the integration test (task 9.4)
exercises. ``create_table`` / ``drop_table`` are backend-portable as-is,
following the precedent of the prior migrations in this tree. No application
code is imported — the migration stays self-contained.

Symmetric downgrade (Req. 9.4)
------------------------------
``downgrade`` drops exactly the object ``upgrade`` created — the
``image_catalog`` table (its UNIQUE constraint goes with it) — so ``upgrade`` ∘
``downgrade`` is a lossless identity on the schema.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-10-25 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # image_catalog — the offline fallback catalog for the ImageResolver: a
    # container image offered for a given target OS (design Model
    # ``image_catalog``). No FK: keyed by (image, target_os) (not a campaign),
    # so the catalog is shared across campaigns. The composite UNIQUE
    # (image, target_os) is the idempotency key.
    op.create_table(
        "image_catalog",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("image", sa.String(), nullable=False),
        sa.Column("target_os", sa.String(), nullable=False),
        sa.Column("services", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "image",
            "target_os",
            name="uq_image_catalog_image_target_os",
        ),
    )


def downgrade() -> None:
    # Exact inverse of ``upgrade``: drop the table (its UNIQUE constraint is
    # dropped with it).
    op.drop_table("image_catalog")
