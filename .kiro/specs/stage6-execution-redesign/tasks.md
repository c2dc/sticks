# Implementation Plan: Estágio 6 — Execução, medição e relatório (redesenho)

## Overview

Este plano converte o design do redesenho do Estágio 6 numa sequência incremental de tarefas de
codificação. A ordem é: primeiro as fundações de dados e configuração (enum, colunas, settings,
migrações), depois o coração da correção do bug (loop de coleta incremental no `OperationRunner`),
seguido dos serviços derivados (`ReportBuilder`, `ExecutionStageService`), do canal WS e dos
endpoints REST, e por fim o frontend (confirmação inline, acompanhamento ao vivo, relatório). Os
testes baseados em propriedade (Hypothesis) acompanham cada bloco de implementação, uma propriedade
por sub-tarefa, próximos do código que validam.

Backend em Python (FastAPI + SQLAlchemy + Hypothesis/pytest); frontend em React/TypeScript. As
mudanças são cirúrgicas e aditivas, preservando os quatro gates de pré-flight do runner.

## Tasks

- [x] 1. Fundações de dados e configuração (enum, colunas, settings, migrações)
  - [x] 1.1 Adicionar `OperationState.INCOMPLETE_TIMEOUT` e constante de estados terminais
    - Adicionar `INCOMPLETE_TIMEOUT = "incompleta_timeout"` em `app/models/enums.py`
    - Extrair/centralizar `TERMINAL_OPERATION_STATES = {"finished","out_of_time","cleanup"}` em local reutilizável (`app/services/caldera/client.py`) para o runner e o canal WS
    - _Requirements: 3.2_

  - [x] 1.2 Migração Alembic aditiva para o CHECK de `operation_state`
    - Criar migração que amplia o CHECK (`native_enum=False`/VARCHAR) de `operation_state` para incluir `incompleta_timeout`, sem alterar linhas existentes
    - _Requirements: 3.2_

  - [x] 1.3 Teste da migração do CHECK de `operation_state`
    - Verificar que `incompleta_timeout` é aceito e valores inválidos são rejeitados após upgrade; verificar downgrade
    - _Requirements: 3.2_

  - [x] 1.4 Colunas aditivas em `AbilityResult`
    - Adicionar colunas nullable `command: Text?`, `target_container: String?`, `attacker_host: String?` em `app/models/domain.py`
    - Criar migração Alembic aditiva para as três colunas
    - _Requirements: 2.4_

  - [x] 1.5 Settings de coleta em `app/core/config.py`
    - Adicionar `operation_poll_interval_seconds` (default `2.0`) e `operation_collection_timeout_seconds` (default configurável, p.ex. `600.0`)
    - Aplicar clamp 1..10 no intervalo onde o valor é consumido
    - _Requirements: 3.1_

- [x] 2. CalderaClient: agentes e estados terminais
  - [x] 2.1 DTO `AgentRef` e `list_agents`
    - Adicionar dataclass frozen `AgentRef` (`paw`, `group`, `alive`, `host`, `last_seen`, `raw`) com `from_payload`, análogo a `AdversaryRef`/`AbilityRef`
    - Implementar `list_agents(group: str | None = None) -> list[AgentRef]` consumindo `GET /api/v2/agents` e filtrando por grupo quando informado
    - _Requirements: 4.1, 4.3, 4.4_

  - [x] 2.2 Teste de propriedade — liberação por saúde de agentes
    - **Property 15: Liberação de avanço por saúde de agentes**
    - **Validates: Requirements 4.1, 4.3**
    - Gerar listas de `AgentRef` com `alive`/`group` variados via `httpx.MockTransport`; asserir liberação sse ≥ 1 `alive=true` no grupo esperado e sinalização de órfãos

  - [x] 2.3 Testes de exemplo para `list_agents` (apontamento/rotas atuais)
    - Resposta 404/host não resolvível ⇒ erro mapeado; payloads com `alive=false` ⇒ órfão detectado
    - _Requirements: 4.4, 4.5_

