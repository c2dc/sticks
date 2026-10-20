# Implementation Plan: Stage 6 Screen Redesign

## Overview

A implementação segue de baixo para cima: primeiro o backend (persistência do
desprovisionamento + migração Alembic, exposição de `total_time_s` real e do objeto
`deprovisioning` no `GET /report`), depois os tipos do cliente, depois os componentes
de frontend (gating elevado no `ExecutionWizard`, Input/Processing em linhas no
`Stage3View`, métricas + relatório de desprovisionamento com gating no `ReportView`),
terminando com a fiação da composição do Estágio 6 e os testes. Cada passo constrói
sobre o anterior e termina integrado; nenhum código fica órfão.

O backend é Python (Hypothesis para PBT) e o frontend é TypeScript/React
(fast-check + React Testing Library), conforme a Testing Strategy do design.

## Tasks

- [x] 1. Persistir o resultado de desprovisionamento (backend)
  - [x] 1.1 Estender o schema de `execution_reports` com o teardown
    - Adicionar colunas `deprovisioned` (bool, nulo por padrão) e `remaining_resources`
      (JSON) à tabela `execution_reports` no modelo ORM
      (`apps/backend/app/services/environment/` / camada de persistência do relatório)
    - Atualizar o `_persist` do relatório para gravar os campos a partir do
      `DeprovisioningResult` produzido na conclusão (`completion.py` /
      `CompletionStage6Service`)
    - _Requirements: 7.3_

  - [x] 1.2 Criar a migração Alembic para as novas colunas
    - Gerar/escrever a revisão em `apps/backend/alembic/versions` adicionando
      `deprovisioned` e `remaining_resources` a `execution_reports`, com `upgrade` e
      `downgrade`
    - _Requirements: 7.3_

  - [x] 1.3 Escrever testes unitários da persistência do teardown
    - Verificar que um `DeprovisioningResult` com falha persiste `deprovisioned=false`
      e os `remaining_resources`; e que sucesso persiste `deprovisioned=true`
    - _Requirements: 7.3_

- [x] 2. Expor tempo total real e desprovisionamento no `GET /report` (backend)
  - [x] 2.1 Estender a view de leitura das Operations com timestamps
    - Adicionar `started_at` e `finished_at` (nulos por padrão) ao `OperationResultView`
      para que `get_report` acesse os instantes por uma única porta de leitura
    - _Requirements: 4.1_

  - [x] 2.2 Calcular e injetar `total_time_s` em `get_report`
    - Em `apps/backend/app/api/campaigns.py :: get_report`, após montar as Operations,
      calcular `duration = finished_at - started_at` (em segundos) para as Operations
      finalizadas (somando durações válidas quando houver mais de uma) e injetar o valor
      em `metrics.total_time_s`, substituindo a soma de `execution_time_s`
    - Deixar `total_time_s = null` quando `started_at` ou `finished_at` estiver ausente
    - _Requirements: 4.1, 4.3_

  - [x] 2.3 Adicionar o objeto `deprovisioning` ao payload do relatório
    - Estender o `ExecutionReport` do backend com um campo `deprovisioning`
      (sucesso + `remaining_resources`), preenchido a partir do teardown persistido
      (Tarefa 1); omitir/`None` quando não houver teardown registrado; preservar o
      booleano legado `environment_deprovisioned`
    - _Requirements: 7.2, 7.3_

  - [x] 2.4 Escrever teste de propriedade para o tempo total (Hypothesis)
    - **Property 1: Tempo total deriva dos instantes da Operação**
    - **Validates: Requirements 4.1, 4.3**
    - Gerar pares `(started_at, finished_at)` com `finished_at >= started_at` e casos de
      ausência; verificar `total_time_s == (finished_at - started_at)` em segundos e
      `null` quando algum instante falta; mínimo de 100 iterações; anotar o teste com
      `Feature: stage6-screen-redesign, Property 1: Tempo total deriva dos instantes da Operação`

  - [x] 2.5 Escrever teste de API do `GET /report`
    - Cobrir `total_time_s` derivado de `started_at`/`finished_at`, timestamp ausente
      (→ `null`) e presença de `deprovisioning` com `remaining_resources` em falha de
      teardown
    - _Requirements: 4.1, 4.3, 7.2, 7.3_

