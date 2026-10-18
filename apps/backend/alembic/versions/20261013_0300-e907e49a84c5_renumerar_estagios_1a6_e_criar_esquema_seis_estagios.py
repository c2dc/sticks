"""renumerar estagios 1a6 e criar o esquema de seis estagios

Migração encadeada sobre ``1cc7b8b24bd2`` (``renumerar_estagios_1a5_e_dividir_
emulacao``). Evolui o pipeline de **cinco** para **seis** Estágios, introduzindo
o novo Estágio 1 (Seleção da base STIX) à frente de todos os demais e criando as
três tabelas novas do fluxo (``versoes_stix``, ``parametros_ambiente``,
``relatorios_execucao``). Vide a seção "Plano de migração (Alembic)" da
design.md da spec ``pipeline-seis-estagios`` — Req. 1.2, 1.5, 5.5, 5.6, 8.3,
15.7.

Mapeamento (esquema antigo 1..5 → novo 1..6):

* OLD 1 (Modelagem Estrutural) → NEW 2 (Seleção da campanha — atributos)
* OLD 2 (Geração do DAG)       → NEW 3 (Geração do DAG)
* OLD 3 (Operacionalização)    → NEW 4 (Operacionalização)
* OLD 4 (Provisionamento)      → NEW 5 (Criação do ambiente)
* OLD 5 (Execução)             → NEW 6 (Execução + medição + relatório)
* NEW 1 (Seleção da base STIX) → sem equivalente antigo; é **criado** no upgrade
  para cada Caso cujo antigo Estágio 1 (Modelagem) estava ``concluido``.

Ou seja, cada antigo Estágio ``n`` vira ``n+1`` e o novo Estágio 1 (Seleção da
base STIX) **não tem equivalente** no esquema de cinco Estágios — é **criado**.
A antiga Modelagem (1) deixa de ser Estágio independente e seus dados são
absorvidos pela Seleção da campanha (novo 2); se a Modelagem concluiu, a base
STIX já estava selecionada/baixada, logo a Seleção da base nasce ``concluido``.

Decisões de implementação (seguindo a design.md e o precedente de
``1cc7b8b24bd2``):

* **PostgreSQL-only.** Usa ``op.get_bind()`` + SQL textual padrão; tudo dentro
  da transação gerenciada pelo Alembic, logo a guarda que aborta deixa os
  dados intactos (Req. 1.2, 1.5). O driver roda contra o Postgres do produto;
  os property tests exercitam ``upgrade()``/``downgrade()`` in-process (SQLite
  efêmero isolado), portanto o SQL se mantém no subconjunto portável.
* **Guarda.** Antes de qualquer alteração, aborta com ``RuntimeError``
  descritivo se existir alguma linha de ``execucoes_estagio`` com ``estagio``
  fora de 1..5 (o intervalo esperado do esquema anterior), sem alterar dados.
* **Remapeamento com faixa temporária.** O deslocamento uniforme ``n → n+1``
  colide se feito de forma ingênua. O upgrade move cada linha primeiro para
  uma faixa alta (``destino + 100`` via ``CASE``) e depois subtrai 100, em
  ordem decrescente de origem, de modo que nenhuma linha seja perdida ou
  duplicada. Cada ``UPDATE`` altera só ``estagio``, preservando ``estado``,
  ``progresso``, ``mensagem_erro``, ``etapa_falha``, ``caso_id`` e
  ``dag_escolhido_id``.
* **Criação da linha de Seleção da base STIX (NEW 1).** Para cada Caso cujo
  **antigo** Estágio 1 (Modelagem) estava ``concluido``, cria uma nova linha de
  Seleção da base em ``estagio = 1`` com ``estado = 'concluido'`` e
  ``progresso = 100`` (preservando ``caso_id``). A detecção usa o estado
  **antes** do remapeamento (OLD 1 ``concluido``).
* **Tabelas novas (Req. 5.5, 5.6, 8.3, 15.7).** ``versoes_stix``,
  ``parametros_ambiente`` e ``relatorios_execucao`` são criadas com tipos
  portáveis; os enums (``plataforma_ambiente``, ``translation_source``)
  persistem via ``sa.Enum(..., native_enum=False)`` (VARCHAR + CHECK),
  espelhando ``_portable_enum`` do modelo: o CHECK de ``plataforma`` já aceita
  ``maquina_virtual`` e o de ``origem`` já aceita ``gerado_ia``, de modo que
  valores futuros não exigem migração de esquema (Req. 16.4, 17.2). A tabela
  ``versoes_stix`` é **semeada** com a versão curada (``CURATED_VERSION`` =
  ``"18.0"``, ``curada = true`` — Req. 5.5, 5.6).
* **Sem CHECK em ``estagio``.** O modelo ``StageRun`` persiste ``estagio`` como
  ``Integer`` puro (sem enum, sem ``CHECK`` de intervalo); as migrações
  anteriores (1..4, 1..5) tampouco criaram ``CHECK (estagio BETWEEN ...)``.
  Logo **não há** constraint de intervalo em ``estagio`` para ajustar: o
  intervalo 1..6 é acomodado pela própria coluna ``Integer``.
* **``downgrade`` simétrico.** Primeiro **remove** as linhas de Seleção da base
  STIX (NEW 1) criadas no upgrade — e somente essas (no esquema 1..5 não havia
  Estágio de seleção de base, logo toda linha em ``estagio = 1`` no esquema novo
  foi criada pelo upgrade) — e só então inverte o remapeamento ``n → n-1`` com
  a mesma faixa temporária. A ordem importa: remover NEW 1 antes de inverter
  impede que a faixa temporária colida com as linhas a descartar. Por fim,
  dropa ``relatorios_execucao``, ``parametros_ambiente`` e ``versoes_stix``.
  up∘down é identidade sobre os dados remapeáveis.

Revision ID: e907e49a84c5
Revises: 1cc7b8b24bd2
Create Date: 2026-10-13 03:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e907e49a84c5"
down_revision: Union[str, None] = "1cc7b8b24bd2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Versão STIX curada semeada em ``versoes_stix`` — fonte única em código é a
# constante ``CURATED_VERSION`` de ``app.services.stix.stix_service`` (Req. 5.5,
# 5.6). Replicada aqui como literal para manter a migração autossuficiente e
# independente de imports da aplicação (seguindo o precedente das migrações
# anteriores, que não importam código da app).
_VERSAO_CURADA: str = "18.0"

# Mapeamento final do upgrade (esquema antigo 1..5 → novo 2..6), sem a criação
# do novo Estágio 1 (que não é um remapeamento e sim uma inserção derivada).
_UPGRADE_MAP: dict[int, int] = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6}
# Inverso usado pelo downgrade (novo → antigo).
_DOWNGRADE_MAP: dict[int, int] = {novo: antigo for antigo, novo in _UPGRADE_MAP.items()}

# Faixa alta de deslocamento temporário para evitar colisão durante o UPDATE.
_TEMP_OFFSET: int = 100


def _remap_estagio(bind, mapping: dict[int, int]) -> None:
    """Remapeia ``execucoes_estagio.estagio`` conforme ``mapping`` usando uma
    faixa temporária (+``_TEMP_OFFSET``) para evitar colisões.

    Passo 1: cada linha cujo ``estagio`` ∈ ``mapping`` recebe
    ``destino + _TEMP_OFFSET`` (via ``CASE``), levando todos os valores a uma
    faixa alta disjunta da faixa de origem. Passo 2: subtrai ``_TEMP_OFFSET``,
    assentando cada linha no número de destino. Só ``estagio`` muda — todos os
    demais atributos (``estado``/``progresso``/``mensagem_erro``/``etapa_falha``/
    ``caso_id``/``dag_escolhido_id``) são preservados.
    """
    # Passo 1: origem → (destino + OFFSET), apenas para os valores mapeados.
    whens = " ".join(
        f"WHEN estagio = {origem} THEN {destino + _TEMP_OFFSET}"
        for origem, destino in mapping.items()
    )
    origens = ", ".join(str(o) for o in mapping)
    bind.execute(
        sa.text(
            f"UPDATE execucoes_estagio SET estagio = CASE {whens} ELSE estagio END "
            f"WHERE estagio IN ({origens})"
        )
    )
    # Passo 2: faixa temporária → destino final.
    destinos_tmp = ", ".join(str(d + _TEMP_OFFSET) for d in mapping.values())
    bind.execute(
        sa.text(
            f"UPDATE execucoes_estagio SET estagio = estagio - {_TEMP_OFFSET} "
            f"WHERE estagio IN ({destinos_tmp})"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()

    # (1) Guarda: aborta sem alterar dados se houver estagio fora de 1..5. Roda
    # antes de qualquer UPDATE/CREATE, dentro da transação do Alembic.
    fora = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM execucoes_estagio "
            "WHERE estagio NOT BETWEEN 1 AND 5"
        )
    ).scalar_one()
    if fora:
        raise RuntimeError(
            f"Migração abortada: {fora} linha(s) de execucoes_estagio com "
            "estagio fora de 1..5. Nenhum dado foi alterado."
        )

    # (2) Capturar, ANTES do remapeamento, os caso_id cujo ANTIGO Estágio 1
    # (Modelagem) estava 'concluido'. Essas Campanhas ganham uma linha de
    # Seleção da base STIX (NEW 1) 'concluido' (Req. 1.5, 5.5). DISTINCT para
    # não duplicar quando houver múltiplas linhas por Caso; inclui caso_id NULL.
    casos_modelagem_concluida = bind.execute(
        sa.text(
            "SELECT DISTINCT caso_id FROM execucoes_estagio "
            "WHERE estagio = 1 AND estado = 'concluido'"
        )
    ).all()

    # (3) Remapear em faixa temporária: OLD n → NEW n+1 (1→2, 2→3, 3→4, 4→5,
    # 5→6), em ordem decrescente de origem via a faixa temporária. Preserva
    # estado/progresso/mensagem_erro/etapa_falha/caso_id/dag_escolhido_id — só
    # altera estagio.
    _remap_estagio(bind, _UPGRADE_MAP)

    # (4) Criar a linha de Seleção da base STIX (NEW 1) 'concluido' para cada
    # Caso cujo antigo Estágio 1 (Modelagem) estava 'concluido' (Req. 1.5).
    for (caso_id,) in casos_modelagem_concluida:
        bind.execute(
            sa.text(
                "INSERT INTO execucoes_estagio "
                "(caso_id, estagio, estado, progresso) "
                "VALUES (:caso_id, 1, 'concluido', 100)"
            ),
            {"caso_id": caso_id},
        )

    # (5) Criar as tabelas novas (tipos portáveis; enums via native_enum=False
    # → VARCHAR + CHECK, já aceitando os valores futuros — Req. 5.5, 5.6, 8.3,
    # 15.7, 16.4, 17.2).
    op.create_table(
        "versoes_stix",
        sa.Column("versao", sa.String(), nullable=False),
        sa.Column("curada", sa.Boolean(), nullable=False),
        sa.Column("artefatos_curados_path", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("versao"),
    )

    op.create_table(
        "parametros_ambiente",
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
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["caso_id"], ["casos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "relatorios_execucao",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("caso_id", sa.String(), nullable=False),
        sa.Column("operacao_id", sa.Integer(), nullable=True),
        sa.Column("metricas", sa.JSON(), nullable=False),
        sa.Column("veredito_sucesso", sa.Boolean(), nullable=False),
        sa.Column("ambiente_desprovisionado", sa.Boolean(), nullable=False),
        sa.Column("gerado_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["caso_id"], ["casos.id"]),
        sa.ForeignKeyConstraint(["operacao_id"], ["operacoes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # (6) Semear versoes_stix com a versão curada (18.0 curada=true — Req. 5.5,
    # 5.6). Fonte única em código: CURATED_VERSION (replicada como literal aqui).
    bind.execute(
        sa.text(
            "INSERT INTO versoes_stix (versao, curada, artefatos_curados_path) "
            "VALUES (:versao, :curada, NULL)"
        ),
        {"versao": _VERSAO_CURADA, "curada": True},
    )

    # (7) CHECK de intervalo de 'estagio': inexistente no esquema anterior
    # (coluna Integer pura, sem enum/CHECK). Nada a ajustar — o intervalo 1..6
    # é acomodado pela própria coluna.


def downgrade() -> None:
    bind = op.get_bind()

    # (1) Remover as tabelas novas (inverso da etapa 5 do upgrade), na ordem
    # inversa da criação para respeitar as FKs.
    op.drop_table("relatorios_execucao")
    op.drop_table("parametros_ambiente")
    op.drop_table("versoes_stix")

    # (2) Remover EXATAMENTE as linhas de Seleção da base STIX (NEW 1) criadas
    # no upgrade — e somente essas. No esquema antigo (1..5) não havia Estágio
    # de seleção de base: toda linha em estagio=1 no esquema novo foi criada
    # pelo upgrade. Remover ANTES de inverter evita que a faixa temporária
    # colida com elas.
    bind.execute(sa.text("DELETE FROM execucoes_estagio WHERE estagio = 1"))

    # (3) Inverter o remapeamento (NEW n → OLD n-1: 2→1, 3→2, 4→3, 5→4, 6→5),
    # restaurando o esquema 1..5 sobre os dados remapeáveis. Mesma faixa
    # temporária para evitar colisão da inversão.
    _remap_estagio(bind, _DOWNGRADE_MAP)

    # (4) CHECK de intervalo: nada a reverter (não foi criado no upgrade).
