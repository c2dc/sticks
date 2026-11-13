"""create the technique_commands table (stix-db-single-source-ai-pipeline task 9.1)

Chained onto ``e6f7a8b9c0d1``
(``align_experience_memory_embedding_dim_768``), the current head. This is the
first of the three ``Command_Base``/provisioning table migrations of Phase 3.2
of the ``stix-db-single-source-ai-pipeline`` spec: task 9.1 landed the
:class:`app.models.domain.TechniqueCommand` ORM model; this migration brings the
live schema in line with it.

What this migration does (Req. 7.2, 13.3)
-----------------------------------------
* **Creates ``technique_commands``** — the relational/exact half of the
  ``Command_Base``: the mapping from an ATT&CK ``technique_id`` to the concrete
  commands that exercise it (ingested from Atomic Red Team or threat-intel
  uploads). The table has no foreign key — it is keyed by the ATT&CK technique
  id, not by a campaign, so the mapping is shared across campaigns.
* **Enforces the idempotency key.** The composite UNIQUE constraint
  ``(technique_id, command_hash, source)`` (named
  ``uq_technique_commands_technique_hash_source``, mirroring the ORM
  ``__table_args__``) makes re-ingesting the same command for the same technique
  and source reuse the existing row rather than duplicate it (Req. 7.6, 13.6).

Purely additive — no existing object touched (Req. 9.4)
-------------------------------------------------------
The migration only CREATEs one new table; it moves no data, backfills nothing,
and alters no existing table, enum, CHECK or constraint.

Portability (product is PostgreSQL, tests run on ephemeral SQLite)
------------------------------------------------------------------
Only portable generic types are used — ``String`` / ``Text`` / ``Integer`` /
``DateTime`` — so the schema compiles identically on PostgreSQL (the product
database) and the SQLite file the integration test (task 9.4) exercises.
``create_table`` / ``drop_table`` are backend-portable as-is, following the
precedent of the prior migrations in this tree. No application code is imported
— the migration stays self-contained.

Symmetric downgrade (Req. 9.4)
------------------------------
``downgrade`` drops exactly the object ``upgrade`` created — the
``technique_commands`` table (its UNIQUE constraint goes with it) — so
``upgrade`` ∘ ``downgrade`` is a lossless identity on the schema.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-10-24 01:00:00.000000+00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # technique_commands — the relational/exact half of the Command_Base: the
    # ATT&CK technique -> concrete command mapping (design Model
    # ``technique_commands``). No FK: keyed by ``technique_id`` (not a campaign),
    # so the mapping is shared across campaigns. The composite UNIQUE
    # (technique_id, command_hash, source) is the idempotency key (Req. 7.6,
    # 13.6).
    op.create_table(
        "technique_commands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("technique_id", sa.String(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("executor", sa.String(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("command_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "technique_id",
            "command_hash",
            "source",
            name="uq_technique_commands_technique_hash_source",
        ),
    )


def downgrade() -> None:
    # Exact inverse of ``upgrade``: drop the table (its UNIQUE constraint is
    # dropped with it).
    op.drop_table("technique_commands")
