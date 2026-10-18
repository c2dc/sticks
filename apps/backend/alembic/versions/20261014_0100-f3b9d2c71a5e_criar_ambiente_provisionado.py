"""criar a tabela ambiente_provisionado (Saída persistida do Estágio 5)

Migração encadeada sobre ``e907e49a84c5`` (``renumerar_estagios_1a6_e_criar_
esquema_seis_estagios``). Cria a tabela ``ambiente_provisionado`` que persiste a
Saída tipada do Estágio 5 (Criação do ambiente de simulação) —
:class:`~app.services.environment.provisionador.AmbienteProvisionado` — como JSON
portável associado ao Caso, de modo que o Estágio 6 e a tela a consumam após a
conclusão (Req. 6.3) e o provisionamento seja reaproveitado sem subir um novo
quando o ambiente já está pronto (idempotência — Req. 1.6). Vide a seção
"Persistência da Saída do Estágio 5" da design.md da spec
``estagio5-provisionamento-ambiente``.

Decisões de implementação (seguindo a design.md e o precedente da migração
``e907e49a84c5``):

* **PostgreSQL-only.** O banco do produto é PostgreSQL exclusivamente; o
  ``upgrade()`` usa apenas ``op.create_table`` com tipos portáveis e o
  ``downgrade()`` ``op.drop_table``, tudo dentro da transação gerenciada pelo
  Alembic. Os property tests exercitam ``upgrade()``/``downgrade()`` in-process
  (SQLite efêmero isolado), portanto o SQL se mantém no subconjunto portável.
* **Tabela nova (Req. 6.3, 1.6).** ``ambiente_provisionado`` espelha
  ``parametros_ambiente``: ``id`` PK, ``caso_id`` FK → ``casos.id``,
  ``plataforma`` e ``origem`` como enums portáveis via
  ``sa.Enum(..., native_enum=False)`` (VARCHAR + CHECK — o CHECK de ``plataforma``
  já aceita ``maquina_virtual`` e o de ``origem`` já aceita ``gerado_ia``, de modo
  que valores futuros não exijam migração — Req. 16.4, 17.2), ``conteudo`` JSON
  portável (a Saída serializada via ``model_dump(mode="json")``), ``pronto``
  Boolean (o veredito de homologação lido pela idempotência — Req. 1.6) e os
  timestamps ``criado_em``/``atualizado_em`` (naive-UTC).
* **``downgrade`` simétrico.** Dropa ``ambiente_provisionado``; up∘down é
  identidade sobre o esquema (a tabela não existe antes do upgrade e volta a não
  existir após o downgrade).

Revision ID: f3b9d2c71a5e
Revises: e907e49a84c5
Create Date: 2026-10-14 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3b9d2c71a5e"
down_revision: Union[str, None] = "e907e49a84c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Cria a tabela da Saída persistida do Estágio 5 (tipos portáveis; enums via
    # native_enum=False → VARCHAR + CHECK, já aceitando os valores futuros —
    # Req. 6.3, 1.6, 16.4, 17.2). Espelha ``parametros_ambiente``.
    op.create_table(
        "ambiente_provisionado",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("caso_id", sa.String(), nullable=False),
        sa.Column(
            "plataforma",
            sa.Enum(
                "conteiner",
                "maquina_virtual",
                name="plataforma_ambiente",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("conteudo", sa.JSON(), nullable=False),
        sa.Column(
            "origem",
            sa.Enum(
                "curadoria_humana",
                "gerado_ia",
                name="translation_source",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("pronto", sa.Boolean(), nullable=False),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["caso_id"], ["casos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # Inverso do upgrade: remove a tabela criada.
    op.drop_table("ambiente_provisionado")
