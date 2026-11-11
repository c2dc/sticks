# Análise — Migração de disco para banco e "tudo via IA"

> Documento de análise e recomendações. Não altera código. Baseado em inspeção
> do código em `apps/backend` e do estado real do banco (Postgres `sticks-postgres`)
> em nov/2026.

## 1. Direção do produto (o alvo)

- Acabar com a distinção curada vs não-curada como *gate*. "Curada" vira só um
  atributo no banco que a IA pode consultar ou não.
- **Toda** campanha (as 52 do pacote STIX) tem que ser emulável pela abordagem
  multiagente de IA.
- Parar de ler arquivo em disco como fonte de verdade. O que é estruturado vai
  pro relacional; o que é associativo vai pro vetorial.
- A IA deve consumir os dados corretos do banco.

## 2. Estado atual (fatos medidos)

Contagens reais no Postgres:

| Dado | Linhas | Observação |
|---|---|---|
| `stix_packages` | 1 | pacote v18.0 persistido |
| `stix_campaigns` | 52 | **todas** as campanhas no banco |
| `stix_campaign_techniques` | 1019 | técnicas por campanha |
| `stix_actors` | 187 | atores |
| `campaigns` (curadas) | 2 | parcial; deveria refletir as 8 curadas |
| `dags` | 1 | DAGs curados majoritariamente em disco |
| `abilities` | 11 | abilities curadas majoritariamente em disco |
| `experience_memory` (pgvector) | 0 | **vazio** — RAG nunca populado |
| `agent_runs` | 0 | nenhum run de IA executado |

Bancos disponíveis:
- **Relacional**: PostgreSQL (`pipeline_ui`). 31 tabelas.
- **Vetorial**: pgvector embutido no mesmo Postgres (`experience_memory.embedding`,
  `VECTOR(768)` via Ollama `nomic-embed-text`). **Não há** um banco não-relacional
  separado — e não precisa. pgvector no Postgres já cobre o caso vetorial.
- **Checkpointer** de IA: tabelas `checkpoints*` (LangGraph) no mesmo Postgres.

## 3. O que ainda vem de disco (a remover/migrar)

Fonte de verdade em disco hoje, lida em tempo de request:

1. **`CaseService`** (`app/services/campaign_service.py`): lê
   `packages/cli/data/api/{slug}_dag-ability.json`,
   `{slug}_dag-adversary.json` e `packages/cli/data/dag/{slug}_dag.json`.
   Registro fixo `CURATED_CASES` com 8 slugs. Qualquer id fora disso →
   `Unknown curated case slug` (o erro que aparece ao clicar numa não-curada).
2. **`DagService`** (Stage 3): lê `data/dag/{slug}_dag.json` via `CaseService`.
3. **Modelos legados** `Case`/`Dag`/`Ability`/`Adversary` em `domain.py` ainda
   descritos como "mapeia o arquivo em data/api/...".
4. **`sticks_config`**: resolve `packages/cli/config/config.py` por caminho.
5. **Atomic Red Team**: vive no plugin do Caldera
   (`infra/docker/.docker/caldera/plugins`), não numa estrutura do backend.

## 4. Pontos de bloqueio para "tudo via IA"

Cadeia que quebra para uma campanha não-curada (endereçada por `stix_id`):

1. **Validação de slug** — `_known_slug_or_404` em `app/api/campaigns.py`
   (stage/1, stage/2/select, stage/3..6) e nos endpoints de IA rejeita qualquer
   id que não seja um dos 8 slugs curados (404).
2. **Stage 2 select** — `Stage2Service.select` →
   `StageService.run_stage1`/`_build_campaign_stix` → `CaseService` (disco).
   Explode com `KeyError`/`Unknown curated case slug` e reporta
   `failed_step = leitura_do_caso`.
3. **FK estrutural** — `agent_runs.campaign_id`, `abilities.campaign_id`,
   `dags.campaign_id`, etc. têm FK para `campaigns.id`, que só tem as curadas.
   Uma não-curada **não existe** nessa tabela. `_materialize_ai_abilities`
   (graph.py) assume que `campaigns.id` já existe.
4. **Flag `selectable`** — `Stage2Service.list_campaigns` marca tudo
   `selectable=True`, mas o caminho de extração só suporta curadas. Contrato
   mente para o frontend.

## 5. O que JÁ está pronto para suportar todas as campanhas

- **Contexto STIX de qualquer campanha** já está no banco e acessível via
  `StixPackageReadService.campaign_overview(version, campaign_key)`: identidade,
  aliases, descrição, técnicas ATT&CK e atores — para as 52. É o candidato
  natural a `agent_input` das não-curadas.
- A arquitetura de duas memórias já está desenhada (`memory.py`): relacional
  para fatos exatos, pgvector para conhecimento associativo (afinidades
  comando↔ferramenta do Atomic Red Team, "o que funcionou").

## 6. Recomendações (proposta de reestruturação)

### 6.1 Fonte de verdade única no banco
- Migrar o conteúdo curado (DAGs, abilities, adversaries dos 8 casos) de disco
  para o banco, de uma vez, por um comando de *seed/ingest* idempotente (não em
  tempo de request). Depois, **deletar** `CaseService`/`DagService` de disco e os
  caminhos que leem `data/api`/`data/dag`.
