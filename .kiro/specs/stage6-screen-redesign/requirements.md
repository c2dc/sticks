# Requirements Document

## Introduction

Esta spec refina a **tela do Estágio 6 — Execução, medição e relatório** do pipeline de seis
Estágios do STICKS, construindo sobre a spec `stage6-execution-redesign` (execução automática,
acompanhamento técnica a técnica ao vivo, separação Estágio 5/6). O objetivo é reorganizar o
**layout** e o **conjunto de componentes** da página do Estágio 6 para remover redundância,
melhorar a leitura e encerrar o fluxo nesta tela com todos os componentes preenchidos e
atualizados.

As decisões abaixo foram levantadas em uso real com a campanha curada **ShadowRay**
(base ATT&CK v18.0), observando a tela do Estágio 6 enquanto a execução acontecia.

### Estado atual (observado no código)

A tela do Estágio 6 é composta por dois componentes empilhados verticalmente:

- `Stage3View` — três seções lado a lado em um grid de 3 colunas: **Input** (adversário +
  Abilities), **Processing** (lista de Abilities ao vivo: comando/alvo/atacante/estado/logs) e
  **Output** (apenas os totais agregados de sucesso/falha).
- `ReportView` — carregado na montagem (`GET /report`), com quatro seções: **verdict**
  (titulado "relatório"/"Execution Report", com veredito geral + flag de desprovisionamento),
  **Procedural Sufficiency Metrics** (taxa de sucesso, cobertura procedural, tempo total,
  contagem de nós falhos), **Failed Nodes** (lista dos nós/técnicas que falharam) e
  **Telemetry by Technique** (status, etapa de falha, tempo e sinais observados por técnica).

### Fatos técnicos relevantes

- O tempo total de execução é **facilmente coletável**: a entidade `Operation` já persiste
  `started_at` e `finished_at`; o tempo total é `finished_at - started_at`.
- Os campos `telemetry` e `execution_time_s` por técnica **não são produzidos** pelo caminho
  real de execução (`OperationRunner`); hoje chegam sempre vazios/nulos.
- O desprovisionamento já é sempre executado ao fim do Estágio 6 e produz um
  `DeprovisioningResult` com `deprovisioned` (bool), `remaining_resources` (recursos que
  sobraram em caso de falha), `origin` e `message`. O `GET /report` hoje só expõe o booleano
  `environment_deprovisioned`.

### Escopo

Incluído: reorganização do layout do Estágio 6 (Input/Processing na horizontal como linhas),
remoção do componente Output, remoção da seção Telemetry by Technique, remoção da lista Failed
Nodes (mantendo a métrica de contagem), coleta e exibição do tempo total de execução,
transformação do antigo "Execution Report" em um **relatório de desprovisionamento**, e o
gating das métricas e do relatório de desprovisionamento para aparecerem **somente após** o
término da execução.

Fora de escopo: o motor de execução, os gates de segurança, o provisionamento (Estágio 5), a
curadoria das Abilities e a correção do caminho de atualização ao vivo dos status
(responsabilidade da spec `stage6-execution-redesign`).

## Requirements

### Requirement 1: Layout horizontal de Input e Processing

**User Story:** Como Pesquisador acompanhando a execução, quero os componentes Input e
Processing dispostos na horizontal (como linhas que ocupam a largura da página), para que a
visualização da execução fique mais legível do que em colunas estreitas.

#### Acceptance Criteria

1. QUANDO a tela do Estágio 6 é exibida ENTÃO o sistema DEVE apresentar as seções Input e
   Processing empilhadas verticalmente (uma por linha, cada uma ocupando a largura disponível),
   e NÃO lado a lado como colunas de um grid.
2. QUANDO a seção Processing lista as Abilities em execução ENTÃO o sistema DEVE preservar o
   conteúdo por Ability já existente (comando concreto, alvo, host atacante, estado e
   logs/saída), sem duplicação entre as linhas.
