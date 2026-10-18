"""renumerar estagios 1a5 e dividir a emulacao em provisionamento + execucao

Migração encadeada sobre ``c348c9e36422`` (``renumerar_estagios_1a4_e_criar_
esquema_do_dag``). Evolui o pipeline de **quatro** para **cinco** Estágios,
corrigindo a inversão dos dois primeiros e dividindo a antiga "Emulação" em
Provisionamento do Ambiente (novo Estágio 4) + Execução da Campanha (novo
Estágio 5). Vide a seção "Plano de migração (Alembic)" da design.md da spec
``pipeline-cinco-estagios`` — Req. 7.1-7.6.

Mapeamento (esquema antigo 1..4 → novo 1..5):

* OLD 1 (Geração do DAG)   → NEW 2
* OLD 2 (Modelagem)        → NEW 1
* OLD 3 (Tradução)         → NEW 3 (número inalterado; só muda o nome)
* OLD 4 (Emulação)         → NEW 5 (Execução)
* NEW 4 (Provisionamento)  → sem equivalente antigo; é **criado** no upgrade
  para cada Caso cujo antigo Estágio 4 (Emulação) estava ``concluido``.

Decisões de implementação (seguindo a design.md e o precedente de
``c348c9e36422``):

* **PostgreSQL-only.** Usa ``op.get_bind()`` + SQL textual padrão; tudo dentro
  da transação gerenciada pelo Alembic, logo a guarda que aborta deixa os
  dados intactos (Req. 7.1, 7.6). O driver roda contra o Postgres do produto;
  os property tests exercitam ``upgrade()``/``downgrade()`` in-process (SQLite
  efêmero isolado), portanto o SQL se mantém no subconjunto portável.
* **Guarda (Req. 7.5).** Antes de qualquer alteração, aborta com
  ``RuntimeError`` descritivo se existir alguma linha de ``execucoes_estagio``
  com ``estagio`` fora de 1..4 (o intervalo esperado do esquema anterior), sem
  alterar dados.
* **Remapeamento com faixa temporária (Req. 7.2, 7.3).** A inversão 1↔2 colide
  se feita diretamente. O upgrade move cada linha primeiro para uma faixa alta
  (``estagio + 100``, mapeada por ``CASE`` para o destino final deslocado) e
  depois subtrai 100, de modo que nenhuma linha seja perdida ou duplicada. Cada
  ``UPDATE`` altera só ``estagio``, preservando ``estado``, ``progresso``,
  ``mensagem_erro``, ``etapa_falha`` e ``caso_id``.
* **Criação da linha de Provisionamento (Req. 7.2, 7.3).** Para cada Caso cujo
  **antigo** Estágio 4 (Emulação) estava ``concluido``, cria uma nova linha de
  Provisionamento em ``estagio = 4`` com ``estado = 'concluido'`` e
  ``progresso = 100`` (preservando ``caso_id``) — pois, se a emulação concluiu,
  o ambiente estava necessariamente provisionado/isolado. A detecção usa o
  estado **antes** do remapeamento (OLD 4 ``concluido``), capturado via a
  tabela temporária de origem descrita abaixo.
* **Sem CHECK em ``estagio``.** O modelo ``StageRun`` persiste ``estagio`` como
  ``Integer`` puro (sem enum, sem ``CHECK``); os ``native_enum=False`` só
  geram ``CHECK`` sobre ``estado``/``origem``/etc., jamais sobre ``estagio``.
  A migração anterior (1..4) tampouco criou ``CHECK (estagio BETWEEN ...)``.
  Logo **não há** constraint de intervalo em ``estagio`` para ajustar: o
  intervalo 1..5 é acomodado automaticamente pela coluna ``Integer`` (Req. 7.6
  — "ajustar o CHECK **se existir**"). Não inventamos uma constraint que não
  existe.
* **``downgrade`` simétrico (Req. 7.4).** Primeiro **remove** as linhas de
  Provisionamento (NEW 4) criadas no upgrade — e somente essas — e só então
  inverte o remapeamento (NEW 1→OLD 2, NEW 2→OLD 1, NEW 3→OLD 3, NEW 5→OLD 4)
  usando a mesma faixa temporária. A ordem importa: remover NEW 4 antes de
  inverter impede que a faixa temporária colida com as linhas a descartar. O
  antigo Estágio 4 era Emulação (↔ NEW 5), e NÃO havia Provisionamento no
  esquema 1..4; portanto deletar todas as linhas em ``estagio = 4`` antes de
  inverter descarta exatamente as que o upgrade criou. up∘down é identidade
  sobre os dados remapeáveis.

Revision ID: 1cc7b8b24bd2
Revises: c348c9e36422
Create Date: 2026-10-12 01:50:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1cc7b8b24bd2"
down_revision: Union[str, None] = "c348c9e36422"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mapeamento final do upgrade (esquema antigo → novo), sem a criação do novo
# Estágio 4 (que não é um remapeamento e sim uma inserção derivada).
_UPGRADE_MAP: dict[int, int] = {1: 2, 2: 1, 3: 3, 4: 5}
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
    assentando cada linha no número de destino. Só ``estagio`` muda.
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

    # (1) Guarda (Req. 7.5): aborta sem alterar dados se houver estagio fora de
    # 1..4. Roda antes de qualquer UPDATE, dentro da transação do Alembic.
    fora = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM execucoes_estagio "
            "WHERE estagio NOT BETWEEN 1 AND 4"
        )
    ).scalar_one()
    if fora:
        raise RuntimeError(
            f"Migração abortada: {fora} linha(s) de execucoes_estagio com "
            "estagio fora de 1..4. Nenhum dado foi alterado."
        )

    # (2) Capturar, ANTES do remapeamento, os caso_id cujo ANTIGO Estágio 4
    # (Emulação) estava 'concluido'. Essas Campanhas ganham uma linha de
    # Provisionamento (NEW 4) 'concluido' (Req. 7.2, 7.3). DISTINCT para não
    # duplicar quando houver múltiplas linhas por Caso; inclui caso_id NULL.
    casos_emulacao_concluida = bind.execute(
        sa.text(
            "SELECT DISTINCT caso_id FROM execucoes_estagio "
            "WHERE estagio = 4 AND estado = 'concluido'"
        )
    ).all()

    # (3) Remapear em faixa temporária: OLD 1→2, OLD 2→1, OLD 3→3, OLD 4→5
    # (Req. 7.2, 7.3). Preserva estado/progresso/mensagem_erro/etapa_falha/
    # caso_id — só altera estagio.
    _remap_estagio(bind, _UPGRADE_MAP)

    # (4) Criar a linha de Provisionamento (NEW 4) 'concluido' para cada Caso
    # cujo antigo Estágio 4 (Emulação) estava 'concluido' (Req. 7.2, 7.3).
    for (caso_id,) in casos_emulacao_concluida:
        bind.execute(
            sa.text(
                "INSERT INTO execucoes_estagio "
                "(caso_id, estagio, estado, progresso) "
                "VALUES (:caso_id, 4, 'concluido', 100)"
            ),
            {"caso_id": caso_id},
        )

    # (5) CHECK de intervalo de 'estagio': inexistente no esquema anterior
    # (coluna Integer pura, sem enum/CHECK). Nada a ajustar — o intervalo 1..5
    # é acomodado pela própria coluna (Req. 7.6, cláusula "se existir").


def downgrade() -> None:
    bind = op.get_bind()

    # (1) Remover EXATAMENTE as linhas de Provisionamento (NEW 4) criadas no
    # upgrade — e somente essas (Req. 7.4). No esquema antigo (1..4) não havia
    # Provisionamento: o antigo Estágio 4 era Emulação (↔ NEW 5), logo toda
    # linha em estagio=4 no esquema novo é uma linha criada pelo upgrade.
    # Remover ANTES de inverter evita que a faixa temporária colida com elas.
    bind.execute(sa.text("DELETE FROM execucoes_estagio WHERE estagio = 4"))

    # (2) Inverter o remapeamento (NEW 1→OLD 2, NEW 2→OLD 1, NEW 3→OLD 3,
    # NEW 5→OLD 4), restaurando o esquema 1..4 sobre os dados remapeáveis
    # (Req. 7.4). Mesma faixa temporária para evitar colisão da inversão 1↔2.
    _remap_estagio(bind, _DOWNGRADE_MAP)

    # (3) CHECK de intervalo: nada a reverter (não foi criado no upgrade).
