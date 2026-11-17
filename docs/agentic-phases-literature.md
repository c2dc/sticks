# Fundamentação acadêmica — fases do sistema agentic-centric do STICKS

> Documento de referência para o trabalho de doutorado. Registra as fontes
> acadêmicas que embasam (a) a contagem/nomenclatura das fases do sistema
> agêntico e (b) a decisão de manter Execução e Avaliação como fases
> **separadas**. Toda afirmação de projeto sobre "quantas fases" ou "por que
> separar execução de avaliação" deve citar este documento.
>
> Conteúdo reescrito/parafraseado a partir das fontes para conformidade com
> restrições de licenciamento; nenhuma citação literal acima de limites.

## Fontes

- **[F1]** Masterman, T. et al. (2024). *The Landscape of Emerging AI Agent
  Architectures for Reasoning, Planning, and Tool Calling.* arXiv:2404.11584.
  https://arxiv.org/abs/2404.11584
- **[F2]** Xu, B. (2026). *AI Agent Systems: Architectures, Applications, and
  Evaluation.* arXiv:2601.01743. https://arxiv.org/abs/2601.01743
- **[F3]** Yao, S. et al. (2023). *ReAct: Synergizing Reasoning and Acting in
  Language Models.* ICLR 2023. arXiv:2210.03629
- **[F4]** Shinn, N. et al. (2023). *Reflexion: Language Agents with Verbal
  Reinforcement Learning.* NeurIPS 2023. arXiv:2303.11366
- **[F5]** Chen, W. et al. (2023). *AgentVerse: Facilitating Multi-Agent
  Collaboration and Exploring Emergent Behaviors.* arXiv:2308.10848
- **[F6]** Miller, D., Alford, R., Applebaum, A., Foster, H., Little, C. &
  Strom, B. (MITRE). *Automated Adversary Emulation: A Case for Planning and
  Acting with Unknowns.* The MITRE Corporation.
  https://www.mitre.org/sites/default/files/2021-11/prs-18-0944-1-automated-adversary-emulation-planning-acting.pdf
- **[F7]** The MITRE Corporation. *MITRE Caldera documentation* (adversary
  emulation platform). https://caldera.readthedocs.io/en/stable/

## Pergunta 1 — Execução e Avaliação: juntas ou separadas?

**Decisão: SEPARADAS.** Execução (rodar a emulação e observar o resultado) e
Avaliação (julgar o resultado e decidir refinar ou concluir) são fases
distintas.

Fundamentação:

- **[F1]** conclui que a maioria das implementações de agentes segue o processo
  **planejar → agir → avaliar** ("plan, act, and evaluate") de forma iterativa,
  tratando a avaliação como passo distinto da ação. O framework AgentVerse
  **[F5]**, citado por [F1], define quatro fases explícitas, entre eles
  "independent action execution" e "evaluation" como fases **separadas**, que se
  repetem até o objetivo ser alcançado.
- **[F2]** modela o agente como um laço de controle com interfaces explícitas a
  ferramentas e a **verificadores/críticos** (verifiers/critics) — ou seja, o
  componente que avalia é um elemento distinto do que executa a ação. O laço
  mínimo descrito é: recuperar contexto → planejar → **agir via ferramentas** →
  **verificar** → atualizar memória → repetir. "Agir" e "verificar" são passos
  distintos.
- **[F2]** reforça o motivo da separação no nosso domínio: a avaliação precisa
  ser **ponta-a-ponta** — julgar se o fluxo de fato completou corretamente, não
  se o texto "parece plausível". Num sistema de emulação de adversário, isso
  significa que a Avaliação deve observar o **resultado real** da Execução
  (veredito do runner, contenção, isolamento), o que só é possível se ela for um
  passo que roda **depois** da Execução e sobre a saída dela.
- O padrão **Reflexion [F4]** institui um avaliador/crítico separado que produz
  feedback para a próxima iteração — é exatamente o laço de refinamento
  (`route_after_eval` / `max_iterations`) que o STICKS já implementa.

Consequência para o STICKS: o veredito (hoje acoplado na fase de execução do
`GuidedRunView` como "Execução + veredito") deve ser promovido a uma **fase de
Avaliação própria**, que recebe a saída da Execução e decide concluir ou
re-entrar no laço.

## Pergunta 2 — Fases do agente e de onde começam

A literatura **não fixa um número universal** de fases nem numera o pipeline:
ela descreve um *ciclo agêntico* (agentic loop) [F1][F2]. O núcleo recorrente é
**planejar → agir (via ferramentas) → avaliar**, com um **laço de refinamento**
realimentando o planejamento [F1][F3][F4]. Por isso as fases do STICKS têm
**nomes semânticos, sem número**.