- [x] 3. Checkpoint - Garantir que o backend passa nos testes
  - Garantir que todos os testes passam, perguntar ao usuário se surgirem dúvidas.

- [x] 4. Atualizar os tipos do cliente (`stages.ts`)
  - [x] 4.1 Adicionar `deprovisioning` ao `ExecutionReport` do cliente
    - Em `apps/frontend/src/.../stages.ts`, adicionar o campo opcional
      `deprovisioning?: DeprovisioningResult` (reutilizando o tipo existente com
      `deprovisioned` e `remaining_resources`), mantendo `environment_deprovisioned`
      (legado) e a semântica real de `total_time_s` (`number | null`)
    - _Requirements: 4.1, 7.3_

- [x] 5. Reorganizar `Stage3View` (Input + Processing em linhas)
  - [x] 5.1 Converter o layout de grid para linhas horizontais
    - Em `apps/frontend/src/components/Stage3View.tsx`, substituir o container
      `grid md:grid-cols-3` por `flex flex-col gap-4` (ou `space-y-4`) para que Input e
      Processing ocupem a largura total como linhas empilhadas; preservar o conteúdo por
      Ability no Processing (`AbilityLiveCard`: comando, alvo, host atacante, estado,
      logs), sem duplicação
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 5.2 Remover o bloco Output
    - Remover a seção `data-section="saida"` e a renderização dos agregados
      `liveAggregate` (totais de sucesso/falha) em qualquer estado da execução
    - _Requirements: 2.1, 2.2_

  - [x] 5.3 Emitir o sinal de término da execução
    - Adicionar a prop `onExecutionFinished?: (finished: boolean) => void` e dispará-la
      em efeito quando a Operação corrente entrar em estado terminal
      (`TERMINAL_OPERATION_STATES`) ou `phase === "persisted"`; idempotente (dispara
      `true` uma vez; volta a `false` ao recarregar outra campanha)
    - _Requirements: 3.1, 7.1, 8.2_

  - [x] 5.4 Escrever testes RTL do `Stage3View`
    - Verificar ausência do container `md:grid-cols-3` e Input/Processing como linhas
      (1.1, 1.3); conteúdo por Ability preservado sem duplicação (1.2); `data-section=
      "saida"` ausente em qualquer estado (2.1); `onExecutionFinished` disparado em
      estado terminal
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 3.1, 7.1, 8.2_