- "Curado" deixa de ser gate: vira coluna booleana informativa em `stix_campaigns`
  (ou em `campaigns`). A IA lê se quiser.

### 6.2 Materializar qualquer campanha
- Ao selecionar/rodar QUALQUER campanha, materializar (upsert idempotente) uma
  linha em `campaigns` a partir do STIX (id = `stix_id` ou `curated_slug` quando
  houver; nome; descrição). Resolve o FK sem relaxar o schema.
- `_known_slug_or_404` passa a aceitar qualquer campanha presente no STIX
  persistido (validação contra `stix_campaigns`, não contra `CURATED_CASES`).

### 6.3 Stage 2 e agentes consumindo o banco
- `Stage2Service.select` monta identidade + técnicas a partir de
  `StixPackageReadService.campaign_overview` (banco) para TODAS; para curadas,
  opcionalmente enriquece com o que já estiver no banco pós-ingest.
- O `agent_input` de cada nó (DAG/op/env) é montado do contexto STIX do banco +
  RAG do pgvector. Sem leitura de disco.

### 6.4 Popular o banco vetorial
- `experience_memory` está vazio. Precisa de um processo de *bootstrap* que
  embeda o conhecimento associativo inicial (padrões dos casos curados, afinidades
  comando↔ferramenta Atomic Red Team) e, daí em diante, cada run grava sua
  experiência. Sem isso o RAG não ajuda os agentes.

### 6.5 STIX e Atomic Red Team — o que persistir onde
- **STIX do pacote**: já está no relacional (correto). Para a IA, além do
  relacional, vale indexar descrições/aliases/técnicas no **vetorial** para
  recuperação semântica (as colunas já estão marcadas "for future embeddings").
- **Comandos do Atomic Red Team**: hoje só no plugin do Caldera. Recomendo
  ingerir num índice consultável: o mapeamento técnica→comandos no **relacional**
  (exato) e as **afinidades/semelhanças** comando↔técnica↔ferramenta no
  **vetorial** (para o agente sugerir comandos por similaridade).

### 6.6 Provisionamento por campanha (documentar)
- Documentar, por campanha, qual ambiente-alvo melhor a emula (imagens,
  serviços, topologia), derivado das técnicas/plataformas das abilities. Esse
  mapa deve virar dado consultável (relacional para o inventário fixo; vetorial
  para "ambientes que funcionaram para padrões parecidos").

## 7. Fluxo-alvo das iterações do agente (resumo)

1. Selecionar campanha (qualquer) → contexto STIX do banco.
2. Stage 3 (DAG) agente: propõe DAG a partir do contexto + RAG.
3. Stage 4 (operacionalização): técnicas → comandos (Atomic Red Team do índice).
4. Stage 5 (ambiente): provisiona o ambiente recomendado para a campanha.
5. Stage 6 (execução/medida): roda no lab contido, mede, gera relatório.
6. Evaluator decide repetir (refino) ou concluir; grava experiência no pgvector.

## 8. Faseamento sugerido (para execução segura)

- **Fase 1** — Seleção sem erro para todas: materializar campanha + Stage 2 lê
  do banco + `_known_slug_or_404` aceita qualquer campanha STIX. (valor visível,
  baixo risco)
- **Fase 2** — Ingest do curado para o banco + remoção da leitura de disco
  (`CaseService`/`DagService`).
- **Fase 3** — Agentes montam `agent_input` do banco para todas; bootstrap do
  pgvector; índice do Atomic Red Team.
- **Fase 4** — Mapa de provisionamento por campanha.

Cada fase deve virar um spec com testes, dado que mexe no caminho crítico.

---

# Fase 1 — Plano de implementação (seleção de qualquer campanha, lendo do banco)

> Objetivo: ao selecionar QUALQUER campanha (curada ou não), a extração de
> atributos (identidade + técnicas) é montada a partir do STIX persistido no
> banco, eliminando o erro `Unknown curated case slug` / `leitura_do_caso`.
> Sem escrita em `campaigns`/`agent_runs` (não mexe em FK nesta fase).

## Causa raiz confirmada (com arquivo:linha)

- `Stage2Service.select` (`app/services/stix/stage2_service.py`) chama
  `StageService.run_stage1(slug, ...)`.
- `run_stage1` (`app/services/stage/stage_service.py`, step `READ_CASE`) faz
  `case = self._case_service.load_case(slug)` — lê `data/dag/{slug}_dag.json`
  e `data/api/{slug}_*.json` do disco. Para um `stix_id` não-curado isso cai no
  `except Exception` → `failed_step = leitura_do_caso` → `completed = False`.
- As técnicas hoje vêm do DAG do caso (`_extract_techniques(case.dag)`), NÃO do
  STIX — por isso não-curadas não têm como completar.
- O `_build_campaign_stix` já é defensivo (não quebra) mas resolve por `slug`.

## Abordagem