A seleção de base STIX e de campanha **não são fases do agente**: são a
**entrada** (`input`) — o humano escolhe o alvo. O agente só passa a atuar
depois. As fases agênticas começam na **operationalization** (os comandos
Atomic de cada técnica da campanha) e só então vem o **flow** (o *attack flow*).

### Fases do STICKS (nomes semânticos, sem numeração)

`input` (não é fase do agente) → então o laço agêntico, nesta ordem
(`input → operationalization → flow → environment → execution → evaluation`):

| Fase (projeto)        | Papel na literatura            | Fonte        |
|-----------------------|--------------------------------|--------------|
| **Operationalization**| ação via ferramentas (tool use)| [F1][F2][F3] |
| **Flow** (attack flow)| planejamento (decomposição)    | [F1][F3][F8] |
| **Environment**       | ação via ferramentas (provisão)| [F1][F2]     |
| **Execution**         | ação/observação do resultado   | [F1][F2]     |
| **Evaluation**        | avaliação/crítico + laço        | [F1][F2][F4] |

Ordem Operationalization → Flow: a ordem anterior colocava o planejamento
(grafo de técnicas) antes dos comandos. Hoje o pesquisador vê primeiro os comandos Atomic de cada
técnica (consulta relacional exata) e valida a operacionalização passo a passo;
o *flow* é construído em seguida, já sabendo quais comandos existem para cada
técnica. A literatura não impõe uma ordem única entre decompor e agir [F1][F3];
o laço de refinamento pode reentrar em qualquer das duas fases [F4].

Observação: pela literatura, Operationalization, Environment e Execution são
todas da família "ação via ferramentas"; nós as mantemos como fases distintas
porque cada uma produz um artefato próprio e verificável, e porque a **fronteira
de rede** cai ENTRE Environment e Execution (ver abaixo) — uma decisão de
engenharia compatível com [F1], que admite múltiplos passos de ação encadeadas.

## Fronteira de rede: provisionar (egress aberto) → isolar → executar (rede interna contida)

O momento mais crítico de segurança do STICKS está na transição Environment →
Execution. **Isolamento NÃO é corte total de rede** — é corte do **egress
externo** mantendo a **rede interna do laboratório**:

- **Environment (provisionamento)** pode exigir **acesso à internet (egress
  aberto)**: se não houver imagem/contêiner já pronto para a campanha, é preciso
  puxar imagens do Docker Hub. Acontece **antes** de qualquer comando de ataque.
  Pode ser **automático** (sem confirmação manual), desde que o isolamento
  posterior seja garantido e auditado.
- **Isolamento** — concluído o provisionamento, o laboratório tem o **egress
  externo cortado** (nada vaza para a internet), de forma auditável, **mas a
  rede interna lab permanece ativa**.
- **Execution** — o Caldera (C2/executor) dispara os ataques nos targets e
  **precisa de rede para isso**: ele fala com os targets pela **rede interna
  contida**. A verificação `is_internal_ip` garante que todo destino é
  interno/contido — nunca a internet. Os comandos ofensivos rodam aqui, sem
  vazar para fora do laboratório.

Regra: a **fronteira fica entre Environment e Execution**, e isolamento significa
**sem egress externo, com rede interna preservada** (Caldera↔targets). O gate de
isolamento é explícito, imutável e auditável. Isso reforça (não contradiz) a
decisão de manter Environment e Execution como fases distintas.

**Fundamentação da fronteira de rede.** A necessidade de uma rede (interna) para
a execução vem da própria natureza da emulação automatizada de adversário: o C2
**lança o perfil do adversário numa rede** para observar os agentes nos alvos
**[F6][F7]**. A emulação de adversário é descrita como um exercício de
**planejar e agir** sobre um ambiente de rede **[F6]**, e a plataforma Caldera
opera comunicando-se com os agentes implantados nos hosts-alvo **[F7]**. Daí a
decisão de engenharia: a `execution` exige a rede interna (Caldera↔targets),
então o isolamento **não pode** ser corte total — é corte do **egress externo**
preservando a rede interna. As duas fontes são verificáveis (ver Fontes acima).
*Conteúdo das fontes [F6][F7] reescrito/parafraseado para conformidade com
restrições de licenciamento.*

## Terminologia padronizada (inglês)

Todo o código e documentação do projeto usa os termos em **inglês** (regra de
nomenclatura do repositório). Decisões fixadas:

- A fase de entrada chama-se **Input** (não "setup", não "entrada"): é a fase em
  que o usuário escolhe a versão do STIX (o pacote) e a campanha (`Input_Phase`).
  **Setup** é exclusivamente a instalação/primeira execução com a ingestão das
  bases (casos curados, Atomic Red Team, catálogo Docker, corpus Attack Flow, pacote STIX) e a
  verificação a cada start. Nenhuma tela ou fase de execução se chama "setup".
- Glossário: **setup** = instalação + ingestão inicial; **ingestion** = o processo
  idempotente em segundo plano que carrega as bases (parte de dados do setup);
  **input** = seleção por execução (STIX + campanha); **agentic phases** =
  `operationalization → flow → environment → execution → evaluation`.
- As fases agênticas: `operationalization`, `flow`, `environment`,
  `execution`, `evaluation`. O artefato da fase `flow` é chamado de
  **attack flow** (formato MITRE Attack Flow [F8]); o termo antigo de grafo
  acíclico não é mais usado, porque o corpus publicado não é estritamente acíclico.
- Os nomes das fases são semânticos, sem número: as fases agênticas começam
  na `operationalization`, e a numeração herdada do pipeline curado antigo foi
  removida.

## Setup (ingestion) vs Input — conceitos distintos

Há **três** conceitos no sistema, não dois. "Setup (instalação + ingestão)" e
"entrada (Input)" NÃO são sinônimos e NÃO devem ser unificados.

| Conceito     | Nome (inglês)   | Quando            | Natureza                          |
|--------------|-----------------|-------------------|-----------------------------------|
| Popular os bancos com as fontes de conhecimento (setup: instalação + ingestão) | **Ingestion**   | Automática no primeiro start; verificada em todo start | Processo em segundo plano, idempotente |
| Usuário escolhe versão do STIX (o pacote) + campanha | **Input**       | Por execução      | Fase 0 do fluxo (observação inicial do agente — [F1][F2]) |
| Ciclo do agente (Operationalization → Flow → … → Evaluation) | **Agentic phases** | Por execução   | O laço agêntico                   |

Relação: **Ingestion enche os bancos → Input lê deles** para montar o contexto
inicial → as fases agênticas rodam. A ingestão é pré-requisito de tudo.

Decisão: **NÃO renomear "Input" para "Ingest".** São passos diferentes, em
momentos diferentes, com consumidores diferentes. A ingestão roda
**automaticamente no primeiro start** e é **verificada em todo start**; a
entrada roda a cada campanha.

### Ingestão automática: estados e configuração

O Ingestion Orchestrator roda em segundo plano (não bloqueia a interface). No
primeiro start ingere as três fontes; nos seguintes verifica se cada base está
carregada e vetorizada e completa só o que faltar (idempotente). Cada fonte
(`stix_packages`, `curated_seed`, `atomic`, `docker_catalog`, `attack_flow`) tem um estado: `pending`, `running`,
`ready` ou `failed`. Uma falha (por exemplo, falta de rede) marca só aquela fonte
como `failed` com a causa; as demais seguem e a próxima verificação retoma o que
ficou pendente. Um segundo início concorrente da mesma fonte é rejeitado.

Variáveis de ambiente: `STICKS_INGEST_ON_START` (liga/desliga a ingestão no
start), `ATTACK_FLOW_REF` (tag do repositório Attack Flow), `GITHUB_TOKEN`
(opcional, para consultar o GitHub), `OLLAMA_BASE_URL` e `OLLAMA_EMBED_MODEL`
(serviço e modelo de embeddings).

### Fontes de ingestão (o que alimenta os bancos)

1. **Bundles STIX** (ATT&CK-in-STIX). Já existe `StixPersistenceService`.
   Pré-carregar v18.0, v19.0, v19.1 para os testes.
2. **Comandos Atomic Red Team** — repositório oficial
   `redcanaryco/atomic-red-team`. Já existe `AtomicRedTeamIngestService`
   (relacional `technique_commands` + afinidades vetoriais `experience_memory`).
3. **Casos curados** (`curated_seed`) — as 8 campanhas curadas vêm de um seed
   versionado e somente leitura (`app/seeds/data/<slug>.json`, carregado via
   `importlib.resources`), aplicado por upsert idempotente e sem rede; os dados de
   runtime vivem no banco relacional e no vetorial. Roda antes de `atomic`, cuja
   derivação de afinidades parte dos casos curados. Entrada manual:
   `python -m app.seeds.curated`.
4. **Catálogo de imagens Docker** (novo) — ver abaixo.
5. **Corpus Attack Flow** (MITRE Center for Threat-Informed Defense [F8]) — os
   fluxos publicados (`.afb`) são gravados em tabelas relacionais
   (`attack_flows`, objetos e arestas) e a descrição de cada fluxo vira um
   embedding em `experience_memory`.