3. O sistema DEVE manter o layout responsivo, sem quebra visual, em larguras de tela típicas de
   desktop.

### Requirement 2: Remoção do componente Output

**User Story:** Como Pesquisador, quero que o componente Output seja removido, porque os totais
de sucesso/falha que ele mostra são redundantes com as métricas e com a lista ao vivo.

#### Acceptance Criteria

1. QUANDO a tela do Estágio 6 é exibida ENTÃO o sistema NÃO DEVE renderizar a seção Output
   (totais agregados de sucesso/falha) em nenhum estado da execução.
2. O sistema DEVE garantir que a remoção do Output não elimina nenhuma informação única: os
   totais de sucesso/falha permanecem disponíveis via a seção de métricas (contagem) e a lista
   ao vivo de Abilities.

### Requirement 3: Métricas de suficiência procedural somente após o término

**User Story:** Como Pesquisador, quero ver as métricas de suficiência procedural somente depois
que a execução da campanha terminar, com os valores corretos da execução, para não ver métricas
zeradas ou parciais durante a execução.

#### Acceptance Criteria

1. ENQUANTO a execução do Estágio 6 não tiver terminado O sistema NÃO DEVE exibir a seção de
   métricas de suficiência procedural.
2. QUANDO a execução do Estágio 6 terminar (estado terminal da Operação) ENTÃO o sistema DEVE
   exibir a seção de métricas com os valores corretos derivados da execução concluída.
3. A seção de métricas DEVE conter: taxa de sucesso, cobertura procedural, tempo total de
   execução e a contagem de nós/técnicas que falharam.
4. A seção de métricas NÃO DEVE listar os nós falhos individualmente (ver Requirement 5); apenas
   a contagem é exibida.

### Requirement 4: Tempo total de execução coletado e exibido

**User Story:** Como Pesquisador, quero ver o tempo total que a execução levou, para avaliar o
custo temporal da emulação.

#### Acceptance Criteria

1. QUANDO a execução termina ENTÃO o sistema DEVE calcular o tempo total como a diferença entre
   o instante de término e o instante de início da Operação (`finished_at - started_at`).
2. QUANDO o tempo total está disponível ENTÃO o sistema DEVE exibi-lo no card de tempo total da
   seção de métricas, formatado de forma legível (ex.: segundos).
3. SE o instante de início ou de término não estiver disponível ENTÃO o sistema DEVE exibir um
   indicador de ausência (ex.: "—") no card, sem erro.

### Requirement 5: Remoção da lista de nós falhos (mantendo a contagem)

**User Story:** Como Pesquisador, quero remover a lista de Failed Nodes, porque esses nós são as
técnicas ordenadas no DAG e eu já consigo ver qual falhou na lista ao vivo (Processing); mas
quero manter a métrica de contagem de nós falhos.

#### Acceptance Criteria

1. QUANDO a tela do Estágio 6 é exibida ENTÃO o sistema NÃO DEVE renderizar a seção que lista os
   nós/técnicas falhos individualmente.
2. O sistema DEVE manter a **contagem** de nós falhos como um card na seção de métricas
   (Requirement 3).
3. A identificação de qual técnica falhou DEVE permanecer disponível ao Pesquisador pela lista
   ao vivo de Abilities (Processing), com o estado de falha por Ability.

### Requirement 6: Remoção da telemetria por técnica

**User Story:** Como Pesquisador, quero remover a seção Telemetry by Technique, porque ela não
traz informação nova: os sinais e o tempo por técnica não são produzidos pelo caminho real de
execução (chegam sempre vazios) e o status por técnica já aparece na lista ao vivo.

#### Acceptance Criteria

1. QUANDO a tela do Estágio 6 é exibida ENTÃO o sistema NÃO DEVE renderizar a seção Telemetry by
   Technique.
