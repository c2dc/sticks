"""create the six STIX package persistence tables (stix-package-persistence-and-overview task 1.2)

Chained onto ``c3d4e5f6a7b8``
(``add_agent_stage_traces_and_agent_run_error``), the current head. This
migration lands the schema half of the ``stix-package-persistence-and-overview``
spec: task 1.1 defined the six ORM models in ``app/models/domain.py``
(:class:`StixPackage`, :class:`StixTypeCount`, :class:`StixCampaign`,
:class:`StixCampaignTechnique`, :class:`StixActor` and
:class:`StixActorCampaign`); this task 1.2 brings the live schema in line with
that ORM so a downloaded STIX base is persisted once and the overview/campaign
views are served from the database instead of reparsing the bundle.

What this migration does (Req. 10.1-10.6, 11.3)
-----------------------------------------------
* **Creates the six tables in dependency order (parents before children).**
  ``stix_packages`` (the root) first, then its direct children
  ``stix_type_counts``, ``stix_campaigns`` and ``stix_actors``, then the
  grandchildren ``stix_campaign_techniques`` (child of ``stix_campaigns``) and
  ``stix_actor_campaigns`` (child of both ``stix_actors`` and
  ``stix_campaigns``). Each ``CREATE TABLE`` only references tables created
  before it, so the foreign keys always resolve (Req. 10.1, 10.5).
* **Enforces the idempotency key.** ``stix_packages.version`` carries a UNIQUE
  constraint (NOT NULL), the stable key that makes re-persisting the same
  version reuse the existing row rather than duplicate it (Req. 10.2, 10.5).
* **Mirrors the composite UNIQUE constraints of the ORM.**
  ``(package_id, stix_type)`` on ``stix_type_counts``, ``(package_id, stix_id)``
  on ``stix_campaigns`` and ``stix_actors``, ``(campaign_id, technique_id)`` on
  ``stix_campaign_techniques`` and ``(actor_id, campaign_id)`` on
  ``stix_actor_campaigns`` — the exact ``__table_args__`` names from the model
  (Req. 10.5).
* **Indexes every foreign key.** Each FK column gets its own btree index so the
  read service's joins/filters (overview counts per package, techniques/actors
  per campaign, attributions per actor) are index-backed rather than full
  scans (Req. 11.3). The indexes are named after the ``ix_{table}_{column}``
  convention.

Purely additive — no existing object touched (Req. 10.6)
--------------------------------------------------------
The migration only CREATEs six new tables plus their indexes; it moves no data,
backfills nothing, and alters no existing table, enum, CHECK or constraint. The
``pgvector`` extension is left exactly as the prior migration enabled it — no
``VECTOR`` column is created here (embeddings are a future phase; the
``stix_campaigns.description``/``aliases`` and
``stix_campaign_techniques.description`` columns are plain Text/JSON that only
prepare for that future).

Portability (product is PostgreSQL, tests run on ephemeral SQLite)
------------------------------------------------------------------
Only portable generic types are used — ``String`` / ``Text`` / ``Integer`` /
``Boolean`` / ``DateTime`` / ``JSON`` — so the schema compiles identically on
PostgreSQL (the product database) and the SQLite file the integration test
(task 1.3) exercises. ``create_table`` / ``create_index`` / ``drop_index`` /
``drop_table`` are backend-portable as-is, following the precedent of the prior
migrations in this tree. No application code is imported — the migration stays
self-contained.

Symmetric downgrade (Req. 10.4)
-------------------------------
``downgrade`` drops exactly the objects ``upgrade`` created — the FK indexes and
then the six tables in reverse dependency order (children before parents) — so
``upgrade`` ∘ ``downgrade`` is a lossless identity on the schema.

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a7b8
Create Date: 2026-10-22 01:00:00.000000+00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Foreign-key indexes (Req. 11.3). One btree index per FK column, named after
# the ``ix_{table}_{column}`` convention, so the read service's joins/filters
# are index-backed. Declared here so ``upgrade`` and ``downgrade`` stay exact
# inverses of one another.
# ---------------------------------------------------------------------------
_FK_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_stix_type_counts_package_id", "stix_type_counts", "package_id"),
    ("ix_stix_campaigns_package_id", "stix_campaigns", "package_id"),
    ("ix_stix_actors_package_id", "stix_actors", "package_id"),
    ("ix_stix_campaign_techniques_campaign_id", "stix_campaign_techniques", "campaign_id"),
    ("ix_stix_actor_campaigns_actor_id", "stix_actor_campaigns", "actor_id"),
    ("ix_stix_actor_campaigns_campaign_id", "stix_actor_campaigns", "campaign_id"),
)


def upgrade() -> None:
    # (1) stix_packages — the root row, one per downloaded STIX version
    # (design Model 1). ``version`` is UNIQUE + NOT NULL (the idempotency key),
    # ``name`` and ``downloaded_at`` are NOT NULL, ``source_file`` nullable.
    op.create_table(
        "stix_packages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(), nullable=False),
        sa.Column("source_file", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", name="uq_stix_packages_version"),
    )

    # (2) stix_type_counts — per-type object counts of a package (design Model
    # 2). FK ``package_id`` -> stix_packages.id; UNIQUE (package_id, stix_type).
    op.create_table(
        "stix_type_counts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("stix_type", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["package_id"], ["stix_packages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", "stix_type", name="uq_stix_type_counts_package_type"),
    )

    # (3) stix_campaigns — one campaign of the bundle (design Model 3). FK
    # ``package_id`` -> stix_packages.id; UNIQUE (package_id, stix_id).
    # ``description`` (Text) and ``aliases`` (JSON) only prepare for future
    # embeddings — no VECTOR column is created.
    op.create_table(
        "stix_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("stix_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("curated", sa.Boolean(), nullable=False),
        sa.Column("curated_slug", sa.String(), nullable=True),
        sa.Column("first_seen", sa.String(), nullable=True),
        sa.Column("last_seen", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["package_id"], ["stix_packages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", "stix_id", name="uq_stix_campaigns_package_stix_id"),
    )

    # (4) stix_actors — one intrusion-set of the bundle (design Model 5). FK
    # ``package_id`` -> stix_packages.id; UNIQUE (package_id, stix_id).
    op.create_table(
        "stix_actors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("stix_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["package_id"], ["stix_packages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", "stix_id", name="uq_stix_actors_package_stix_id"),
    )

    # (5) stix_campaign_techniques — ATT&CK techniques a campaign uses (design
    # Model 4). FK ``campaign_id`` -> stix_campaigns.id; UNIQUE
    # (campaign_id, technique_id). ``description`` (Text) prepares for future
    # embeddings.
    op.create_table(
        "stix_campaign_techniques",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("technique_id", sa.String(), nullable=False),
        sa.Column("technique_name", sa.String(), nullable=True),
        sa.Column("tactic", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["stix_campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "technique_id",
            name="uq_stix_campaign_techniques_campaign_technique",
        ),
    )

    # (6) stix_actor_campaigns — actor->campaign attribution (design Model 6).
    # FKs ``actor_id`` -> stix_actors.id and ``campaign_id`` -> stix_campaigns.id;
    # UNIQUE (actor_id, campaign_id).
    op.create_table(
        "stix_actor_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["stix_actors.id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["stix_campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_id",
            "campaign_id",
            name="uq_stix_actor_campaigns_actor_campaign",
        ),
    )

    # (7) Index every foreign key so the read service's joins/filters are
    # index-backed (Req. 11.3). Created after all tables exist.
    for index_name, table_name, column_name in _FK_INDEXES:
        op.create_index(index_name, table_name, [column_name])


def downgrade() -> None:
    # Exact inverse of ``upgrade``. (1) Drop the FK indexes first (they are the
    # last objects created).
    for index_name, table_name, _column_name in _FK_INDEXES:
        op.drop_index(index_name, table_name=table_name)

    # (2) Drop the six tables in reverse dependency order (children before
    # parents) — the exact inverse of the create order in upgrade.
    op.drop_table("stix_actor_campaigns")
    op.drop_table("stix_campaign_techniques")
    op.drop_table("stix_actors")
    op.drop_table("stix_campaigns")
    op.drop_table("stix_type_counts")
    op.drop_table("stix_packages")
