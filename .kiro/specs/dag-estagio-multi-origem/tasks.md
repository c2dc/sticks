# Implementation Plan: DAG como Estágio 1 persistido e multi-origem

## Overview

Converte o design em passos incrementais de código. A ordem valida a base de dados cedo (enum, modelo, migração), depois a costura `GeradorDeDag` e o `DagService`, depois o `StageService` renumerado para 1..4, em seguida os endpoints REST e, por fim, o frontend (flow/wizard/Estágio 1). Cada passo se apoia nos anteriores e termina costurando as peças ao fluxo existente, sem código órfão. O backend fica em `ui/backend`, o frontend em `ui/frontend`; títulos em inglês, descrições em pt-BR. Testes de propriedade usam Hypothesis (backend) e fast-check (frontend), ≥100 iterações, com a tag `Feature: dag-estagio-multi-origem, Property {n}`.

## Tasks

- [x] 1. Modelo de dados: enum `OrigemDag`, entidade `Dag` e FK `dag_escolhido_id`
  - [x] 1.1 Adicionar o enum `OrigemDag` em `ui/backend/app/models/enums.py`
    - Definir `OrigemDag(str, enum.Enum)` com `CURADORIA_HUMANA = "curadoria_humana"` e `GERADO_IA = "gerado_ia"`, seguindo exatamente o precedente de `TranslationSource` (valores pt-BR, `str` Enum)
    - Documentar que incluir `GERADO_IA` desde já faz o `CHECK` aceitar `gerado_ia` sem migração futura
    - _Requirements: 4.3, 4.6, 9.4_

  - [x] 1.2 Adicionar o modelo `Dag` em `ui/backend/app/models/domain.py`
    - Criar a classe `Dag(Base)` com `__tablename__ = "dags"`: `id` (PK), `caso_id` (String FK→`casos.id`), `origem` via `_portable_enum(OrigemDag, "origem_dag")` default `CURADORIA_HUMANA`, `identificador` (String), `rotulo` (String nullable), `conteudo` (JSON), `criado_em` (DateTime)
    - Garantir relação 1 Caso → N Dags e reutilizar os tipos portáveis já usados no arquivo
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 1.3 Adicionar a coluna `dag_escolhido_id` ao modelo `StageRun` em `ui/backend/app/models/domain.py`
    - Incluir `dag_escolhido_id: Mapped[Optional[int]]` (Integer FK→`dags.id`, nullable) na classe `StageRun` (`execucoes_estagio`)
    - _Requirements: 4.7_

  - [x] 1.4 Escrever testes de exemplo para enum e modelos
    - Persistir um `Dag` com `origem=gerado_ia` sem migração nova e lê-lo de volta; confirmar que `OrigemDag` cobre `{curadoria_humana, gerado_ia}`
    - _Requirements: 4.6, 9.2, 9.4_

- [x] 2. Migração Alembic: renumerar Estágios e criar esquema do DAG
  - [x] 2.1 Criar a migração encadeada em `ui/backend/alembic/versions`
    - Nova revisão com `down_revision = "a28d8c1ae4aa"`, PostgreSQL-only, usando `op.get_bind()` e SQL textual (sem SQLite)
    - `upgrade()`: (1) guarda que aborta com `RuntimeError` descritivo se houver `estagio` fora de 1..3, sem alterar dados; (2) renumerar em ordem decrescente (3→4, 2→3, 1→2) preservando `estado`, `progresso`, `mensagem_erro`, `etapa_falha`, `caso_id`; (3) `CREATE TABLE dags` com `origem` como `sa.Enum('curadoria_humana','gerado_ia', name='origem_dag', native_enum=False)`; (4) `ADD COLUMN execucoes_estagio.dag_escolhido_id` (Integer nullable FK→`dags.id`)
    - `downgrade()`: remover `dag_escolhido_id`, remover `dags`, renumerar em ordem crescente (2→1, 3→2, 4→3) preservando os demais atributos
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 7.3_

  - [x] 2.2 Escrever property test da renumeração do upgrade
    - **Property 4: Renumeração do upgrade desloca e preserva**
    - **Validates: Requirements 2.1, 2.2, 2.3, 8.3**

  - [x] 2.3 Escrever property test do round-trip up∘down
    - **Property 5: Round-trip up∘down da migração é identidade**
    - **Validates: Requirements 2.4, 8.3**

  - [x] 2.4 Escrever property test da guarda da migração
    - **Property 6: Guarda da migração aborta sem alterar dados**
    - **Validates: Requirements 2.6**

