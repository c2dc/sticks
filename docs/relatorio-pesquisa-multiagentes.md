# Relatório de Progresso da Pesquisa — STICKS

**Projeto:** Automação da curadoria do Estágio 2 (procedural sufficiency) para emulação de APTs a partir de ATT&CK-in-STIX.

> **Nota de atualização (fluxo exclusivamente agêntico).** Este relatório registra marcos históricos. O pipeline curado de seis estágios (`StageService`, rotas `/stage/N`) foi removido; "curado" é hoje apenas a flag `curated` no banco. A ordem atual das fases é `input → operationalization → flow → environment → execution → evaluation`, e o antigo "DAG" é o *attack flow* (formato MITRE Attack Flow). As bases (Atomic Red Team, catálogo Docker Hub e corpus Attack Flow) são ingeridas automaticamente no primeiro start e verificadas em todo start. Detalhes em `README.md` e `docs/agentic-phases-literature.md`. As seções 2 e 3 abaixo descrevem o marco no momento em que foi alcançado, exceto o grafo e os agentes da seção 3.1, já atualizados.
**Período coberto:** a partir de 15/out (marco da arquitetura de seis estágios) até a data atual.
**Autor:** Nilson

---

## 1. Resumo executivo

Nesta fase a pesquisa atingiu dois marcos:

1. **Execução ponta a ponta de um fluxo completo das campanhas curadas.** O pipeline hoje conduz uma campanha pelos **seis estágios** (seleção da base STIX → seleção de campanha → geração do attack flow → operacionalização → criação do ambiente → execução/medição/relatório), com execução real contida no laboratório Docker isolado. O caso **ShadowRay** foi executado de ponta a ponta (E2E) no laboratório real.

2. **Montagem do ambiente de multiagentes de IA** que automatiza o Estágio 2 (a curadoria antes manual). O sistema usa **LangGraph** para orquestrar agentes especializados por estágio, com **memória de curto prazo** (checkpointer PostgreSQL) e **memória de longo prazo vetorial** (pgvector) para aprendizado entre campanhas (RAG), e um **LLM** (endpoint OpenAI-compatível da SEPINF) como motor de raciocínio dos agentes.

O banco de dados passou a ser a **fonte única de verdade**: todo o conteúdo (STIX, attack flows, abilities, adversaries, memória de experiência) é lido do banco em tempo de request — o runtime não lê mais disco.

---

## 2. Marco 1 — Fluxo completo das campanhas curadas (seis estágios)

A arquitetura foi consolidada em **seis estágios** sequenciais, cada um com Entrada/Processamento/Saída explícitos e bloqueio sequencial (um estágio só abre quando o anterior conclui):

| Estágio | Nome | O que faz |
|---|---|---|
| 1 | Seleção da base STIX | Escolhe a versão do STIX, que é o próprio pacote ATT&CK-in-STIX (ex.: Enterprise v18) como `Pipeline_Input`. |
| 2 | Seleção de campanha | Lista as 52 campanhas persistidas do pacote e extrai identidade/técnicas/atores da campanha escolhida. |
| 3 | Geração do attack flow | Monta o grafo de técnicas (estrutura do ataque), no formato MITRE Attack Flow. |
| 4 | Operacionalização | Traduz técnicas abstratas em abilities executáveis (comandos concretos + ordenação do adversary). **É a lacuna que a pesquisa automatiza.** |
| 5 | Criação do ambiente | Provisiona o ambiente contido (contêineres), verifica isolamento (redes `internal: true`). |
| 6 | Execução + medição + relatório | Roda a operação no Caldera dentro do lab isolado, mede suficiência procedural e gera o relatório. |

**Contenção de segurança (inegociável):** toda emulação ocorre em redes Docker `internal: true` (sem egresso). As verificações de contenção e isolamento são feitas em código, não confiadas ao agente: validação de destino (nenhum comando atinge host/alvo externo), inspeção de isolamento do contêiner (read-only) e confirmação explícita antes de executar.

