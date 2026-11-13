"""create attack flow corpus tables and ingest state (agentic-only-flow-redesign task 6.1)

Migration M2 of the ``agentic-only-flow-redesign`` spec. Chained onto
``e1f2a3b4c5d6`` (M1), the previous head. It only **adds** structure; no
existing row is rewritten.

What ``upgrade`` does
---------------------
1. Extends ``attack_flows`` with the corpus metadata columns ``source_key``,
   ``source_sha``, ``scope``, ``description``, ``has_cycles`` (NOT NULL, default
   false) and ``provenance`` (JSON). ``campaign_id`` becomes nullable because
   MITRE corpus flows do not belong to a Sticks campaign (Req. 4.1).
2. Installs the named CHECK ``ck_attack_flows_origin`` over
   ``human_curation, ai_generated, mitre_corpus``. ``dags.origin`` was a bare
   VARCHAR until now, so there is no previous CHECK to replace.
3. Creates the partial UNIQUE index ``uq_attack_flows_source_key`` over
   ``source_key WHERE source_key IS NOT NULL`` (idempotency key of the corpus).
4. Creates ``attack_flow_objects`` and ``attack_flow_edges`` (both cascade on
   the flow) and ``ingest_source_state``, seeded with one ``pending`` row per
   source (Req. 7.8).

``downgrade`` is the inverse: drops the three new tables, deletes the
``mitre_corpus`` flows (rebuildable by re-ingestion), then removes the index,
the CHECK and the new columns and restores ``campaign_id`` NOT NULL. Curated
and AI-generated flows are never touched.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-29 01:00:00.000000+00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ORIGIN_CHECK = "ck_attack_flows_origin"
_ORIGIN_VALUES: tuple[str, ...] = ("human_curation", "ai_generated", "mitre_corpus")
_SOURCE_INDEX = "uq_attack_flows_source_key"
_INGEST_SOURCES: tuple[str, ...] = ("atomic", "docker_catalog", "attack_flow")
_INGEST_STATUSES: tuple[str, ...] = ("pending", "running", "ready", "failed")


def _in_list(column: str, values: Sequence[str]) -> str:
    """Render ``column IN ('v1', ...)`` (fixed internal literals, no user input)."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # (1) + (2) attack_flows: new columns, nullable campaign_id, origin CHECK.
    with op.batch_alter_table("attack_flows") as batch_op:
        batch_op.add_column(sa.Column("source_key", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("source_sha", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("scope", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "has_cycles",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("provenance", sa.JSON(), nullable=True))
        batch_op.alter_column("campaign_id", existing_type=sa.String(), nullable=True)
        batch_op.create_check_constraint(_ORIGIN_CHECK, _in_list("origin", _ORIGIN_VALUES))

    # (3) Partial UNIQUE index: one corpus row per source file.
    op.create_index(
        _SOURCE_INDEX,
        "attack_flows",
        ["source_key"],
        unique=True,
        sqlite_where=sa.text("source_key IS NOT NULL"),
        postgresql_where=sa.text("source_key IS NOT NULL"),
    )

    # (4) New tables.
    op.create_table(
        "attack_flow_objects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("flow_id", sa.Integer(), nullable=False),
        sa.Column("stix_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("technique_id", sa.String(), nullable=True),
        sa.Column("tactic_id", sa.String(), nullable=True),
        sa.Column("properties", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["flow_id"],
            ["attack_flows.id"],
            name="fk_attack_flow_objects_flow_id_attack_flows",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("flow_id", "stix_id", name="uq_attack_flow_objects_flow_id_stix_id"),
    )
    op.create_index(
        "ix_attack_flow_objects_technique_id",
        "attack_flow_objects",
        ["technique_id"],
    )

    op.create_table(
        "attack_flow_edges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("flow_id", sa.Integer(), nullable=False),
        sa.Column("source_stix_id", sa.String(), nullable=False),
        sa.Column("target_stix_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "is_back_edge",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["flow_id"],
            ["attack_flows.id"],
            name="fk_attack_flow_edges_flow_id_attack_flows",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_attack_flow_edges_flow_id", "attack_flow_edges", ["flow_id"])

    ingest_state = op.create_table(
        "ingest_source_state",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("loaded_count", sa.Integer(), nullable=True),
        sa.Column("embedded_count", sa.Integer(), nullable=True),
        sa.Column("expected_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("skipped", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("source"),
        sa.CheckConstraint(
            _in_list("source", _INGEST_SOURCES), name="ck_ingest_source_state_source"
        ),
        sa.CheckConstraint(
            _in_list("status", _INGEST_STATUSES), name="ck_ingest_source_state_status"
        ),
    )
    op.bulk_insert(
        ingest_state,
        [{"source": s, "status": "pending"} for s in _INGEST_SOURCES],
    )


def downgrade() -> None:
    # (4') Drop the new tables (indexes and constraints go with them).
    op.drop_table("ingest_source_state")
    op.drop_index("ix_attack_flow_edges_flow_id", table_name="attack_flow_edges")
    op.drop_table("attack_flow_edges")
    op.drop_index("ix_attack_flow_objects_technique_id", table_name="attack_flow_objects")
    op.drop_table("attack_flow_objects")

    # Corpus rows are rebuildable by re-ingestion; detach any run pointing at one.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE stage_runs SET chosen_flow_id = NULL WHERE chosen_flow_id IN "
            "(SELECT id FROM attack_flows WHERE origin = :origin)"
        ),
        {"origin": "mitre_corpus"},
    )
    bind.execute(
        sa.text("DELETE FROM attack_flows WHERE origin = :origin"),
        {"origin": "mitre_corpus"},
    )

    # (3') Index, then (1')/(2') CHECK, columns and NOT NULL.
    op.drop_index(_SOURCE_INDEX, table_name="attack_flows")
    with op.batch_alter_table("attack_flows") as batch_op:
        batch_op.drop_constraint(_ORIGIN_CHECK, type_="check")
        batch_op.alter_column("campaign_id", existing_type=sa.String(), nullable=False)
        batch_op.drop_column("provenance")
        batch_op.drop_column("has_cycles")
        batch_op.drop_column("description")
        batch_op.drop_column("scope")
        batch_op.drop_column("source_sha")
        batch_op.drop_column("source_key")