- [x] 3. Checkpoint - garantir base de dados e migração verdes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Costura `GeradorDeDag` e implementação de curadoria
  - [x] 4.1 Definir a costura `GeradorDeDag` e o DTO `DagProduzido` no pacote `ui/backend/app/services/dag`
    - `GeradorDeDag` como `Protocol` `@runtime_checkable` com `origem() -> OrigemDag` e `gerar(caso: str) -> list[DagProduzido]`
    - `DagProduzido` (Pydantic) com `origem`, `identificador`, `rotulo`, `conteudo` (`{"structural_nodes": [...], "arestas": [{"de","para"}]}`)
    - _Requirements: 5.1, 5.3, 9.3_

  - [x] 4.2 Implementar `CuradoriaGeradorDeDag` no pacote `ui/backend/app/services/dag`
    - Ler `data/dag/{slug}_dag.json` via `CaseService.try_load_case`, derivar `structural_nodes` + arestas (reaproveitar `_derivar_arestas`) e devolver um `DagProduzido` com `origem=CURADORIA_HUMANA`; sem acesso à rede e sem IA
    - _Requirements: 5.2, 5.4, 5.5_

  - [x] 4.3 Escrever property test do gerador de curadoria
    - **Property 8: Gerador de curadoria sempre produz curadoria_humana sem rede/IA**
    - **Validates: Requirements 5.2, 5.4, 5.5**

  - [x] 4.4 Escrever teste de exemplo de conformidade com o Protocol
    - `CuradoriaGeradorDeDag` satisfaz `GeradorDeDag`; um stub `gerado_ia` também satisfaz, provando a extensibilidade sem redesenho
    - _Requirements: 5.1, 5.3, 9.2, 9.3_

- [x] 5. `DagService` refatorado sobre o DAG persistido
  - [x] 5.1 Refatorar `ui/backend/app/services/dag/dag_service.py` para injetar `GeradorDeDag` e persistir/listar DAGs
    - Construtor recebe `session`, `case_service`, `gerador` (default `CuradoriaGeradorDeDag`); implementar `sincronizar_dags_curados(caso)` (backfill idempotente: 1 Dag `curadoria_humana` por Caso), `listar_dags(caso)` (id, origem, identificador, criado_em) e `obter_dag_persistido(dag_id)` (grafo + Mermaid, reaproveitando `montar_mermaid`/`_derivar_arestas` sobre o `conteudo` persistido)
    - Isolar Caso curado ausente/malformado com mensagem descritiva, sem persistir Dag inválido
    - _Requirements: 4.5, 4.8, 4.4_

  - [x] 5.2 Implementar `escolher_dag(caso, dag_id)` no `DagService`
    - Gravar `dag_escolhido_id` no `StageRun` de `estagio=1` e marcá-lo `concluido` (progresso 100), liberando o Estágio 2; 404 para `dag_id` inexistente/alheio ao Caso; 409 quando não há DAG persistido; padrão transacional com rollback preservando o estado anterior
    - _Requirements: 4.7, 6.2, 6.3_

  - [x] 5.3 Escrever property test da entidade DAG multi-origem
    - **Property 7: Entidade DAG — 1 Caso → N, origem no domínio, round-trip do conteúdo**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.8, 8.4**

  - [x] 5.4 Escrever property test do backfill idempotente
    - **Property 11: Backfill de DAGs curados é idempotente**
    - **Validates: Requirements 4.5**

  - [x] 5.5 Escrever property tests da escolha de DAG e gate do Estágio 1
    - **Property 9: Escolher um DAG conclui o Estágio 1 e libera o Estágio 2**
    - **Property 10: Sem DAG, o avanço além do Estágio 1 permanece bloqueado**
    - **Validates: Requirements 4.7, 6.2, 6.3**