As **8 campanhas curadas manualmente** (APT41-DUST, C0010, C0026, CostaRicto, Operation MidnightEclipse, Operation Outer Space, Salesforce Data Exfiltration, ShadowRay) servem como **ground truth**: a curadoria gerada por IA é comparada com a humana para medir a suficiência procedural.

---

## 3. Marco 2 — Ambiente de multiagentes de IA

O Estágio 2 (curadoria) foi automatizado por um **sistema de múltiplos agentes** orquestrado com **LangGraph** (`app/services/ai/`). O princípio de projeto é **"o agente propõe; a máquina de código dispõe"**: o LLM gera propostas, mas os portões de segurança/contenção, limites de recurso e isolamento são autoridade do código, nunca do agente.

### 3.1 Orquestração (grafo de estados)

O grafo (`graph.py`) é um `StateGraph` do LangGraph com os nós:

```
orchestrate → operationalize → flow → environment → execute → evaluate
                    ↑________________________________________________|
                           (loop condicional de refinamento)
```

- **orchestrate** — monta o `Agent_Input` da iteração (contexto base + experiência recuperada) e inicializa os contadores do loop.
- **operationalize (Operationalization_Agent)** — resolve comandos concretos por técnica via o `Command_Base` (consulta relacional exata; a similaridade vetorial é apoio secundário). Vem **antes** do fluxo.
- **flow (Flow_Agent)** — produz o *attack flow* da campanha (formato MITRE Attack Flow: Attack Flow, MITRE Center for Threat-Informed Defense, https://center-for-threat-informed-defense.github.io/attack-flow/) segundo a política de três caminhos (`exact`, `adapted`, `created`), registrando a proveniência; técnicas sem comando de respaldo viram `gaps` (o agente nunca inventa comando).
- **environment (Environment_Agent)** — propõe o ambiente contido; detecta `Target_OS`, resolve imagem (Docker Hub online → catálogo offline) e passa pelo `VM_Confirmation_Gate` (sempre contêiner).
- **execute** — roda a operação no Caldera dentro do lab e serializa o relatório do Estágio 6.
- **evaluate (Evaluator)** — lê o relatório, aplica a métrica de suficiência procedural e decide **FINALIZAR** (veredito passou) ou **REFINAR** (escolhe exatamente um estágio para re-entrar).

O loop é limitado pelo **Max_Iterations_Guard** (padrão 3 iterações): se o veredito passa, o run termina `COMPLETED`; se o limite é atingido sem sucesso, termina `ABORTED`. Cada iteração registra o estágio refinado e o motivo (rationale).

### 3.2 Persistência e memória (duas memórias — "Memory_Rule")

O sistema separa estritamente dois tipos de memória:

**(a) Memória estruturada/exata (relacional, PostgreSQL)** — fatos: o grafo de técnicas, as abilities/adversaries, o mapeamento técnica→comando (`technique_commands`), o catálogo de imagens, o mapa de provisionamento, e toda a bookkeeping dos runs de IA:
- `agent_runs` → `agent_iterations` → `agent_stage_outputs` (+ `agent_stage_traces` com o Audit_Trace 1:1) e `agent_evaluations`.

**(b) Memória associativa/vetorial (pgvector)** — conhecimento recuperado por similaridade, na tabela `experience_memory`:
- afinidades comando↔ferramenta (ingeridas do Atomic Red Team),
- "ambientes que funcionaram" por campanha,
- contexto STIX por versão do STIX (`stix_context`),
- resultados de runs passados (sucesso/falha).

**Curto prazo vs longo prazo:**
- **Curto prazo:** o estado do grafo (`AgentState`) é "fotografado" pelo **checkpointer PostgreSQL** do LangGraph após cada nó → o run é **resumível** (sobrevive a reinício).
- **Longo prazo (RAG):** a `ExperienceMemory` recupera, por **distância de cosseno**, as experiências mais similares de **outras** campanhas (exclui a campanha corrente) para primar as propostas — aprendizado **entre** campanhas sem alterar pesos de modelo.

### 3.3 Vetorização (embeddings)

- Embeddings gerados pelo **`nomic-embed-text`** (via Ollama), **768 dimensões** — a coluna `experience_memory.embedding` é um `VECTOR(768)` do pgvector, com índice IVFFlat de cosseno.
- A ingestão é **idempotente** por uma `dedup_key` determinística (hash do conteúdo), então reprocessar a mesma fonte não duplica linhas.
- O `Command_Base` resolve comandos por **mapeamento exato** (tabela relacional) e, na ausência, cai para **similaridade vetorial** (RAG) — nunca inventa.

### 3.4 Uso do LLM

- Cliente **OpenAI-compatível** (LangChain `ChatOpenAI`) apontado para o endpoint da **SEPINF** (`SEPINF_BASE_URL`/`SEPINF_MODEL_ID`/`SEPINF_API_KEY`), construído de forma **lazy** (nenhuma conexão na importação; a chave nunca é logada).
- **Temperatura 0.0** para planejamento determinístico.
- Cada agente faz no máximo **uma tentativa de reparo** quando a saída do LLM não parseia num artefato válido (ex.: attack flow malformado) — além disso, falha de forma auditável.
- O LLM atua como **tradutor de comportamento→comando** e **planejador**, mas toda decisão de segurança/execução é do código.

### 3.5 Entrada dos agentes (Agent_Input) e auditoria

O `Agent_Input` de cada agente é montado **100% a partir do banco** (sem disco): identidade/técnicas/atores do `Campaign_Overview` (relacional) + itens recuperados por similaridade (vetorial). Cada saída de agente é persistida com um **Audit_Trace 1:1** (`agent_stage_traces`) contendo o input recebido e o processamento (prompt enviado, resposta do LLM, ferramentas invocadas, tentativas de reparo). Isso torna cada contribuição da IA **totalmente auditável** por estágio e iteração, e permite comparar com a curadoria humana (Ground_Truth_Comparison).

---

## 4. Banco de dados como fonte única de verdade

Um trabalho estrutural importante foi **eliminar as leituras de disco em tempo de request**. Antes, o runtime lia arquivos JSON curados do disco; agora, todo o conteúdo é ingerido uma vez para o banco e servido de lá (STIX, attack flows, abilities, adversaries). Isso foi comprovado por um teste de integração com **probe de filesystem** que falha se o runtime abrir os diretórios de dados curados durante um request.

A evolução do esquema está registrada em **18 migrações Alembic** encadeadas, incluindo: criação do domínio, renumeração dos estágios (1→4→5→6), criação das tabelas de IA + habilitação do pgvector, padronização do esquema para inglês, persistência do pacote STIX, alinhamento do embedding para 768, e as tabelas do `Command_Base`/provisionamento (`technique_commands`, `image_catalog`, `provisioning_map`).

---

## 5. Qualidade e testes

A base de testes cobre as invariantes críticas: contenção de rede, isolamento de contêiner, integridade de FK, idempotência de ingestão/migração, término do loop do avaliador e equivalência curado banco↔disco. Os testes rodam de forma isolada (SQLite efêmero), sem Docker/rede/LLM reais, usando dublês — exceto o E2E de laboratório, que é executado na máquina real.

---

## 6. Próximos passos

- Ampliar a avaliação de **suficiência procedural** comparando sistematicamente a curadoria gerada por IA com as 8 campanhas curadas (ground truth).
- Ingestão de inteligência de ameaça externa (`ThreatIntelIngestService`) para enriquecer o `Command_Base`.
- Rodar as 52 campanhas não curadas pelo pipeline de IA e medir a lacuna procedural em escala.
