"""create the provisioning_map table (stix-db-single-source-ai-pipeline task 9.3)

Chained onto ``a8b9c0d1e2f3`` (``create_image_catalog``), the current head.
This is the third and last of the three ``Command_Base``/provisioning table
migrations of Phase 3.2 of the ``stix-db-single-source-ai-pipeline`` spec: task
9.3 landed the :class:`app.models.domain.ProvisioningMap` ORM model; this
migration brings the live schema in line with it.

What this migration does (Req. 8.2)
-----------------------------------
* **Creates ``provisioning_map``** — the recommended target environment per
  campaign: the fixed inventory (images/services/topology) derived from the
  techniques/platforms of the campaign's abilities (Req. 8.1, 8.2). Unlike the
  two preceding tables, this one DOES carry a foreign key — ``campaign_id`` →
  ``campaigns.id`` (NOT NULL) — because a provisioning map belongs to a single
  campaign (the same FK style as ``environment_parameters`` /
  ``provisioned_environment``).
* **Enforces the idempotency key.** The single-column UNIQUE constraint
  ``(campaign_id)`` (named ``uq_provisioning_map_campaign_id``, mirroring the
  ORM ``__table_args__``) makes re-deriving the map for a campaign update the
  existing row rather than duplicate it — one map per campaign (upsert
  latest-wins), consistent with ``provisioned_environment``.

Purely additive — no existing object touched (Req. 9.4)
-------------------------------------------------------
The migration only CREATEs one new table; it moves no data, backfills nothing,
and alters no existing table, enum, CHECK or constraint. The FK references the
pre-existing ``campaigns`` table.

Portability (product is PostgreSQL, tests run on ephemeral SQLite)
------------------------------------------------------------------
Only portable generic types are used — ``String`` / ``JSON`` / ``DateTime`` —
so the schema compiles identically on PostgreSQL (the product database) and the
SQLite file the integration test (task 9.4) exercises. ``create_table`` /
``drop_table`` are backend-portable as-is, following the precedent of the prior
migrations in this tree. No application code is imported — the migration stays
self-contained.

Symmetric downgrade (Req. 9.4)
------------------------------
``downgrade`` drops exactly the object ``upgrade`` created — the
``provisioning_map`` table (its FK and UNIQUE constraint go with it) — so
``upgrade`` ∘ ``downgrade`` is a lossless identity on the schema.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-10-26 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # provisioning_map — the recommended target environment per campaign: the
    # fixed inventory (images/services/topology) derived from the campaign's
    # abilities (design Model ``provisioning_map``). FK ``campaign_id`` ->
    # ``campaigns.id`` (NOT NULL): a map belongs to a single campaign. The
    # single-column UNIQUE (campaign_id) is the idempotency key — one map per
    # campaign (upsert latest-wins).
    op.create_table(
        "provisioning_map",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("target_os", sa.String(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_provisioning_map_campaign_id_campaigns",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            name="uq_provisioning_map_campaign_id",
        ),
    )


def downgrade() -> None:
    # Exact inverse of ``upgrade``: drop the table (its FK and UNIQUE constraint
    # are dropped with it).
    op.drop_table("provisioning_map")