- [x] 6. Checkpoint - garantir serviços de DAG verdes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. `StageService` e serviços dependentes renumerados para 1..4
  - [x] 7.1 Generalizar as constantes de Estágio em `ui/backend/app/services/stage/stage_service.py`
    - `LAST_STAGE = 4`, `VALID_STAGES = (1, 2, 3, 4)`; atualizar a mensagem de erro de `_validate_stage` para citar `(1, 2, 3, 4)`; nomear os papéis (1 Geração do DAG, 2 Modelagem, 3 Tradução, 4 Emulação) sem reescrever o algoritmo de gate/progresso
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6_

  - [x] 7.2 Atualizar `SessionStateService` para derivar Estágios concluídos em 1..4
    - `ui/backend/app/services/session/session_state_service.py`: ajustar docstring de `persist_stage_completion` para aceitar 1..4; confirmar que `get_completed_stages` deriva de `StageRun` sem fixar número de Estágios
    - _Requirements: 1.8_

  - [x] 7.3 Escrever property test do bloqueio sequencial em 1..4
    - **Property 1: Bloqueio sequencial em todo par adjacente de 1..4**
    - **Validates: Requirements 1.4, 1.5, 8.2**

  - [x] 7.4 Escrever property test do progresso agregado sobre 4 Estágios
    - **Property 2: Caso totalmente concluído sse os quatro Estágios concluido**
    - **Validates: Requirements 1.6, 8.2**

  - [x] 7.5 Escrever property test da derivação de estado por StageRun em 1..4
    - **Property 3: Derivação de estado por StageRun em 1..4**
    - **Validates: Requirements 1.3, 1.7, 1.8**

  - [x] 7.6 Escrever testes de exemplo de constantes e nomes de Estágio
    - `VALID_STAGES == (1,2,3,4)` e os nomes dos 4 Estágios; persistência do `StageRun` do Estágio 1
    - _Requirements: 1.1, 1.2_

- [x] 8. Endpoints REST renumerados e de DAG em `casos.py`
  - [x] 8.1 Renumerar os endpoints de Estágio para 1..4 em `ui/backend/app/api/casos.py`
    - Mapear `estagio/2` para Modelagem (antigo 1), `estagio/3` para Tradução (antigo 2), `estagio/4` para Emulação (antigo 3), incluindo `.../2/executar` e `.../3/concluir`; `GET /api/casos` e `GET /api/casos/{caso}` passam a retornar 4 Estágios por Caso
    - _Requirements: 1.1, 1.3, 1.7, 7.5_

  - [x] 8.2 Adicionar os endpoints de DAG em `ui/backend/app/api/casos.py`
    - `GET /api/casos/{caso}/dags` (lista com origem/identificador/criado_em), `GET /api/casos/{caso}/dags/{dag_id}` (grafo + Mermaid, 404 se alheio), `POST /api/casos/{caso}/estagio/1/escolher-dag` (200 com estados por Estágio, 404 dag alheio, 409 sem DAG); adaptar o legado `GET /api/casos/{caso}/dag` para resolver o DAG escolhido/único via `obter_dag_persistido`
    - Acionar `sincronizar_dags_curados` de forma preguiçosa ao listar os DAGs do Caso
    - _Requirements: 4.4, 4.8, 6.2, 6.3_

  - [x] 8.3 Habilitar o canal WebSocket de Estágios em 1..4
    - Garantir que `run_stage_progress_channel`/`ws/channels.py` aceite `estagio` 1..4 via `VALID_STAGES`
    - _Requirements: 1.7_

  - [x] 8.4 Escrever testes de integração dos contratos REST
    - Endpoints de Estágio respondem em 1..4; `escolher-dag` retorna 404 para `dag_id` alheio e 409 sem DAG
    - _Requirements: 4.8, 6.2, 6.3_

- [x] 9. Checkpoint - garantir backend verde ponta a ponta
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Frontend: tipos, wrappers de API e fluxo renumerado
  - [x] 10.1 Atualizar tipos e wrappers em `ui/frontend/src/lib/casos.ts`, `estagios.ts` e `dag.ts`
    - `NumeroEstagio = 1 | 2 | 3 | 4`; `PIPELINE_STAGES = [1,2,3,4]`; `OrigemDag = "curadoria_humana" | "gerado_ia" | (string & {})`; tipos `DagResumo`/`DagDetalhe`; wrappers `getDags`, `getDagPorId`, `escolherDag`; `connectEstagios` aceita 1..4
    - _Requirements: 1.7, 3.4, 4.8, 6.3_

  - [x] 10.2 Remover a view `"dag"` do fluxo em `ui/frontend/src/lib/flow.ts`
    - `View = "landing" | "bases" | "dashboard" | "selecao" | "wizard"`; remover eventos `DAG_PRONTO`/`AVANCAR` ligados à view `"dag"`; transição `selecao → wizard`
    - _Requirements: 3.1, 3.2_

  - [x] 10.3 Atualizar o property test do reducer de fluxo
    - **Property 12: Reducer de fluxo preserva a seleção e é determinístico sobre as views renumeradas**
    - **Validates: Requirements 3.1, 3.2**

