"""rename Portuguese tables and columns to English/STIX names (Option A)

Chained onto ``f3b9d2c71a5e`` (``criar_ambiente_provisionado``), the current
head. This migration implements the design's **Option A** (§4): a pure,
reversible rename of every Portuguese physical table and column to the
English / STIX-aligned names the ORM now exposes (task 2.4 made the ORM
*attributes* English; task 4.2 sets the matching ``__tablename__`` /
``mapped_column`` names). No data is moved, no column is dropped or added, no
type is changed — only ``ALTER TABLE ... RENAME`` / ``RENAME COLUMN`` and the
rename of the single explicitly-named foreign key that embeds an old table
name.

Design authority
----------------
The table and column names below are the §4 Option A table plus the §2
terminology table and the §3c field table, reconciled against **every**
Portuguese physical name defined across the prior migrations:

* ``a28d8c1ae4aa`` (domain tables),
* ``c348c9e36422`` (``dags`` + ``execucoes_estagio.dag_escolhido_id``),
* ``e907e49a84c5`` (``versoes_stix`` / ``parametros_ambiente`` /
  ``relatorios_execucao``),
* ``f3b9d2c71a5e`` (``ambiente_provisionado``).

Table renames (§4 Option A — authoritative)
--------------------------------------------
``casos``                → ``campaigns``
``execucoes_estagio``    → ``stage_runs``
``resultados_ability``   → ``ability_results``
``operacoes``            → ``operations``
``relatorios_execucao``  → ``execution_reports``
``versoes_stix``         → ``stix_versions``
``parametros_ambiente``  → ``environment_parameters``
``ambiente_provisionado``→ ``provisioned_environment``
``auditoria``            → ``audit``
``estado_sessao``        → ``session_state``
``preferencias``         → ``preferences``

Already-English tables (``dags``, ``abilities``, ``adversaries``) keep their
**names**, but their Portuguese *columns* are still renamed to the English ORM
attributes (e.g. ``dags.caso_id`` → ``dags.campaign_id``).

Portability
-----------
The product database is PostgreSQL; the unit tests run against an ephemeral
SQLite file. SQLite has no native ``RENAME COLUMN`` across all supported
versions and no ``ALTER CONSTRAINT``, so every *column* rename and the FK
rename run inside ``op.batch_alter_table`` (which recreates the table on
SQLite and is a thin ``ALTER`` on PostgreSQL — ``render_as_batch`` is already
enabled for SQLite in ``env.py``). Table renames use ``op.rename_table`` which
is portable on both backends. Tables are renamed **first**, then column/FK
renames target the new table names.

The only explicitly-named constraint in the schema is the FK
``fk_execucoes_estagio_dag_escolhido_id_dags`` (created in ``c348c9e36422``);
it is renamed to ``fk_stage_runs_chosen_dag_id_dags``. All other FKs/PKs were
created unnamed (backend-generated names) and travel with their table through
the rename on both backends, so there is nothing portable to rename for them.

``downgrade`` restores every Portuguese table, column, index/constraint and FK
name exactly, so ``upgrade`` ∘ ``downgrade`` is a lossless identity on the
schema.

Revision ID: 2d8cb3e55bb7
Revises: f3b9d2c71a5e
Create Date: 2026-10-15 02:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "2d8cb3e55bb7"
down_revision: Union[str, None] = "f3b9d2c71a5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --- Table renames (Portuguese → English, §4 Option A) ----------------------
# Order is irrelevant for pure RENAME TABLE (no cross-table dependency on the
# physical name during a rename), but it is listed in the create order of the
# original migrations for readability.
_TABLE_RENAMES: list[tuple[str, str]] = [
    ("casos", "campaigns"),
    ("preferencias", "preferences"),
    ("estado_sessao", "session_state"),
    ("execucoes_estagio", "stage_runs"),
    ("operacoes", "operations"),
    ("auditoria", "audit"),
    ("resultados_ability", "ability_results"),
    ("versoes_stix", "stix_versions"),
    ("parametros_ambiente", "environment_parameters"),
    ("relatorios_execucao", "execution_reports"),
    ("ambiente_provisionado", "provisioned_environment"),
]

# --- Column renames, keyed by the ENGLISH (post-rename) table name ----------
# Each value maps Portuguese column → English column. Tables not listed (none)
# have no Portuguese columns. ``dags``/``abilities``/``adversaries`` keep their
# table names but still rename Portuguese columns here. Columns already English
# (ids, ``name``, ``status``, ``caldera_operation_id`` ...) are intentionally
# omitted — they are left untouched.
_COLUMN_RENAMES: dict[str, dict[str, str]] = {
    # casos → campaigns
    "campaigns": {
        "nome": "name",
        "descricao": "description",
        "arquivo_ability": "ability_file",
        "arquivo_adversary": "adversary_file",
        "arquivo_dag": "dag_file",
        "origem_traducao": "translation_source",
    },
    # preferencias → preferences
    "preferences": {
        "tema": "theme",
        "idioma": "language",
    },
    # estado_sessao → session_state
    "session_state": {
        "caso_atual": "current_campaign",
        "atualizado_em": "updated_at",
    },
    # execucoes_estagio → stage_runs (FK rename handled separately below)
    "stage_runs": {
        "caso_id": "campaign_id",
        "estagio": "stage",
        "estado": "state",
        "progresso": "progress",
        "mensagem_erro": "error_message",
        "etapa_falha": "failed_step",
        "atualizado_em": "updated_at",
        "dag_escolhido_id": "chosen_dag_id",
    },
    # operacoes → operations
    "operations": {
        "caso_id": "campaign_id",
        "estado": "state",
        "total_sucesso": "total_success",
        "total_falha": "total_failure",
        "iniciada_em": "started_at",
        "finalizada_em": "finished_at",
    },
    # auditoria → audit
    "audit": {
        "operacao_id": "operation_id",
        "comando": "command",
        "container_destino": "target_container",
        "resultado": "result",
        "registrado_em": "recorded_at",
    },
    # resultados_ability → ability_results
    "ability_results": {
        "operacao_id": "operation_id",
        "saida_comando": "command_output",
    },
    # versoes_stix → stix_versions
    "stix_versions": {
        "versao": "version",
        "curada": "curated",
        "artefatos_curados_path": "curated_artifacts_path",
    },
    # parametros_ambiente → environment_parameters
    "environment_parameters": {
        "caso_id": "campaign_id",
        "plataforma": "platform",
        "conteudo": "content",
        "origem": "origin",
        "criado_em": "created_at",
    },
    # relatorios_execucao → execution_reports
    "execution_reports": {
        "caso_id": "campaign_id",
        "operacao_id": "operation_id",
        "metricas": "metrics",
        "veredito_sucesso": "success_verdict",
        "ambiente_desprovisionado": "environment_deprovisioned",
        "gerado_em": "generated_at",
    },
    # ambiente_provisionado → provisioned_environment
    "provisioned_environment": {
        "caso_id": "campaign_id",
        "plataforma": "platform",
        "conteudo": "content",
        "origem": "origin",
        "pronto": "ready",
        "criado_em": "created_at",
        "atualizado_em": "updated_at",
    },
    # dags: table name unchanged, Portuguese columns renamed to English ORM attrs
    "dags": {
        "caso_id": "campaign_id",
        "origem": "origin",
        "identificador": "identifier",
        "rotulo": "label",
        "conteudo": "content",
        "criado_em": "created_at",
    },
    # abilities: table name unchanged, Portuguese columns renamed
    "abilities": {
        "caso_id": "campaign_id",
        "origem_traducao": "translation_source",
    },
    # adversaries: table name unchanged, Portuguese columns renamed
    "adversaries": {
        "caso_id": "campaign_id",
        "origem_traducao": "translation_source",
    },
}

# Explicitly-named FK that embeds the old table name (created in c348c9e36422).
_FK_OLD_NAME = "fk_execucoes_estagio_dag_escolhido_id_dags"
_FK_NEW_NAME = "fk_stage_runs_chosen_dag_id_dags"


def _rename_columns(table: str, mapping: dict[str, str]) -> None:
    """Rename every ``old -> new`` column of ``table`` inside one batch block.

    Column renames run in batch mode so SQLite (the test DB) can perform the
    ALTER; on PostgreSQL this lowers to a plain ``RENAME COLUMN``.
    ``render_as_batch`` is enabled for SQLite in ``env.py``.
    """
    with op.batch_alter_table(table) as batch_op:
        for old_col, new_col in mapping.items():
            batch_op.alter_column(old_col, new_column_name=new_col)


def upgrade() -> None:
    # (1) Rename the tables first so column/FK renames target the new names.
    for old, new in _TABLE_RENAMES:
        op.rename_table(old, new)

    # (2) Rename the single explicitly-named FK AND the ``stage_runs`` columns
    # together in one batch block. Doing the FK drop/recreate in the same block
    # as the column rename avoids relying on an intermediate reflected state on
    # SQLite (where batch rebuilds the table). This mirrors the create pattern
    # of migration ``c348c9e36422`` which already drops/creates this FK by name
    # inside a batch block. The FK always targets ``dags.id``.
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.drop_constraint(_FK_OLD_NAME, type_="foreignkey")
        for old_col, new_col in _COLUMN_RENAMES["stage_runs"].items():
            batch_op.alter_column(old_col, new_column_name=new_col)
        batch_op.create_foreign_key(
            _FK_NEW_NAME, "dags", ["chosen_dag_id"], ["id"]
        )

    # (3) Rename every Portuguese column on the remaining tables.
    for table, columns in _COLUMN_RENAMES.items():
        if table == "stage_runs":
            continue  # already handled with its FK in step (2)
        _rename_columns(table, columns)


def downgrade() -> None:
    # Exact inverse of ``upgrade``: undo the non-stage_runs columns, then the
    # stage_runs FK+columns, then the table names — restoring the Portuguese
    # schema exactly.

    # (3') Restore the Portuguese column names on the non-stage_runs tables.
    for table, columns in _COLUMN_RENAMES.items():
        if table == "stage_runs":
            continue
        inverse = {new_col: old_col for old_col, new_col in columns.items()}
        _rename_columns(table, inverse)

    # (2') Restore the ``stage_runs`` FK name and column names in one batch
    # block (inverse of upgrade step 2).
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.drop_constraint(_FK_NEW_NAME, type_="foreignkey")
        for old_col, new_col in _COLUMN_RENAMES["stage_runs"].items():
            batch_op.alter_column(new_col, new_column_name=old_col)
        batch_op.create_foreign_key(
            _FK_OLD_NAME, "dags", ["dag_escolhido_id"], ["id"]
        )

    # (1') Restore the Portuguese table names (English → Portuguese).
    for old, new in _TABLE_RENAMES:
        op.rename_table(new, old)