Fazer `Stage2Service.select` montar o resultado a partir de
`StixPackageReadService.campaign_overview(version, campaign_key)` — que já existe
e funciona para TODAS as 52 campanhas (identidade, aliases, descrição, técnicas,
atores). O `campaign_key` é o `curated_slug` quando houver, senão o `stix_id`
(mesma regra que a listagem usa).

Isso unifica curadas e não-curadas numa única fonte (o banco), alinhado à
direção "tudo via banco". O caminho de disco (`run_stage1`/`CaseService`) deixa
de ser usado pela seleção.

## Mudanças (escopo da Fase 1)

1. **`Stage2Service`** (`stage2_service.py`):
   - Injetar/ços construir um `StixPackageReadService(session)` (a Session já
     está disponível via `StageService._session`).
   - Reescrever `select(slug_or_key, version)` para:
     a) resolver a campanha no banco por `campaign_key` (curated_slug ou stix_id);
     b) montar `identity` (`CampaignStixInput`) dos campos do overview;
     c) montar `techniques` (`TechniqueElement`) das técnicas do overview;
     d) `completed = True` quando a campanha existe no banco e o overview foi
        montado; reportar causa clara se não persistida.
   - `read(...)` idem (view read-only), do banco.
   - `list_campaigns`: manter `selectable=True` para todas (coerente com a nova
     direção — o gate morre).

2. **Endpoint** (`api/campaigns.py`):
   - `stage/2` e `stage/2/select`: a validação `_known_slug_or_404` é sobre o
     PATH (o probe curado) e continua válida; a seleção em si passa a aceitar
     qualquer `body.slug` (stix_id ou curated_slug), pois o select agora lê do
     banco. Ajustar a validação apenas se necessário.

3. **Sem mudança de schema** nesta fase (seleção não escreve em `campaigns`).

## Testes

- Atualizar os testes de Stage 2 que hoje assumem extração via DAG/`CaseService`
  curado para a nova fonte (banco). Semear `stix_*` no SQLite de teste.
- Novo teste: selecionar uma campanha NÃO-curada (por `stix_id`) retorna
  `completed=True` com identidade + técnicas do banco, SEM erro.
- Teste de regressão: selecionar uma curada continua retornando identidade +
  técnicas (agora do banco).

## Fora de escopo (fases seguintes)

- Rodar o Run de IA para não-curadas (FK em `campaigns`, materialização) — Fase 2/3.
- Ingest do curado de disco para o banco e remoção do `CaseService` — Fase 2.
- Bootstrap do pgvector e índice do Atomic Red Team — Fase 3.

---

# Fase 2 - Plano decomposto (migrar curado disco -> banco, remover leitura de disco)

> A Fase 2 e estrutural e toca o caminho critico. NAO sera feita numa tacada.
> Decomposta em sub-passos pequenos e verificaveis (cada um testado antes do
> proximo), no mesmo estilo que funcionou na Fase 1.

## Raio de impacto medido

- Producao: 7 arquivos importam CaseService/DagService (api/topology.py,
  api/emulation.py, api/campaigns.py, api/ai.py, services/campaign_service.py,
  services/stage/stage_service.py, services/dag/dag_service.py).
- Testes: ~37 arquivos tocam esse caminho ou os dados de disco curados.
- Dados: as tabelas dags/abilities/adversaries existem mas estao quase vazias
  (o conteudo curado real ainda vive em packages/cli/data/api e .../data/dag).

## Sub-passos (ordem de execucao, cada um com testes)

### 2.1 - Ingest idempotente disco -> banco (NAO remove nada)
Comando/servico que le os 8 casos curados (CaseService) e persiste:
- `dags` (structural_nodes + edges do {slug}_dag.json),
- `abilities` ({slug}_dag-ability.json -> executors),
- `adversaries` ({slug}_dag-adversary.json -> atomic_ordering),
- a linha `campaigns` (slug, nome, descricao).
Idempotente (upsert por chave). Verificavel: contar linhas no banco apos rodar.
Risco baixo: so adiciona dados, nada e removido nem reescrito.

### 2.2 - DagService le do banco (fallback disco)
`DagService.get_dag`/auto_complete passam a ler os `dags` do banco; mantem o
fallback de disco ate 2.4. Testes do Stage 3 passam a semear o banco.

### 2.3 - Operacionalizacao/emulacao leem do banco
`_operationalize`, emulation.py e topology.py leem abilities/adversary/dag do
banco em vez de CaseService. Stage 4/5/6 passam a depender do banco.

### 2.4 - Remover a leitura de disco
Com tudo lendo do banco e verde: deletar CaseService/DagService (ou reduzir a
um ingest one-shot), remover os imports e os arquivos de disco como fonte de
verdade em runtime. Renomear as funcoes legadas enganosas (run_stage1/
read_stage1 -> nomes de Stage 2; etc.).

### 2.5 - Limpeza de testes
Remover/!atualizar os ~37 testes que exercitavam o caminho de disco; os que
viram obsoletos (cobriam codigo deletado) saem junto com o codigo.

## Principio
Cada sub-passo e um commit verificavel. Rodar SOMENTE os testes afetados de
cada sub-passo (nunca a suite inteira). Nada de big-bang.