- [x] 11. Frontend: Estágio 1 (DAG) dentro do wizard de 4 Estágios
  - [x] 11.1 Transformar `DagStageView.tsx` no corpo do Estágio 1
    - `ui/frontend/src/components/DagStageView.tsx`: listar DAGs via `getDags`, permitir seleção quando há ≥1 DAG, renderizar o grafo do selecionado com `MermaidDiagram`, botão "Gerar com IA" visível e desabilitado ("em breve") como no-op, bloquear avanço sem DAG escolhido e chamar `escolherDag` ao escolher
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 11.2 Expandir `ExecutionWizard.tsx` para 4 Estágios e costurar os componentes
    - `ui/frontend/src/components/ExecutionWizard.tsx`: trilha de badges 1..4, `stepIndicator` com `total: 4`, Estágio 1 renderiza `DagStageView`, Estágios 2/3/4 renderizam `Stage1View`/`Stage2View`/`Stage3View` (apenas rótulos mudam); preservar `EmulationConfirmModal` no Estágio 4; derivar "pode avançar do Estágio 1" do backend (`dag_escolhido_id`)
    - _Requirements: 3.1, 3.2, 7.2, 7.5_

  - [x] 11.3 Unificar as chaves i18n para "Estágio" em `ui/frontend/src/i18n`
    - `stages.1.name = "Geração do DAG"`, `stages.2.name = "Modelagem Estrutural"`, `stages.3.name = "Tradução Curada"`, `stages.4.name = "Emulação"`; `dag.gerarComIa`, `dag.emBreve`, `dag.selecionar`, `dag.semDag` em pt-BR e en
    - _Requirements: 3.1, 3.3_

  - [x] 11.4 Escrever testes de UI (Vitest) do Estágio 1 e i18n
    - Sem DAG mostra "Gerar com IA" desabilitado ("em breve") e clique é no-op; com ≥1 DAG a lista permite seleção; escolher chama `escolherDag` e libera o avanço; chaves `stages.1..4.name` existem em pt-BR/en e usam "Estágio"
    - _Requirements: 3.1, 3.3, 6.1, 6.3, 6.4, 6.5_

  - [x] 11.5 Escrever teste e2e (Playwright) do fluxo de 4 Estágios
    - Caso curado: Estágio 1 (escolher DAG) → 2 (modelagem) → 3 (tradução) → 4 (emulação com `EmulationConfirmModal` antes da execução); confirmar DAG como Estágio 1 do wizard e preservação das telas de tradução/emulação e do modal
    - _Requirements: 7.2, 7.5, 9.1, 3.2_

- [x] 12. Final checkpoint - garantir todas as suítes verdes
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tarefas marcadas com `*` são opcionais (testes) e podem ser puladas para um MVP mais rápido; as tarefas de implementação principal nunca são opcionais.
- Cada tarefa referencia cláusulas específicas dos requisitos para rastreabilidade.
- Os checkpoints asseguram validação incremental mantendo as suítes existentes verdes (Req. 8.1).
- Testes de propriedade (Hypothesis/fast-check, ≥100 iterações) validam as propriedades de correção; testes de exemplo/integração cobrem UI, contratos REST (404/409), infraestrutura e o fluxo e2e.
- A migração e os serviços operam exclusivamente contra PostgreSQL; os testes de migração nunca recorrem a SQLite (Req. 2.5, 7.3).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "4.1"] },
    { "id": 2, "tasks": ["1.3", "4.2"] },
    { "id": 3, "tasks": ["1.4", "2.1", "4.3", "4.4"] },
    { "id": 4, "tasks": ["2.2", "2.3", "2.4", "5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "7.2"] },
    { "id": 6, "tasks": ["5.5", "7.1"] },
    { "id": 7, "tasks": ["7.3", "7.4", "7.5", "7.6", "8.1", "8.3"] },
    { "id": 8, "tasks": ["8.2"] },
    { "id": 9, "tasks": ["8.4", "10.1"] },
    { "id": 10, "tasks": ["10.2", "11.1", "11.3"] },
    { "id": 11, "tasks": ["10.3", "11.2"] },
    { "id": 12, "tasks": ["11.4", "11.5"] }
  ]
}
```
