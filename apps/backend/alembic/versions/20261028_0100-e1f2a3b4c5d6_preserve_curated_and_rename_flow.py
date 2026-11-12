"""preserve curated cases as a flag and rename dag to flow (agentic-only-flow-redesign task 2.1)

Migration M1 of the ``agentic-only-flow-redesign`` spec. Chained onto
``d1e2f3a4b5c6`` (``create_technique_and_container_emulability``), the previous
head. It only **adds and renames**: it never deletes a row, so the curated data
of the 8 original cases (abilities, adversaries, flows, environment) survives
in both directions.

What ``upgrade`` does
---------------------
1. Adds ``campaigns.curated`` (Boolean, NOT NULL, ``server_default`` false) and
   backfills it with ``true`` only for campaigns that own at least one
   ``human_curation`` flow (``dags.origin = 'human_curation'``). This marks the
   8 curated cases without marking campaigns the AI materialized later, which
   have no human-curated flow (Req. 1.4, 5.4).
2. Renames the table ``dags`` to ``attack_flows`` and the column
   ``stage_runs.chosen_dag_id`` to ``chosen_flow_id``. The named foreign key
   ``fk_stage_runs_chosen_dag_id_dags`` is dropped and re-created under its new
   name (``fk_stage_runs_chosen_flow_id_attack_flows``) in the same batch block,
   following the ``2d8cb3e55bb7`` precedent (Req. 1.5, 5.2).
3. Regenerates the ``AgentStage`` CHECK on its three columns
   (``agent_iterations.refined_stage``, ``agent_stage_outputs.stage``,
   ``experience_memory.stage``) following the ``c0d1e2f3a4b5`` pattern: open
   (drop) the named CHECK, convert ``dag`` to ``flow`` with a parameterized
   UPDATE, and re-create the CHECK over
   ``operationalization, flow, environment, execution, evaluation``. A NULL
   ``refined_stage`` matches no UPDATE and stays valid (Req. 5.3).
4. The origin enum ``dag_origin`` becomes ``flow_origin``. ``dags.origin`` is a
   bare VARCHAR on this schema (no named CHECK, see ``b2c3d4e5f6a7``) and its
   value set (``human_curation`` / ``ai_generated``) is unchanged, so there is
   no physical object to rename; the enum name lives only in the ORM and is
   renamed there by task 4.1 (Req. 5.2).

``downgrade`` applies the exact inverse: ``flow`` back to ``dag`` with the
previous CHECK, the old table, column and foreign key names, and drops
``campaigns.curated`` (derivable, so discarding it loses nothing). No curated
row is touched in either direction (Req. 1.6, 5.5).

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-10-28 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# AgentStage value sets before and after this migration. The "after" order
# follows the design (flow replaces dag); the "before" order is the one
# installed by ``c0d1e2f3a4b5``.
_DAG_VALUES: tuple[str, ...] = (
    "dag",
    "operationalization",
    "environment",
    "execution",
    "evaluation",
)
_FLOW_VALUES: tuple[str, ...] = (
    "operationalization",
    "flow",
    "environment",
    "execution",
    "evaluation",
)

# (table, column, check_name) for the three AgentStage-backed columns.
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("agent_iterations", "refined_stage", "ck_agent_iterations_refined_stage"),
    ("agent_stage_outputs", "stage", "ck_agent_stage_outputs_stage"),
    ("experience_memory", "stage", "ck_experience_memory_stage"),
)

_FK_OLD_NAME = "fk_stage_runs_chosen_dag_id_dags"
_FK_NEW_NAME = "fk_stage_runs_chosen_flow_id_attack_flows"


def _check_expr(column: str, values: Sequence[str]) -> str:
    """Render ``column IN ('v1', ...)`` (fixed internal literals, no user input)."""
    in_list = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({in_list})"


def _regenerate(
    table: str,
    column: str,
    check_name: str,
    src: str,
    dst: str,
    dst_values: Sequence[str],
) -> None:
    """Open the CHECK, rewrite ``src`` to ``dst`` in place, re-install the CHECK."""
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_constraint(check_name, type_="check")

    op.get_bind().execute(
        sa.text(f"UPDATE {table} SET {column} = :dst WHERE {column} = :src"),
        {"dst": dst, "src": src},
    )

    with op.batch_alter_table(table) as batch_op:
        batch_op.create_check_constraint(
            check_name, _check_expr(column, dst_values)
        )


def upgrade() -> None:
    # (1) campaigns.curated + backfill from the existing human_curation flows.
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.add_column(
            sa.Column(
                "curated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.get_bind().execute(
        sa.text(
            "UPDATE campaigns SET curated = :curated "
            "WHERE EXISTS (SELECT 1 FROM dags d "
            "WHERE d.campaign_id = campaigns.id AND d.origin = :origin)"
        ).bindparams(
            sa.bindparam("curated", True, type_=sa.Boolean()),
            sa.bindparam("origin", "human_curation", type_=sa.String()),
        )
    )

    # (2) dags -> attack_flows and stage_runs.chosen_dag_id -> chosen_flow_id.
    op.rename_table("dags", "attack_flows")
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.drop_constraint(_FK_OLD_NAME, type_="foreignkey")
        batch_op.alter_column("chosen_dag_id", new_column_name="chosen_flow_id")
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.create_foreign_key(
            _FK_NEW_NAME, "attack_flows", ["chosen_flow_id"], ["id"]
        )

    # (3) AgentStage CHECKs: dag -> flow on the three columns.
    for table, column, check_name in _COLUMNS:
        _regenerate(table, column, check_name, "dag", "flow", _FLOW_VALUES)

    # (4) dag_origin -> flow_origin: nothing physical to change (see docstring).


def downgrade() -> None:
    # (3') AgentStage CHECKs: flow -> dag on the three columns.
    for table, column, check_name in _COLUMNS:
        _regenerate(table, column, check_name, "flow", "dag", _DAG_VALUES)

    # (2') attack_flows -> dags and chosen_flow_id -> chosen_dag_id.
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.drop_constraint(_FK_NEW_NAME, type_="foreignkey")
        batch_op.alter_column("chosen_flow_id", new_column_name="chosen_dag_id")
    op.rename_table("attack_flows", "dags")
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.create_foreign_key(_FK_OLD_NAME, "dags", ["chosen_dag_id"], ["id"])

    # (1') Drop the derivable flag.
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_column("curated")