- [x] 6. Reorganizar `ReportView` (métricas + desprovisionamento com gating)
  - [x] 6.1 Adicionar a prop de gating e condicionar a busca/renderização
    - Em `apps/frontend/src/components/ReportView.tsx`, adicionar a prop
      `executionFinished: boolean`; enquanto `false`, não disparar `GET /report` nem
      renderizar métricas ou desprovisionamento (estado vazio neutro); disparar a busca
      apenas quando vira `true`
    - _Requirements: 3.1, 3.2, 7.1, 8.2_

  - [x] 6.2 Remover os blocos verdict, Failed Nodes e Telemetry by Technique
    - Remover `data-section="verdict"` (7.4), `data-section="failed-nodes"` (5.1) e
      `data-section="telemetry"` (6.1), além do `TechniqueCard`/`StatusBadge` que só
      serviam à telemetria; manter os dados subjacentes intactos (nenhuma informação
      única perdida)
    - _Requirements: 5.1, 6.1, 7.4_

  - [x] 6.3 Manter os quatro cards de métricas com tempo real e contagem de falhas
    - Preservar a seção `data-section="metricas"` com taxa de sucesso, cobertura
      procedural, tempo total (`formatTime` do `total_time_s` real; "—" quando `null`)
      e a contagem de nós falhos (`metrics.failed_nodes.length`), sem listar os nós
    - _Requirements: 3.3, 3.4, 4.2, 4.3, 5.2, 5.3_

  - [x] 6.4 Adicionar o bloco "Relatório de desprovisionamento" por último
    - Renderizar, após as métricas, um bloco que indica sucesso/falha do teardown e, em
      falha, lista `deprovisioning.remaining_resources` (chaves i18n
      `relatorio.ambiente.desprovisionado` / `naoDesprovisionado` /
      `recursosRemanescentes`); defensivo quando `deprovisioning` estiver ausente
      (estado "não desprovisionado" neutro, sem erro)
    - _Requirements: 7.2, 7.3_

  - [x] 6.5 Escrever teste de propriedade para o gating (fast-check)
    - **Property 2: Gating das seções pós-término**
    - **Validates: Requirements 3.1, 3.2, 7.1, 7.2, 8.2**
    - Gerar estados de execução (terminais e não terminais) e verificar o bicondicional:
      métricas e relatório de desprovisionamento presentes se e somente se
      `executionFinished`; mínimo de 100 iterações; anotar o teste com
      `Feature: stage6-screen-redesign, Property 2: Gating das seções pós-término`

  - [x] 6.6 Escrever testes RTL do `ReportView`
    - `executionFinished=false`: `data-section="metricas"` ausente e `getReport` não
      chamado; `true`: quatro cards presentes incluindo contagem de falhas (3.3, 3.4);
      `data-section="failed-nodes"`, `verdict` e `telemetry` ausentes (5.1, 6.1, 7.4);
      bloco de desprovisionamento presente por último com sucesso/falha e recursos
      remanescentes em falha (7.2, 7.3)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 5.1, 5.2, 6.1, 6.2, 7.2, 7.3, 7.4_

- [x] 7. Fiação da composição do Estágio 6 (`ExecutionWizard`)
  - [x] 7.1 Conectar o gating entre `Stage3View` e `ReportView`
    - Em `apps/frontend/src/components/ExecutionWizard.tsx`, manter estado
      `executionFinished` (`useState(false)`), passar `onExecutionFinished=
      setExecutionFinished` ao `Stage3View` e `executionFinished={executionFinished}` ao
      `ReportView`; garantir a ordem de renderização Input → Processing → Métricas →
      Relatório de desprovisionamento; sem reintroduzir modal/pop-up ou botão de
      confirmação; o fluxo para nesta tela
    - _Requirements: 7.5, 8.1, 8.2, 8.3_

  - [x] 7.2 Escrever teste RTL de integração do Estágio 6
    - Em andamento: apenas Input e Processing visíveis; após término: ordem completa
      Input → Processing → Métricas → Desprovisionamento; nenhum `role="dialog"`
      introduzido
    - _Requirements: 8.1, 8.2, 8.3_

- [x] 8. Checkpoint final - Garantir que todos os testes passam
  - Garantir que todos os testes passam, perguntar ao usuário se surgirem dúvidas.

## Notes

- Tarefas marcadas com `*` são opcionais (testes) e podem ser puladas para um MVP mais
  rápido; as de implementação principal nunca são opcionais.
- Cada tarefa referencia requisitos específicos para rastreabilidade.
- Os checkpoints garantem validação incremental (backend e, ao final, o conjunto todo).
- Testes de propriedade validam as duas peças com lógica genuína (tempo total e gating);
  testes por exemplo/RTL cobrem layout, remoções e ordem da UI.
- A persistência do desprovisionamento (Tarefa 1) precede sua exposição no `GET /report`
  (Tarefa 2.3) para evitar código órfão.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "5.1", "5.2", "6.2"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.2", "5.3", "6.3"] },
    { "id": 2, "tasks": ["2.3", "4.1", "6.1", "6.4"] },
    { "id": 3, "tasks": ["2.4", "2.5", "5.4", "6.5", "6.6"] },
    { "id": 4, "tasks": ["7.1"] },
    { "id": 5, "tasks": ["7.2"] }
  ]
}
```
