# Implementation Plan — Wizard de Execução Guiada

## Overview

Fecha as duas lacunas de backend (concluir Estágio 2; marcar Estágio 3 concluído pós-emulação) mais um endpoint de reset, e adiciona o componente `ExecutionWizard` no frontend reaproveitando `Stage1/2/3View` e o `EmulationConfirmModal`. Cada tarefa é incremental e test-driven, delegando a conclusão de estágio ao `SessionStateService.persist_stage_completion` já existente. A suíte atual (188 backend + 17 frontend) não pode quebrar.

## Tasks

- [ ] 1. Backend — endpoint de conclusão do Estágio 2
  - [ ] 1.1 Implementar `POST /api/casos/{caso}/estagio/2/concluir` em `app/api/casos.py`
    - Validar slug (404), checar via `StageService.get_stage_states` que o Estágio 1 está `concluido`; senão 409 sem persistir
    - Chamar `SessionStateService(db).persist_stage_completion(caso, 2)`; se `saved=False`, retornar 500 com a mensagem; senão retornar `CaseStageStates` atualizado
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6_

  - [ ]* 1.2 Teste de propriedade — guarda sequencial da conclusão do Estágio 2
    - **Property 2: Conclusão persiste antes de desbloquear o próximo (guarda no backend)**
    - **Validates: Requirements 3.2, 3.3**

  - [ ]* 1.3 Testes por exemplo do endpoint de conclusão
    - Caminho feliz (1 concluído → persiste 2), 404 (slug desconhecido), 500 (commit forçado a falhar com estado preservado)
    - _Requirements: 3.4, 3.6_

- [ ] 2. Backend — persistir conclusão do Estágio 3 após emulação
  - [ ] 2.1 Ajustar `executar_emulacao` em `app/api/emulacao.py`
    - Após o `run`, quando `result.outcome is COMPLETED` e `result.state is concluida`, chamar `SessionStateService(db).persist_stage_completion(caso, 3)`
    - Garantir que ramos ABORTED/NOT_CONFIRMED/409/503 NÃO persistem; falha ao persistir a conclusão não derruba a resposta de sucesso
    - _Requirements: 4.4, 4.5_

  - [ ]* 2.2 Testes por exemplo da conclusão pós-emulação (runner mockado)
    - `COMPLETED` persiste `StageRun(caso,3)=concluido`; `ABORTED` e `NOT_CONFIRMED` não persistem (reusa overrides/MockTransport)
    - _Requirements: 4.4, 4.5_

- [ ] 3. Backend — endpoint de reset de campanha
  - [ ] 3.1 Implementar `POST /api/casos/{caso}/reset` em `app/api/casos.py`
    - Validar slug (404); em transação única remover `StageRun` da campanha e `Operation`/`AbilityResult` com `caso_id == caso`; retornar `CaseStageStates` recém-derivado
    - _Requirements: 6.1, 6.2_

  - [ ]* 3.2 Teste de propriedade — reset retorna ao estado inicial
    - **Property 3: Reset retorna ao estado inicial (round-trip / idempotência)**
    - **Validates: Requirements 6.1**

- [ ] 4. Checkpoint — backend
  - Rodar `pytest` completo (188 testes não podem quebrar) e os novos testes. Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Frontend — wrappers de API e derivação de estado
  - [ ] 5.1 Adicionar `concluirEstagio2(caso)` e `resetCaso(caso)` em `src/lib/estagios.ts`
    - Estender o arquivo sem editar exports existentes; tipar resposta como estado por estágio
    - _Requirements: 3.1, 6.3_

  - [ ] 5.2 Implementar função pura `derivarEstados(concluidos)` com a regra do bloqueio sequencial
    - Estágio N `bloqueado` sse N>1 e N-1 não concluído; expor habilitação do "Avançar"
    - _Requirements: 1.6, 5.3_

  - [ ]* 5.3 Teste de propriedade — derivação do bloqueio sequencial
    - **Property 1: Derivação do bloqueio sequencial**
    - **Validates: Requirements 1.6, 5.3**

- [ ] 6. Frontend — componente ExecutionWizard
  - [ ] 6.1 Criar `ExecutionWizard` reaproveitando Stage1/2/3View e lendo estado do backend
    - Estado local mínimo (`estagioAtual`); derivar badges/controles de `derivarEstados`; "Voltar" (N>1) e "Avançar" (próximo não bloqueado); relê o backend após cada transição
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 5.1, 5.2, 5.4_

  - [ ] 6.2 Ligar as ações aos endpoints
    - Estágio 1: "Executar modelagem" → `POST estagio/1/executar`, habilitar avanço ao concluir; Estágio 2: "Avançar" → `concluirEstagio2`; Estágio 3: "Executar emulação" abre `EmulationConfirmModal`; "Reiniciar" → `resetCaso`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.5, 4.1, 4.2, 4.3, 4.6, 6.3_

  - [ ] 6.3 Montar o ExecutionWizard em `App.tsx` quando houver campanha selecionada
    - Manter o `EmulationConfirmModal` existente no nível do App; não quebrar a seleção de caso atual
    - _Requirements: 1.1_

  - [ ]* 6.4 Testes de componente do Wizard
    - Navega 1→2→3; "Avançar" desabilitado quando próximo bloqueado; "Executar emulação" abre o modal; Saída do Estágio 3 renderiza resultados por Ability após sucesso
    - _Requirements: 1.3, 1.6, 2.3, 4.2, 4.6_

- [ ] 7. Checkpoint final
  - Rodar `pytest` e `vitest --run` (17 testes frontend não podem quebrar). Ensure all tests pass, ask the user if questions arise.

## Notes

- Tarefas marcadas com `*` são opcionais (testes) e podem ser puladas para um MVP mais rápido da demo.
- Toda conclusão de estágio passa por `SessionStateService.persist_stage_completion` (atomicidade e preservação de estado já garantidas).
- O pre-flight de contenção do Estágio 3 permanece dentro de `OperationRunner.run`; o Wizard apenas aciona o endpoint de emulação.
- Nenhuma mudança de contrato da API é exigida pela futura migração a PostgreSQL.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "3.1", "5.1", "5.2"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.2", "3.2", "5.3", "6.1"] },
    { "id": 2, "tasks": ["6.2"] },
    { "id": 3, "tasks": ["6.3"] },
    { "id": 4, "tasks": ["6.4"] }
  ]
}
```