## Catálogo de imagens Docker (base de ambientes de emulação)

O STICKS emula ambientes com imagens de contêiner. Precisamos de um **catálogo
curado** de imagens-alvo (não um espelho do Docker Hub inteiro).

- **Fonte:** Docker Hub REST API (`hub.docker.com/v2/...`) e a Registry API v2
  (OCI). As *Docker Official Images* (nginx, mysql, redis, httpd, postgres,
  ubuntu, …) são a parte curada e estável — as úteis para emulação de serviços
  que as campanhas ATT&CK exercitam.
  - https://docs.docker.com/reference/api/hub/latest/
  - https://docs.docker.com/reference/api/registry/latest/
  - *Conteúdo das fontes reescrito para conformidade com licenciamento.*
- **Escala (decisão do projeto):** o catálogo é **somente texto** (descrição +
  metadados + serviços), portanto pequeno. Alvo: **~10.000 descrições** de
  imagens do Docker Hub, com **teto de ~10 MB** de texto ingerido. Sem baixar
  nenhuma imagem/camada — apenas os metadados textuais.
- **Onde persistir (relacional):** tabela `image_catalog` que JÁ existe
  (`image`, `target_os`, `services`, `notes`), hoje usada como fallback offline
  do `ImageResolver`. A ingestão popula essa tabela.
- **Vetorização (nova):** embeddar a descrição + serviços de cada imagem no
  `experience_memory` (pgvector), para que a fase **Environment** recupere por
  similaridade imagens candidatas a partir da necessidade de uma técnica
  (ex.: "servidor web vulnerável" → nginx/httpd). Espelha o padrão Command_Base
  (metade relacional + metade vetorial) já usado para os comandos.
- **Quando ingerir:** automaticamente no **primeiro start** (com verificação em todo start),
  junto com STIX, Atomic Red Team e o corpus Attack Flow. Quanto mais completo o catálogo pré-ingerido, **menos**
  a fase Environment precisa ir à internet em tempo de execução.
- **Nota:** não há dataset acadêmico pronto de imagens Docker vetorizadas; o
  catálogo curado + vetorização é construído pelo próprio projeto (contribuição).
  Aqui "curado" descreve a seleção do catálogo; não há mais pipeline de "caso
  curado" no sistema (curated é apenas uma flag nas campanhas).

## Attack Flow e a política de três caminhos

O artefato da fase `flow` segue o formato **Attack Flow** [F8]: um modelo de
ações, condições, operadores e ativos ligados por referências de efeito, que
substitui o conceito anterior de grafo acíclico simples. *Descrição
parafraseada da documentação do projeto, para conformidade com restrições de
licenciamento.*

Medição do corpus publicado (registrada no design da feature): 41 fluxos
(~10 MB) e 3 deles contêm ciclos; portanto os fluxos **não são estritamente
acíclicos**. O STICKS preserva o grafo original, registra os ciclos com os
objetos envolvidos, deriva uma visão de execução acíclica e nunca encerra a
aplicação por causa de um ciclo.

Política de decisão do agente (registrada como *proveniência* do fluxo):

| Caminho    | Condição                                              | Resultado                                   |
|------------|-------------------------------------------------------|---------------------------------------------|
| `exact`    | Um fluxo documentado do corpus casa com a campanha    | Usa exatamente esse fluxo                   |
| `adapted`  | Sem casamento exato, mas há fluxos similares          | Adapta os mais similares ao fluxo mais próximo |
| `created`  | Nenhum fluxo de referência                            | O agente cria um fluxo próprio              |

Fluxos similares são buscados por similaridade vetorial (descrição do fluxo) e
por consulta relacional (fluxos estruturados). Fundamento: o ciclo
planejar → agir → avaliar [F1][F3] com reuso de experiência prévia [F4]; o
formato do fluxo vem de [F8].

## O que vetorizar no sistema (consolidado)

| Fonte                     | Relacional (fatos)     | Vetorial (busca semântica)                 | Serve à fase          |
|---------------------------|------------------------|--------------------------------------------|-----------------------|
| Comandos Atomic Red Team  | `technique_commands`   | `experience_memory` (embedding do comando) | Operationalization    |
| Campanhas/técnicas STIX   | tabelas `stix_*`       | embedding da descrição                     | Input / Flow          |
| Fluxos Attack Flow        | `attack_flows` (+ objetos e arestas) | `experience_memory` (descrição do fluxo) | Flow            |
| Imagens Docker            | `image_catalog`        | embedding descrição+serviços (novo)        | Environment           |
| Experiência de execuções  | —                      | `experience_memory` (outcome)              | Evaluation / loop     |