2. O sistema DEVE garantir que nenhuma informação única é perdida: o status por técnica
   permanece na lista ao vivo (Processing) e a contagem agregada permanece nas métricas.

### Requirement 7: Relatório de desprovisionamento ao fim do fluxo

**User Story:** Como Pesquisador, quero que o antigo "Execution Report" seja transformado em um
**relatório de desprovisionamento do ambiente**, exibido somente depois que a execução terminar
e os resultados forem apresentados, encerrando o fluxo na tela do Estágio 6 com todos os
componentes preenchidos e atualizados.

#### Acceptance Criteria

1. ENQUANTO a execução do Estágio 6 não tiver terminado O sistema NÃO DEVE exibir o relatório de
   desprovisionamento.
2. QUANDO a execução termina e as métricas são apresentadas ENTÃO o sistema DEVE exibir, por
   último, o relatório de desprovisionamento do ambiente.
3. O relatório de desprovisionamento DEVE indicar se o ambiente foi desprovisionado com sucesso
   e, em caso de falha, DEVE identificar os recursos remanescentes (contêineres/redes) para o
   Pesquisador.
4. O sistema NÃO DEVE mais exibir o veredito de execução no formato do antigo "Execution Report"
   (verdict genérico), dado que essa informação já é transmitida pelas métricas e pela lista ao
   vivo.
5. QUANDO todos os componentes acima estiverem preenchidos e atualizados ENTÃO o fluxo DEVE
   permanecer (parar) nesta tela do Estágio 6, sem avançar para outra tela.

### Requirement 8: Ordem e integridade da página do Estágio 6

**User Story:** Como Pesquisador, quero que a página do Estágio 6 apresente as informações em
uma ordem coerente do começo ao fim da execução, para acompanhar o fluxo sem confusão.

#### Acceptance Criteria

1. QUANDO a tela do Estágio 6 é exibida ENTÃO o sistema DEVE apresentar as seções na ordem:
   (1) Input, (2) Processing (lista ao vivo), (3) Métricas de suficiência procedural
   (após o término), (4) Relatório de desprovisionamento (após o término).
2. QUANDO a execução está em andamento ENTÃO o sistema DEVE exibir apenas Input e Processing; as
   seções de métricas e de desprovisionamento permanecem ocultas até o término.
3. O sistema DEVE manter o comportamento de execução automática e acompanhamento ao vivo já
   definido pela spec `stage6-execution-redesign`, sem reintroduzir modal/pop-up ou botão de
   confirmação.

## Glossary

- **Estágio 6**: etapa final do pipeline (Execução, medição e relatório), onde a campanha curada
  é emulada contra o ambiente provisionado no Estágio 5.
- **Input**: seção da tela que lista o adversário e as Abilities (técnicas) a emular.
- **Processing**: seção da tela com a lista ao vivo de Abilities (comando, alvo, host atacante,
  estado e logs), atualizada técnica a técnica durante a execução.
- **Output**: seção (a ser removida) que exibia apenas os totais agregados de sucesso/falha.
- **Ability**: unidade executável de uma técnica ATT&CK na emulação (um comando/alvo).
- **Métricas de suficiência procedural**: taxa de sucesso, cobertura procedural, tempo total e
  contagem de técnicas falhas, derivadas da execução concluída.
- **Nó falho (failed node)**: técnica do DAG cujo resultado foi falha; identificada na lista ao
  vivo (Processing).
- **Telemetry by Technique**: seção (a ser removida) que exibia sinais observados e tempo por
  técnica — campos não produzidos pelo caminho real de execução.
- **Desprovisionamento (deprovision)**: teardown do ambiente ao fim do Estágio 6 (remoção dos
  contêineres/redes criados para a campanha).
- **Relatório de desprovisionamento**: seção final que informa se o ambiente foi removido e,
  em caso de falha, quais recursos remanesceram.
- **Operação (Operation)**: entidade que representa uma execução da campanha no Caldera; carrega
  `started_at` e `finished_at`.