- [x] 3. OperationRunner: loop de coleta incremental (núcleo da correção do bug)
  - [x] 3.1 Ampliar `RunOutcome` e injetar clock/sleeper
    - Adicionar `RunOutcome.INCOMPLETE_TIMEOUT = "incompleta_timeout"` e `RunOutcome.ISOLATION_BROKEN = "isolamento_quebrado"`
    - Injetar `clock`/`sleeper` e parâmetros `collection_interval`/`collection_timeout` no `OperationRunner` (defaults vindos das settings), para testes determinísticos
    - _Requirements: 3.1, 3.2, 6.4_

  - [x] 3.2 Implementar `_reconcile(operation_id, links)` (upsert por link)
    - Fazer upsert de `AbilityResult` por `ability_id`/`link_id`: inserir novos, atualizar estados que avançaram; mapear `command`, `target_container`, `attacker_host` a partir de `OperationLink`
    - Preservar o seam de auditoria por comando (`_audit_command`), mantendo o aborto por falha de auditoria
    - _Requirements: 2.4, 6.9_

  - [x] 3.3 Teste de propriedade — particionamento de estado por link
    - **Property 8: Estado por Ability é exatamente um dos quatro**
    - **Validates: Requirements 2.1**
    - Gerar `status_code` arbitrários (incl. `None`, `-3`, `0`, outros) e asserir mapeamento total e disjunto de `_derive_link_status`

  - [x] 3.4 Testes de exemplo de `_derive_link_status` (sentinelas)
    - `None`→pendente, `-3`→em_execucao, `0`→sucesso, outro→falha
    - _Requirements: 2.1_

  - [x] 3.5 Implementar `_collect_until_terminal(operation_id, started_at)`
    - Substituir `poll_until_complete` + iteração única por laço chamando `get_operation` em intervalo `clamp(valor,1,10)`; reconciliar a cada poll
    - Parar no primeiro estado terminal (`TERMINAL_OPERATION_STATES`) ou ao decorrer o timeout contado do início da Operação
    - Terminal: reconciliação final para contagem persistida == links terminais, marcar `FINISHED`; timeout: marcar `INCOMPLETE_TIMEOUT` preservando parciais e sem contar como sucesso
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 3.6 Teste de propriedade — contagem persistida iguala links terminais
    - **Property 1: Contagem persistida iguala links terminais (correção do bug)**
    - **Validates: Requirements 3.3**

  - [x] 3.7 Teste de propriedade — parada no primeiro terminal e clamp de intervalo
    - **Property 2: Loop de coleta para no primeiro terminal e respeita o clamp de intervalo**
    - **Validates: Requirements 3.1**

  - [x] 3.8 Teste de propriedade — timeout produz INCOMPLETE_TIMEOUT e preserva parciais
    - **Property 3: Timeout sem terminal produz INCOMPLETE_TIMEOUT e preserva parciais**
    - **Validates: Requirements 3.2**

- [x] 4. OperationRunner: reverificação de isolamento durante a execução
  - [x] 4.1 Reverificação periódica fail-closed no laço de coleta
    - A cada iteração, se decorreram ≥ `isolation_recheck_interval` (≤ 30s), chamar `self._verify_isolation(...)` de novo
    - Em quebra: interromper sem novos comandos, preservar parciais, marcar `ISOLATION_BROKEN` + estado `ABORTED` identificando o contêiner afetado
    - Preservar intactos os quatro gates de pré-flight e sua ordem
    - _Requirements: 6.3, 6.4_

  - [x] 4.2 Teste de propriedade — fail-closed de disparo de comando (gate inegociável)
    - **Property 6: Fail-closed de disparo de comando (gate inegociável)**
    - **Validates: Requirements 1.2, 1.6, 6.2, 6.5**

  - [x] 4.3 Teste de propriedade — isolado sse todos os alvos satisfazem a regra
    - **Property 12: Isolamento é "isolado" sse todos os alvos satisfazem a regra**
    - **Validates: Requirements 6.1**

  - [x] 4.4 Teste de propriedade — interrupção fail-closed na quebra durante a execução
    - **Property 14: Interrupção fail-closed na quebra de isolamento durante a execução**
    - **Validates: Requirements 6.3, 6.4**

