"""create the technique_emulability and container_emulability tables (agentic-phase-model-refactor task 12.1)

Chained onto ``c0d1e2f3a4b5`` (``rename_agent_stage_to_semantic_values``, the
sibling task-2.1 migration of this same spec), which itself chains onto the
``create_provisioning_map`` head ``b9c0d1e2f3a4``. Task 12.1 of the
``agentic-phase-model-refactor`` spec landed the
:class:`app.models.domain.TechniqueEmulability` and
:class:`app.models.domain.ContainerEmulability` ORM models; this migration
brings the live schema in line with them.

What this migration does (Req. 6.1, 6.3)
----------------------------------------
* **Creates ``technique_emulability``** — the per-ATT&CK-technique
  container-emulability classification (Req. 6.1). ``technique_id`` is the
  logical primary key (the classification is shared across campaigns, so there
  is no foreign key). ``emulability`` is bounded by a portable
  ``CHECK (emulability IN ('container', 'dispensable', 'not_container'))`` — the
  VARCHAR + CHECK rendering of the :class:`TechniqueEmulabilityKind` enum
  persisted with ``native_enum=False`` by the ORM. ``rationale`` is an optional
  free-form note.
* **Creates ``container_emulability``** — the per-campaign emulability verdict
  derived by the Container_Emulability_Service (Req. 6.3). The single-column
  UNIQUE constraint ``(campaign_id)`` (named
  ``uq_container_emulability_campaign_id``, mirroring the ORM
  ``__table_args__``) is the idempotency key: re-deriving a campaign's
  emulability updates the existing row rather than duplicating it (upsert
  latest-wins), consistent with ``provisioning_map`` /
  ``provisioned_environment``. ``blocking_techniques`` is an optional JSON list
  of the technique ids that block container emulation (Req. 6.5).

Purely additive — no existing object touched (Req. 9.3)
-------------------------------------------------------
The migration only CREATEs two new tables; it moves no data, backfills nothing,
and alters no existing table, enum, CHECK or constraint. ``container_emulability``
carries no foreign key, matching the ORM model (campaign emulability is keyed by
``campaign_id`` with a UNIQUE idempotency constraint, not a relational FK).

Portability (product is PostgreSQL, tests run on ephemeral SQLite)
------------------------------------------------------------------
Only portable generic types are used — ``String`` / ``Text`` / ``Integer`` /
``Boolean`` / ``JSON`` — plus a portable ``CHECK (... IN (...))`` on the
emulability enum column, so the schema compiles identically on PostgreSQL (the
product database) and the SQLite file the unit tests exercise.
``create_table`` / ``drop_table`` are backend-portable as-is, following the
precedent of the prior migrations in this tree. No application code is imported
— the migration stays self-contained.

Symmetric downgrade (Req. 9.3)
------------------------------
``downgrade`` drops exactly the objects ``upgrade`` created — the
``container_emulability`` and ``technique_emulability`` tables (their UNIQUE and
CHECK constraints go with them) — so ``upgrade`` ∘ ``downgrade`` is a lossless
identity on the schema.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-10-27 02:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Value set of the TechniqueEmulabilityKind enum (VARCHAR + CHECK). Kept as a
# local literal so the migration stays self-contained (no application import),
# matching the precedent of the other enum-CHECK migrations in this tree.
_EMULABILITY_VALUES: tuple[str, ...] = ("container", "dispensable", "not_container")


def _quote_values(values: tuple[str, ...]) -> str:
    """Render ``values`` as a SQL IN-list of single-quoted literals."""
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # technique_emulability — per-ATT&CK-technique container-emulability class
    # (design Model ``technique_emulability``). ``technique_id`` is the logical
    # PK; no FK (the classification is shared across campaigns). ``emulability``
    # is bounded by a portable CHECK to the TechniqueEmulabilityKind value set.
    op.create_table(
        "technique_emulability",
        sa.Column("technique_id", sa.String(), nullable=False),
        sa.Column("emulability", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("technique_id"),
        sa.CheckConstraint(
            f"emulability IN ({_quote_values(_EMULABILITY_VALUES)})",
            name="ck_technique_emulability_kind",
        ),
    )

    # container_emulability — per-campaign emulability verdict (design Model
    # ``container_emulability``). No FK: keyed by ``campaign_id`` with a
    # single-column UNIQUE idempotency constraint (one row per campaign, upsert
    # latest-wins). ``blocking_techniques`` is an optional JSON list of the
    # technique ids that block container emulation.
    op.create_table(
        "container_emulability",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("container_emulable", sa.Boolean(), nullable=False),
        sa.Column("blocking_techniques", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            name="uq_container_emulability_campaign_id",
        ),
    )


def downgrade() -> None:
    # Exact inverse of ``upgrade``: drop the two tables (their UNIQUE and CHECK
    # constraints are dropped with them). Dropped in reverse creation order.
    op.drop_table("container_emulability")
    op.drop_table("technique_emulability")
