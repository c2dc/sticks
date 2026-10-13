"""renumerar estagios 1a4 e criar esquema do dag

Migração encadeada sobre ``a28d8c1ae4aa`` (``criar_tabelas_do_dominio``).
Promove a geração do DAG a Estágio real e persistido, renumerando o pipeline
de três para quatro Estágios e introduzindo a entidade ``dags`` multi-origem
mais a FK ``execucoes_estagio.dag_escolhido_id`` (seção "Plano de migração"
da design.md — Req. 2.1-2.6, 4.1-4.7, 7.3).

Decisões de implementação (seguindo a design.md):

* **PostgreSQL-only.** Usa ``op.get_bind()`` + SQL textual padrão para a
  renumeração; não recorre a SQLite (Req. 2.5, 7.3). Tudo roda na transação
  gerenciada pelo Alembic, logo a guarda que aborta deixa os dados intactos.
* **Guarda (Req. 2.6).** Antes de qualquer alteração, aborta com
  ``RuntimeError`` descritivo se existir alguma linha de ``execucoes_estagio``
  com ``estagio`` fora de 1..3 — sem alterar dados.
* **Renumeração do upgrade (Req. 2.1, 2.2, 2.3).** Desloca em ordem
  **decrescente** (3→4, 2→3, 1→2) para não colidir. Cada ``UPDATE`` altera só
  ``estagio``, preservando ``estado``, ``progresso``, ``mensagem_erro``,
  ``etapa_falha`` e ``caso_id``.
* **Nova tabela ``dags`` (Req. 4.1-4.4, 4.6).** ``origem`` persiste via
  ``sa.Enum('curadoria_humana', 'gerado_ia', name='origem_dag',
  native_enum=False)``, espelhando ``_portable_enum(OrigemDag, "origem_dag")``
  do modelo ``Dag``: o ``CHECK`` já aceita ``gerado_ia`` desde já (Req. 4.6,
  9.4). ``conteudo`` é JSON portável; FK ``caso_id → casos.id``.
* **Coluna ``dag_escolhido_id`` (Req. 4.7).** Integer nullable, FK →
  ``dags.id``, adicionada após criar ``dags``.
* **``downgrade`` simétrico (Req. 2.4).** Remove a coluna, dropa ``dags`` e
  renumera de volta em ordem **crescente** (2→1, 3→2, 4→3), de modo que
  up∘down seja identidade sobre os dados preservados.

Revision ID: c348c9e36422
Revises: a28d8c1ae4aa
Create Date: 2026-10-11 21:43:01.155431+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c348c9e36422'
down_revision: Union[str, None] = 'a28d8c1ae4aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # (1) Guarda (Req. 2.6): aborta sem alterar dados se houver estagio fora de
    # 1..3. Roda antes de qualquer UPDATE, dentro da transação do Alembic.
    fora = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM execucoes_estagio "
            "WHERE estagio NOT BETWEEN 1 AND 3"
        )
    ).scalar_one()
    if fora:
        raise RuntimeError(
            f"Migração abortada: {fora} linha(s) de execucoes_estagio com "
            "estagio fora de 1..3. Nenhum dado foi alterado."
        )

    # (2) Renumerar em ordem decrescente (3→4, 2→3, 1→2) para evitar colisão
    # (Req. 2.1, 2.3). Cada UPDATE preserva estado/progresso/mensagem_erro/
    # etapa_falha/caso_id, alterando apenas estagio (Req. 2.2).
    for antigo in (3, 2, 1):
        bind.execute(
            sa.text(
                "UPDATE execucoes_estagio SET estagio = :novo "
                "WHERE estagio = :antigo"
            ),
            {"novo": antigo + 1, "antigo": antigo},
        )

    # (3) Criar a tabela dags (tipos portáveis; origem como VARCHAR + CHECK via
    # native_enum=False, já aceitando 'gerado_ia' — Req. 4.1-4.4, 4.6).
    op.create_table(
        "dags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("caso_id", sa.String(), nullable=False),
        sa.Column(
            "origem",
            sa.Enum(
                "curadoria_humana",
                "gerado_ia",
                name="origem_dag",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("identificador", sa.String(), nullable=False),
        sa.Column("rotulo", sa.String(), nullable=True),
        sa.Column("conteudo", sa.JSON(), nullable=False),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["caso_id"], ["casos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # (4) Adicionar execucoes_estagio.dag_escolhido_id (Integer nullable,
    # FK → dags.id), após criar dags (Req. 4.7).
    with op.batch_alter_table("execucoes_estagio") as batch_op:
        batch_op.add_column(
            sa.Column("dag_escolhido_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_execucoes_estagio_dag_escolhido_id_dags",
            "dags",
            ["dag_escolhido_id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()

    # (1) Remover a FK + coluna dag_escolhido_id (inverso da etapa 4 do upgrade).
    with op.batch_alter_table("execucoes_estagio") as batch_op:
        batch_op.drop_constraint(
            "fk_execucoes_estagio_dag_escolhido_id_dags", type_="foreignkey"
        )
        batch_op.drop_column("dag_escolhido_id")

    # (2) Remover a tabela dags (inverso da etapa 3 do upgrade).
    op.drop_table("dags")

    # (3) Renumerar de volta em ordem crescente (2→1, 3→2, 4→3) para evitar
    # colisão no sentido inverso, preservando os demais atributos (Req. 2.4).
    for antigo in (2, 3, 4):
        bind.execute(
            sa.text(
                "UPDATE execucoes_estagio SET estagio = :novo "
                "WHERE estagio = :antigo"
            ),
            {"novo": antigo - 1, "antigo": antigo},
        )