- [x] 5. Checkpoint — garantir que todos os testes de coleta/isolamento passem
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. ReportBuilder (novo serviço de relatório)
  - [x] 6.1 Implementar `build_report(operation) -> ExecutionReportView`
    - Em `app/services/operation/report.py`: taxa de sucesso, veredito determinístico, invariantes (soma == total; nós_falharam == #falhas; nenhuma técnica não-terminal em relatório final)
    - Descartar e recomputar estado persistido obsoleto (`operation_id`/esquema divergente); nunca combinar execuções; persistir `metrics`/`success_verdict` em `ExecutionReport`
    - _Requirements: 3.4, 3.5_

  - [x] 6.2 Teste de propriedade — invariantes de consistência do relatório final
    - **Property 4: Invariantes de consistência do relatório final**
    - **Validates: Requirements 3.4**

  - [x] 6.3 Teste de propriedade — estado obsoleto descartado e recomputado
    - **Property 5: Estado obsoleto é descartado e recomputado (nunca mistura execuções)**
    - **Validates: Requirements 3.5**

  - [x] 6.4 Teste de exemplo — relatório ShadowRay (11 técnicas)
    - Reproduzir o cenário do bug: terminal com todos os links coletados ⇒ relatório consistente
    - _Requirements: 3.3, 3.4_

- [x] 7. ExecutionStageService (orquestração do auto-start)
  - [x] 7.1 Implementar `autostart(campaign_id, confirmed) -> Stage6StartResult`
    - Verificar Estágio 5 concluído; idempotência (Operação terminal persistida ⇒ reexibe snapshot, sem novo `run`); delegar ao `OperationRunner.run(...)` via `OperationRunnerProtocol`
    - _Requirements: 1.1, 1.4, 1.5_

  - [x] 7.2 Teste de propriedade — idempotência da reentrada no Estágio 6
    - **Property 7: Idempotência da reentrada no Estágio 6**
    - **Validates: Requirements 1.5**

- [x] 8. Gate de saúde de agentes no Estágio 5
  - [x] 8.1 Integrar `list_agents` no serviço do Estágio 5
    - Exigir ≥ 1 agente `alive=true` no grupo esperado; detectar órfãos `alive=false`; bloquear avanço com causa específica (ausência de agente, órfão, apontamento divergente) usando endpoints/nomes atuais
    - Expor o veredito ao Estágio 6 como somente-leitura (sem reexecução no 6)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 5.1, 5.2_

  - [x] 8.2 Teste de propriedade — fail-closed do avanço do Estágio 5
    - **Property 16: Fail-closed do avanço do Estágio 5**
    - **Validates: Requirements 5.3**

  - [x] 8.3 Teste de propriedade — equivalência de isolamento entre Estágios 5 e 6
    - **Property 13: Equivalência da verificação de isolamento entre Estágios 5 e 6**
    - **Validates: Requirements 6.6**

- [x] 9. Canal de progresso WS (frames enriquecidos + estado terminal)
  - [x] 9.1 Ajustar `run_operation_progress_channel` e estados terminais
    - Incluir `OperationState.INCOMPLETE_TIMEOUT` em `_OPERATION_TERMINAL_STATES` (enviar frame final e encerrar também nesse caso)
    - Reduzir `OPERATION_PUSH_INTERVAL_SECONDS` para ≤ 2s
    - Enriquecer o frame por Ability com `command`, `target_container`, `attacker_host` (aditivo)
    - _Requirements: 2.3, 2.4_

  - [x] 9.2 Teste de propriedade — completude dos dados por Ability na serialização
    - **Property 9: Completude dos dados por Ability na serialização**
    - **Validates: Requirements 2.4, 2.5**

  - [x] 9.3 Teste de propriedade — preservação da ordem de execução (atomic ordering)
    - **Property 10: Preservação da ordem de execução (atomic ordering)**
    - **Validates: Requirements 2.6**

  - [x] 9.4 Teste de propriedade — unicidade canônica das Abilities
    - **Property 11: Unicidade canônica das Abilities (sem duplicação)**
    - **Validates: Requirements 2.7**

- [x] 10. Endpoints REST do Estágio 6
  - [x] 10.1 Implementar `POST /stage/6/execute` e `GET /stage/6`
    - `POST /stage/6/execute` (auto-start) delegando a `ExecutionStageService.autostart`, repassando `confirmed` do consentimento inline; mapear erros tipados a HTTP (503 Caldera indisponível; 409 contenção/isolamento)
    - `GET /stage/6` devolve snapshot somente-leitura (resultado persistido + resultado do Estágio 5)
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 5.2, 6.2_

  - [x] 10.2 Testes de integração — auto-start, bloqueio/redirect e separação 5↔6
    - Estágio 5 concluído ⇒ `autostart` dispara uma vez; Estágio 5 incompleto ⇒ bloqueio; asserir que verificações de ambiente não são reexecutadas no 6 (gate de isolamento do 6 continua exercido)
    - _Requirements: 1.1, 1.4, 5.1, 5.2_

- [x] 11. Checkpoint — backend completo e verde
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Frontend — contrato e cliente (`lib/stages.ts`)
  - [x] 12.1 Ampliar o contrato TypeScript do Estágio 6
    - Adicionar literal `"incompleta_timeout"` a `OperationState`; campos aditivos opcionais `command?`, `target_container?`, `attacker_host?` em `AbilityResult`/`OperationAbilityFrame`
    - Adicionar `startExecution(campaign, confirmed)` encapsulando `POST /stage/6/execute`; preservar `getStage3`/`connectOperation`
    - _Requirements: 1.1, 2.4, 3.2_

- [x] 13. Frontend — Stage6View (evolução de `Stage3View.tsx`)
  - [x] 13.1 Auto-start embutido e confirmação inline
    - Ao montar, após `getStage6`: se não há resultado persistido e pré-requisitos ok, disparar execução via consentimento inline em até 3s; se Estágio 5 incompleto, bloquear e redirecionar ao 5 em ≤ 3s
    - Criar seção `InlineExecutionConfirmation` (preview de comandos/alvos + consentimento explícito), sem `Dialog`/modal
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6_

  - [x] 13.2 Acompanhamento técnica por técnica ao vivo
    - Por Ability: exatamente um dos quatro estados; indicador de atividade em `em_execucao`; comando/alvo/host/logs com rótulo "não disponível" para dados ausentes; ordem de execução (atomic ordering); sem duplicação canônica
    - Indicador de "conexão perdida" na queda do WS e retomada sem recarregar a página; painel do Estágio 5 somente-leitura
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 5.4_

  - [x] 13.3 Remover `EmulationConfirmModal` e sua fiação
    - Remover `EmulationConfirmModal.tsx` e descontinuar seu uso em `ExecutionWizard.tsx`, substituindo pela confirmação inline
    - _Requirements: 1.3_

  - [x] 13.4 Testes de componente do Estágio 6
    - Ausência de modal/`Dialog` (1.3); confirmação inline (1.6); indicador de atividade por status (2.2); rótulo "não disponível" (2.5); painel do Estágio 5 somente-leitura (5.4); indicador de conexão perdida (2.8)
    - _Requirements: 1.3, 1.6, 2.2, 2.5, 2.8, 5.4_

- [x] 14. Frontend — ReportView
  - [x] 14.1 Garantir relatório final consistente
    - Consumir o relatório do `ReportBuilder`; nunca renderizar relatório final com técnica em estado não-terminal; contagens respeitam invariantes (soma = total; nós falhados = falhas)
    - _Requirements: 3.4_

  - [x] 14.2 Teste de componente do ReportView
    - Relatório final com técnica não-terminal é rejeitado/normalizado; contagens exibidas batem com as invariantes
    - _Requirements: 3.4_

- [x] 15. Checkpoint final — garantir que todos os testes passem
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tarefas marcadas com `*` são opcionais (testes) e podem ser puladas para um MVP mais rápido.
- Cada tarefa referencia requisitos específicos para rastreabilidade.
- Checkpoints garantem validação incremental.
- Testes de propriedade (Hypothesis) validam as 16 propriedades universais do design; cada teste usa
  dependências injetadas (`httpx.MockTransport`, inspetor de isolamento fake, relógio/`sleeper`
  injetados, SQLite efêmero) e deve ser anotado no formato
  `# Feature: stage6-execution-redesign, Property N: {texto da propriedade}` com no mínimo 100 iterações.
- Testes de exemplo/integração cobrem forma de UI e critérios de temporização (1.3, 2.2, 2.3, 2.5,
  2.8, 4.2, 4.5, 5.1, 5.4) não cobertos por propriedades.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.4", "1.5"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4"] },
    { "id": 4, "tasks": ["3.5"] },
    { "id": 5, "tasks": ["3.6", "3.7", "3.8", "4.1"] },
    { "id": 6, "tasks": ["4.2", "4.3", "4.4", "6.1"] },
    { "id": 7, "tasks": ["6.2", "6.3", "6.4", "7.1", "8.1"] },
    { "id": 8, "tasks": ["7.2", "8.2", "8.3", "9.1"] },
    { "id": 9, "tasks": ["9.2", "9.3", "9.4", "10.1", "12.1"] },
    { "id": 10, "tasks": ["10.2", "13.1", "14.1"] },
    { "id": 11, "tasks": ["13.2", "13.3", "14.2"] },
    { "id": 12, "tasks": ["13.4"] }
  ]
}
```
