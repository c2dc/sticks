# Implementation Plan

## Overview

Este plano converte o design aprovado em passos de implementação incrementais e orientados a testes. O princípio é **estender, não reescrever**: o `ExecutionWizard`, `Stage2View`, `Stage3View`, `EmulationConfirmModal` e `MermaidDiagram` são reaproveitados sem alteração de comportamento; a contenção (`OperationRunner`, pré-voo de 4 portões, redes `internal: true`, modal de confirmação) **não é tocada**. Nenhuma tabela nova é criada. Todos os endpoints novos de inspeção são `GET` sem efeito colateral; a única ação de saída de rede é `POST /api/stix/bases/{versao}/download`.

A ordem é: (A) backend `StixService` + endpoints `/api/stix/*`; (B) backend `DagService` + `GET /api/casos/{caso}/dag`; (C) clientes HTTP e reducer no frontend (`lib/stix.ts`, `lib/dag.ts`, `lib/flow.ts`); (D) componentes de tela; (E) integração no `App.tsx` via `useAppFlow`; (F) strings i18n pt-BR/en; (G) verificação full-stack mantendo as suítes existentes verdes.

Convenções de teste:
- Linguagens: **Python** (pytest + Hypothesis) no backend, **TypeScript** (Vitest + fast-check) no frontend — conforme o design (sem pseudocódigo).
- Cada uma das 11 Correctness Properties vira **um único** teste de propriedade, com mínimo de **100 iterações**, anotado no formato `Feature: fluxo-stix-dashboard, Property N: <texto>`.
- Testes de download usam **HTTP mockado** — nunca rede real.
- Sub-tarefas marcadas com `*` são de teste e **opcionais** (podem ser puladas em um MVP). Tarefas de implementação central nunca são opcionais.

---

## Tasks

- [x] 1. Fundação backend STIX: modelos Pydantic, constante de versões e resolução de caminho
  - [x] 1.1 Criar `app/services/stix/__init__.py` e `app/services/stix/stix_service.py` com o esqueleto do `StixService`
    - Definir modelos Pydantic (nomes de campo em pt-BR idênticos ao contrato do design): `BaseStixInfo` (`versao`, `nome`, `baixada`, `selecionavel`, `arquivo`), `ContagemTipo` (`tipo`, `stix_type`, `quantidade`, `descricao`), `DashboardStix` (`versao`, `nome`, `contagens`), `CampanhaStix` (`stix_id`, `nome`, `descricao`, `curada`, `slug_curado`), `AtorStix` (`stix_id`, `nome`, `campanhas`)
    - Resolver o diretório de dados via `sticks_config._repo_root() / "sticks" / "data" / "stix"` (mesma estratégia do `CaseService`); aceitar `data_dir: Path | None` e `http_client=None` injetáveis para teste
    - Ler `STIX_URL`/`STIX_DIR` via `load_sticks_config()`; definir a constante `VERSOES_CONHECIDAS` (lista versionada no backend) e o mapa estático `DESCRICOES_TIPO` (pt-BR) e `SLUG_PARA_STIX` (8 slugs de `CaseService.CURATED_CASES`)
    - Arquivo-alvo em disco: `sticks/data/stix/enterprise-attack-{versao}.json`
    - _Requisitos: 2.1, 2.5, 3.3, 4.5_

  - [x] 1.2 Implementar `StixService.listar_bases()` (ordenação + flag baixada/selecionável)
    - Enumerar `VERSOES_CONHECIDAS` ordenadas por versão semântica de forma monotonicamente decrescente (mais nova primeiro); nenhuma saída de rede
    - Marcar `baixada = (arquivo presente em sticks/data/stix)` ao vivo e `selecionavel == baixada`; preencher `arquivo` apenas quando baixada
    - _Requisitos: 2.1, 2.2, 2.3_

  - [x]* 1.3 Escrever teste de propriedade para ordenação das bases
    - **Feature: fluxo-stix-dashboard, Property 1: Bases sempre ordenadas da mais nova para a mais antiga**
    - Hypothesis: gerar conjuntos de versões conhecidas; verificar ordenação decrescente por versão semântica; mínimo 100 iterações
    - **Valida: Requisitos 2.1**

  - [x]* 1.4 Escrever teste de propriedade para flag baixada/selecionável
    - **Feature: fluxo-stix-dashboard, Property 2: "Baixada" e "selecionável" refletem exatamente a presença em disco**
    - Hypothesis: gerar versões + subconjunto de arquivos presentes via `tmp_path`; verificar `baixada == arquivo presente` e `selecionavel == baixada`; mínimo 100 iterações
    - **Valida: Requisitos 2.2, 2.3**

- [x] 2. Parsing do bundle STIX e dashboard de inteligência
  - [x] 2.1 Implementar `StixService.ler_bundle(versao)` com erro tipado
    - Ler e fazer parse do bundle da base baixada; distinguir arquivo ausente de JSON malformado/estrutura inválida; levantar erro tipado (reutilizar o padrão de `CaseFileError`/`CaseLoadErrorType` do `CaseService` como referência)
    - Nunca retornar objeto parcial em caso de falha
    - _Requisitos: 3.4, 3.5_

  - [x] 2.2 Implementar `StixService.dashboard(versao)` (contagens por tipo + descrições)
    - Iterar `bundle["objects"]`, agrupar por `type`; produzir contagens para ao menos: `campaign`→campanhas, `attack-pattern`→técnicas, `x-mitre-tactic`→táticas, `intrusion-set`→intrusion sets, `malware`→malware, `relationship`→relacionamentos
    - Preencher `descricao` de cada contagem a partir de `DESCRICOES_TIPO` (pt-BR, não vazia); incluir `versao` e `nome`
    - _Requisitos: 3.1, 3.2, 3.3, 3.4_

  - [x]* 2.3 Escrever teste de propriedade para contagens do dashboard
    - **Feature: fluxo-stix-dashboard, Property 3: Contagem por tipo igual ao número de objetos daquele tipo no bundle**
    - Hypothesis: gerar bundles sintéticos com N objetos aleatórios por `type`; verificar `quantidade[tipo] == N` para cada tipo exigido; mínimo 100 iterações
    - **Valida: Requisitos 3.2, 3.4**

  - [x]* 2.4 Escrever teste de propriedade para descrições não vazias
    - **Feature: fluxo-stix-dashboard, Property 4: Todo tipo contabilizado tem descrição não vazia**
    - Hypothesis: para qualquer dashboard produzido, toda contagem tem `descricao` não vazia; mínimo 100 iterações
    - **Valida: Requisitos 3.3**

  - [x]* 2.5 Escrever teste de exemplo/edge para bundle inválido
    - JSON malformado e estrutura inválida → erro tipado; garantir que nenhum conjunto de contagens "completo" é retornado como se fosse válido
    - _Requisitos: 3.5_

- [x] 3. Campanhas, atores e marcação de caso curado no `StixService`
  - [x] 3.1 Implementar `StixService.listar_campanhas(versao)` com flag curada
    - Selecionar objetos `type == "campaign"`; para cada um, resolver `curada`/`slug_curado` via `SLUG_PARA_STIX` (casar por `external_references` tipo `C0010`, senão por nome canônico normalizado; fallback silencioso quando nenhuma casa — nenhuma campanha inventada)
    - _Requisitos: 4.2, 4.5_

  - [x] 3.2 Implementar `StixService.listar_atores(versao)` (agrupamento ator→campanhas)
    - Selecionar objetos `type == "intrusion-set"`; indexar `relationship` com `relationship_type == "attributed-to"` (campaign `source_ref` → intrusion-set `target_ref`); agregar as campanhas por ator; campanhas sem atribuição ficam sob "sem ator associado"
    - Propagar `curada`/`slug_curado` nas campanhas aninhadas
    - _Requisitos: 4.3_

  - [x]* 3.3 Escrever teste de propriedade para a lista de campanhas
    - **Feature: fluxo-stix-dashboard, Property 5: Lista de campanhas igual aos objetos campaign do bundle**
    - Hypothesis: gerar bundles aleatórios; verificar que a lista retornada iguala exatamente o conjunto de objetos `type == "campaign"` (sem perder nem duplicar); mínimo 100 iterações
    - **Valida: Requisitos 4.2**

  - [x]* 3.4 Escrever teste de propriedade para o agrupamento ator→campanhas
    - **Feature: fluxo-stix-dashboard, Property 6: Agrupamento Ator → Campanhas reflete os relationships**
    - Hypothesis: gerar intrusion-sets, campanhas e relationships `attributed-to` aleatórios; verificar que cada campanha aparece sob o ator do seu relacionamento, e apenas sob ele, sem perder nem duplicar; mínimo 100 iterações
    - **Valida: Requisitos 4.3**

  - [x]* 3.5 Escrever teste de propriedade para a flag "curada"
    - **Feature: fluxo-stix-dashboard, Property 7: Flag "curada" corresponde exatamente ao mapeamento de casos curados**
    - Hypothesis: gerar listas de campanhas (curadas e não); verificar `curada == true` com `slug_curado` preenchido sse e somente se casa com o mapeamento dos 8 casos, senão `curada == false` e `slug_curado == null`; mínimo 100 iterações
    - **Valida: Requisitos 4.5**

- [x] 4. Download seguro de base STIX (única saída de rede)
  - [x] 4.1 Implementar `StixService.baixar_base(versao)` com escrita atômica
    - Validar `versao` contra `VERSOES_CONHECIDAS` (sem interpolação arbitrária de caminho/URL — evita path/URL injection); montar a URL versionada do `mitre-attack/attack-stix-data` a partir de `STIX_URL`
    - Fazer o `GET` externo via `http_client` injetável; escrever em arquivo temporário e só então `rename` atômico para `sticks/data/stix/enterprise-attack-{versao}.json`; em falha, remover o temporário e não deixar arquivo parcial
    - Retornar `BaseStixInfo` com `baixada=true` em sucesso; em falha, sinalizar erro descritivo e manter a base não baixada
    - _Requisitos: 2.5, 2.6, 2.7_

  - [x]* 4.2 Escrever testes de exemplo/edge do download (HTTP mockado — sem rede real)
    - Sucesso: `http_client` mockado retorna o bundle; verificar a URL versionada correta, escrita atômica e `baixada=true`
    - Falha (timeout/404/erro de escrita): erro descritivo, nenhum arquivo parcial em disco, base permanece não baixada
    - Edge: `versao` fora de `VERSOES_CONHECIDAS` é rejeitada sem tocar o disco nem a rede
    - _Requisitos: 2.5, 2.7_

- [x] 5. Endpoints REST `app/api/stix.py` e registro no router
  - [x] 5.1 Criar `app/api/stix.py` com os GETs de inspeção (side-effect free) e o POST de download
    - `GET /api/stix/bases` → `listar_bases`; `GET /api/stix/bases/{versao}/dashboard` → `dashboard`; `GET /api/stix/bases/{versao}/campanhas` → `listar_campanhas`; `GET /api/stix/bases/{versao}/atores` → `listar_atores`; `POST /api/stix/bases/{versao}/download` → `baixar_base`
    - Handlers finos delegando ao `StixService`; modelos de resposta Pydantic do design; erro no padrão `{"detail": {"motivo","mensagem"}}` (ex.: `bundle_invalido` → 422, `falha_download` → 502/422) com mensagens em pt-BR
    - _Requisitos: 2.1, 2.5, 2.7, 3.1, 3.2, 3.5, 4.2, 4.3, 4.5_

  - [x] 5.2 Registrar o router STIX em `app/api/router.py`
    - Incluir `stix.router` via `api_router.include_router(stix.router)` ao lado dos demais (padrão de `casos`/`topologia`)
    - _Requisitos: 2.1, 3.1, 4.2_

  - [x]* 5.3 Escrever testes de endpoint para `/api/stix/*`
    - Em `tests/test_api_endpoints.py` (ou novo `tests/test_stix_endpoints.py`): bases ordenadas + flag baixada; dashboard com contagens e descrições; dashboard de bundle inválido → 422 sem contagens parciais; download mockado sucesso/falha; campanhas/atores
    - _Requisitos: 2.1, 2.2, 2.7, 3.2, 3.5, 4.2, 4.3_

- [x] 6. Checkpoint — backend STIX
  - Garantir que todos os testes passam; perguntar ao usuário se surgirem dúvidas.

- [x] 7. `DagService`: carregamento do DAG, Mermaid acíclico e distinção curado/stub
  - [x] 7.1 Criar `app/services/dag/__init__.py` e `app/services/dag/dag_service.py` com os modelos e o esqueleto
    - Definir `DagService(case_service: CaseService | None = None)`; modelos `DagView` (`caso_id`, `nome`, `curado`, `pode_avancar`, `nos`, `arestas`, `mermaid`) e os campos de stub (`indisponivel`, `mensagem`) e de erro (`erro`)
    - Reaproveitar `CaseService` para resolução de caminho (`dag_path`) e parsing tipado (`DagData`/`DagNode`); carregar `structural_nodes` e as `edges` (pares `{from,to}` por `node_id`) conforme o contrato do design
    - _Requisitos: 5.1, 5.2_

  - [x] 7.2 Implementar `DagService.montar_mermaid(dag)` (função pura, acíclica por construção)
    - Seguir o padrão de `app/api/topologia.py::_mermaid`: um nó por `structural_node` rotulado `n_{node_id}["{technique_id}<br/>{technique_name}<br/>{tactic}"]`; uma aresta por entrada de `edges` → `n_{from} --> n_{to}`; `flowchart TD`
    - Não criar arestas adicionais (preserva a aciclicidade garantida por `metadata.validation.is_dag`); nenhum script embutido (renderização `strict` no cliente)
    - _Requisitos: 6.1, 6.2_

  - [x] 7.3 Implementar `DagService.obter_dag(caso)` (curado / stub / erro)
    - Caso curado (slug em `CURATED_CASES`) com `{slug}_dag.json` válido: `curado=true`, `pode_avancar=true`, `nos`/`arestas` preenchidos e `mermaid` montado
    - Caso não curado: estado "passa-direto" `curado=false`, `pode_avancar=false`, `indisponivel=true`, `mensagem` — **sem** invocar qualquer serviço de IA (nenhum cliente de IA no caminho)
    - DAG ausente/malformado de caso curado: erro tipado (`curado=true`, `pode_avancar=false`) distinguindo ausente de malformado
    - _Requisitos: 5.2, 5.3, 5.4, 5.5, 6.1, 6.3, 6.4, 9.1, 9.2_

  - [x]* 7.4 Escrever teste de propriedade para caso curado (Mermaid acíclico + avanço)
    - **Feature: fluxo-stix-dashboard, Property 8: Caso curado produz Mermaid acíclico não vazio e habilita avanço**
    - Hypothesis: gerar DAGs acíclicos aleatórios; verificar `|nos| == |structural_nodes|`, `|arestas| == |edges|`, Mermaid não vazio, aciclicidade preservada (nenhuma aresta extra), `curado==true`, `pode_avancar==true`; mínimo 100 iterações. Incluir exemplo real `shadowray_dag.json`
    - **Valida: Requisitos 5.2, 6.1, 6.3**

  - [x]* 7.5 Escrever teste de propriedade para caso não curado ("passa-direto", sem IA)
    - **Feature: fluxo-stix-dashboard, Property 9: Caso não curado é sempre "passa-direto", sem IA e sem avanço**
    - Hypothesis: para slug fora dos 8 curados, `obter_dag` retorna `curado==false`, `pode_avancar==false`, estado de indisponibilidade, sem jamais chamar serviço de IA (garantido pela ausência de dependência; assegurar o contrato); mínimo 100 iterações
    - **Valida: Requisitos 5.4, 5.5, 9.1, 9.2**

  - [x]* 7.6 Escrever teste de exemplo/edge para DAG inválido de caso curado
    - Arquivo ausente e JSON malformado de um caso curado → erro tipado, `pode_avancar==false`
    - _Requisitos: 5.3, 6.4_

- [x] 8. Endpoint `GET /api/casos/{caso}/dag`
  - [x] 8.1 Adicionar a rota em `app/api/casos.py` delegando ao `DagService`
    - Handler fino: slug desconhecido → 404 (padrão de `casos.py`/`topologia.py`); caso curado válido → payload `DagView`; não curado → "passa-direto"; curado inválido → `{"detail":{"motivo":"dag_invalido","mensagem":...}}` (422) ou payload `curado:true, pode_avancar:false, erro`
    - GET sem efeito colateral (espelha o invariante read-only de `read_stage1`/`topologia.py`)
    - _Requisitos: 5.2, 5.3, 5.4, 6.1, 6.3, 6.4, 9.1, 9.2_

  - [x]* 8.2 Escrever testes de endpoint para `GET /api/casos/{caso}/dag`
    - Curado (`shadowray`) → `curado:true, pode_avancar:true, mermaid` não vazio; não curado → stub; slug desconhecido → 404
    - _Requisitos: 5.2, 5.4, 6.1, 9.1_

- [x] 9. Verificação de contenção intacta (regressão)
  - [x] 9.1 Confirmar que as novas rotas não tocam o caminho de execução/contenção
    - Rodar as suítes `tests/test_containment_security.py`, `tests/test_isolation_property.py`, `tests/test_operation_runner_property.py`, `tests/test_operation_runner_audit_abort_property.py` e confirmar que permanecem verdes
    - Revisar que `app/api/stix.py` e a rota `/dag` não importam nem invocam `OperationRunner`, Docker ou reconfiguram redes; nenhuma redução do nível de isolamento
    - _Requisitos: 8.1, 8.2, 8.5, 10.1_

- [x] 10. Checkpoint — backend DAG + contenção
  - Garantir que todos os testes passam; perguntar ao usuário se surgirem dúvidas.

- [x] 11. Clientes HTTP e reducer do fluxo (frontend)
  - [x] 11.1 Criar `src/lib/stix.ts` (tipos + wrappers de `/api/stix/*`)
    - Tipos TS espelhando os contratos pt-BR (`BaseStixInfo`, `DashboardStix`, `ContagemTipo`, `CampanhaStix`, `AtorStix`); funções `getBases`, `baixarBase`, `getDashboard`, `getCampanhas`, `getAtores` reutilizando o `api` de `lib/api.ts` (estilo de `lib/casos.ts`, tratamento de `ApiError`)
    - _Requisitos: 2.1, 2.5, 3.1, 4.2, 4.3_

  - [x] 11.2 Criar `src/lib/dag.ts` (tipos + wrapper de `GET /api/casos/{caso}/dag`)
    - Tipos TS (`DagView`, nós/arestas, campos de stub/erro) e função `getDag(caso)` reutilizando `lib/api.ts`
    - _Requisitos: 5.2, 5.4, 6.1_

  - [x] 11.3 Criar `src/lib/flow.ts` com `FlowState`, `FlowEvent` e `flowReducer` puro
    - Implementar o reducer determinístico conforme o diagrama de estados: `INICIAR` (landing→bases), `SELECIONAR_BASE`, `IR_PARA_SELECAO`, `SELECIONAR_CAMPANHA`, `DAG_PRONTO({podeAvancar})`, `AVANCAR` (dag→wizard somente se `dagPodeAvancar`), `VOLTAR` (wizard→dag preservando a mesma campanha)
    - Exportar também o hook `useAppFlow` (encapsula `useReducer`)
    - _Requisitos: 1.3, 5.5, 6.3, 7.4_

  - [x]* 11.4 Escrever teste de propriedade da transição inicial da Landing
    - **Feature: fluxo-stix-dashboard, Property 11: Transição inicial da Landing**
    - fast-check: para qualquer estado na view `landing`, aplicar `INICIAR` resulta na view `bases`; mínimo 100 iterações
    - **Valida: Requisitos 1.3**

  - [x]* 11.5 Escrever teste de propriedade de ida e volta DAG ↔ wizard
    - **Feature: fluxo-stix-dashboard, Property 10: Ida e volta de navegação DAG ↔ tradução preserva a campanha**
    - fast-check: para qualquer campanha curada na view `dag` com `dagPodeAvancar == true`, `AVANCAR` seguido de `VOLTAR` retorna à view `dag` com a mesma campanha; mínimo 100 iterações
    - **Valida: Requisitos 7.4**

- [x] 12. Tela Landing e hook de redução de movimento
  - [x] 12.1 Criar `src/components/Landing.tsx` com marca STICKS, CTA e animações limitadas
    - Reaproveitar a wordmark/logo (`public/sticks-logomarca-*.png`, `public/sticks-logo-mais-fina.png`) e o padrão de logo adaptável ao tema do `NavBar`; botão de início dispara `INICIAR`
    - Constante única `LANDING_TRANSITION_MS = 600` limitando as classes de animação Tailwind (duração ≤ 600 ms)
    - Hook `usePrefersReducedMotion()` via `window.matchMedia("(prefers-reduced-motion: reduce)")` que desliga as animações quando o usuário prefere movimento reduzido
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x]* 12.2 Escrever testes de exemplo da Landing
    - Renderiza marca + CTA; clique dispara a transição; duração de transição ≤ 600 ms; com `matchMedia(prefers-reduced-motion: reduce)` mockado, renderiza sem animações
    - _Requisitos: 1.1, 1.2, 1.4, 1.5_

- [x] 13. Listagem de Bases STIX
  - [x] 13.1 Criar `src/components/StixBasesList.tsx`
    - Consumir `getBases`; listar da mais nova para a mais antiga; badge "já baixada" e card selecionável apenas em baixadas (dispara `SELECIONAR_BASE`); botão "Baixar" apenas em não baixadas (dispara `baixarBase`, estado de carregamento, relê a lista ao concluir)
    - Em falha de download, exibir mensagem de erro descritiva e manter a base não baixada
    - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x]* 13.2 Escrever testes de exemplo da listagem
    - Ordenação decrescente; badge/selecionável só em baixadas; botão "Baixar" só em não baixadas; pós-download relê e marca baixada; falha mostra erro e mantém não baixada
    - _Requisitos: 2.1, 2.2, 2.3, 2.6, 2.7_

- [x] 14. Dashboard de Inteligência
  - [x] 14.1 Criar `src/components/StixIntelligenceDashboard.tsx`
    - Consumir `getDashboard(versao)`; exibir identificação da base com versão; cards de contagem por tipo (campanhas, técnicas, táticas, intrusion sets, malware, relacionamentos) com a descrição de cada tipo; botão "Selecionar campanha/ator" dispara `IR_PARA_SELECAO`
    - Em erro de bundle, exibir erro descritivo e **não** renderizar contagens parciais como completas
    - _Requisitos: 3.1, 3.2, 3.3, 3.5, 3.6_

  - [x]* 14.2 Escrever testes de exemplo/edge do dashboard
    - Renderiza versão + cards com descrições; em erro de bundle, mostra erro e não renderiza contagens parciais
    - _Requisitos: 3.1, 3.2, 3.3, 3.5, 10.3_

- [x] 15. Seleção de Campanha/Ator
  - [x] 15.1 Criar `src/components/CampaignActorSelector.tsx`
    - Toggle entre visão por Campanha (consome `getCampanhas`) e visão por Ator (consome `getAtores`, campanhas aninhadas por ator); badge "já curada" nas campanhas com `slug_curado`; selecionar campanha dispara `SELECIONAR_CAMPANHA` (com `stix_id`, `nome`, `slug_curado`) e transiciona para `dag`
    - _Requisitos: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x]* 15.2 Escrever testes de exemplo da seleção
    - Toggle alterna visões; campanhas curadas recebem badge; ator lista campanhas associadas; selecionar campanha transiciona para `dag`
    - _Requisitos: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 16. Fase de Geração/Visualização do DAG
  - [x] 16.1 Criar `src/components/DagStageView.tsx` reusando `MermaidDiagram`
    - Caso curado (`slug_curado` presente): consome `getDag`, entrega a `mermaid` ao `MermaidDiagram` (que já usa `securityLevel: "strict"`); ao renderizar com sucesso dispara `DAG_PRONTO({ podeAvancar: true })` e habilita "Avançar"
    - DAG inválido ou falha de renderização: exibe erro descritivo e dispara `DAG_PRONTO({ podeAvancar: false })`, mantendo "Avançar" desabilitado
    - Campanha não curada (`slug_curado` ausente): exibe o "passa-direto" **sem** chamar o backend de DAG nem qualquer IA e dispara `DAG_PRONTO({ podeAvancar: false })`
    - _Requisitos: 5.2, 5.3, 5.4, 5.5, 6.1, 6.2, 6.3, 6.4, 9.1, 9.2_

  - [x]* 16.2 Escrever testes de exemplo da fase de DAG
    - Curado renderiza via `MermaidDiagram` e habilita "Avançar"; não curado mostra "passa-direto" e mantém "Avançar" desabilitado; erro de DAG desabilita "Avançar"
    - _Requisitos: 5.2, 5.4, 6.1, 6.3, 6.4, 10.3_

- [x] 17. Integração no `App.tsx` com a máquina de estados `useAppFlow`
  - [x] 17.1 Adicionar o seam opcional `onVoltarAntesDoEstagio1` ao `ExecutionWizard`
    - Novo prop opcional que, no Estágio 1, ao acionar "Voltar", dispara o callback (para o fluxo retornar à `DagStageView`) sem alterar a navegação interna entre estágios 1↔2↔3
    - _Requisitos: 7.2, 7.4_

  - [x] 17.2 Encapsular `useAppFlow` no `App.tsx` e rotear a view para os novos componentes
    - Renderizar, dentro do `AppLayout`, o componente conforme `view`: `landing` → `Landing`; `bases` → `StixBasesList`; `dashboard` → `StixIntelligenceDashboard`; `selecao` → `CampaignActorSelector`; `dag` → `DagStageView`; `wizard` → `ExecutionWizard` existente com `casoId = slug_curado`
    - Manter `Stage2View`/`Stage3View`/`EmulationConfirmModal` intactos; preservar o estado de tema/preferências como está; "Voltar" do Estágio 1 dispara `VOLTAR` do fluxo (via `onVoltarAntesDoEstagio1`)
    - _Requisitos: 7.1, 7.2, 7.3, 7.4, 9.3_

  - [x]* 17.3 Escrever teste de integração do fluxo
    - Avançar de `dag` curado monta o `ExecutionWizard` existente no Estágio 1; `Stage2View`/`Stage3View`/`EmulationConfirmModal` seguem reutilizados sem alteração de contrato
    - _Requisitos: 7.1, 9.3_

- [x] 18. Internacionalização (pt-BR + en)
  - [x] 18.1 Adicionar as strings do novo fluxo aos locales
    - Criar/estender os arquivos em `src/i18n/locales/pt-BR/` e `src/i18n/locales/en/` (ex.: namespace `stix.json`) cobrindo Landing, Bases, Dashboard, Seleção e fase de DAG; registrar no `src/i18n/index.ts`
    - pt-BR como padrão; manter paridade de chaves entre pt-BR e en
    - _Requisitos: 1.1, 2.4, 3.3, 4.1, 5.4_

- [x] 19. Verificação full-stack e manutenção das suítes existentes
  - [x] 19.1 Rodar e manter verde a suíte de backend
    - Executar `pytest` no `ui/backend`; garantir que as suítes existentes (incl. contenção/OperationRunner) continuam passando junto das novas
    - _Requisitos: 10.1_

  - [x] 19.2 Rodar e manter verde a suíte de frontend
    - Executar `vitest --run` no `ui/frontend`; garantir que as suítes existentes (wizard, tema, i18n, case selector) continuam passando junto das novas
    - _Requisitos: 10.2_

  - [x] 19.3 Confirmar cobertura dos novos pontos exigidos
    - Verificar presença dos testes de contagens do dashboard, carregamento do DAG de um caso curado e estado "passa-direto" de uma campanha não curada
    - _Requisitos: 10.3_

- [x] 20. Checkpoint final — todas as suítes verdes
  - Garantir que todos os testes passam; perguntar ao usuário se surgirem dúvidas.

## Notes

- Tarefas marcadas com `*` são de teste e opcionais (podem ser puladas em um MVP); as tarefas de implementação central nunca são opcionais.
- Cada tarefa referencia os requisitos que satisfaz; os testes de propriedade referenciam explicitamente a Correctness Property do design.
- A geração de DAG por IA para casos não curados é **fora de escopo**: o único trabalho relacionado é o "passa-direto" (sem cliente de IA).
- A contenção não é alterada: as novas rotas são de inspeção read-only; a única saída de rede é `POST /api/stix/bases/{versao}/download`, com validação de versão e escrita atômica.
- Sobre as arestas do DAG: o contrato do design usa `edges [{from,to}]` por `node_id`; quando um `{slug}_dag.json` expressa relações via `parent_nodes`/`child_nodes` nos `structural_nodes`, derivar as `edges` dessas relações sem introduzir arestas extras (preservando a aciclicidade).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "7.1"] },
    { "id": 2, "tasks": ["1.3", "1.4", "2.2", "3.1", "3.2", "4.1", "7.2"] },
    { "id": 3, "tasks": ["2.3", "2.4", "2.5", "3.3", "3.4", "3.5", "4.2", "7.3"] },
    { "id": 4, "tasks": ["5.1", "7.4", "7.5", "7.6", "8.1"] },
    { "id": 5, "tasks": ["5.2", "8.2", "9.1"] },
    { "id": 6, "tasks": ["5.3", "11.1", "11.2", "11.3"] },
    { "id": 7, "tasks": ["11.4", "11.5", "12.1", "13.1", "14.1", "15.1", "16.1"] },
    { "id": 8, "tasks": ["12.2", "13.2", "14.2", "15.2", "16.2", "17.1", "18.1"] },
    { "id": 9, "tasks": ["17.2"] },
    { "id": 10, "tasks": ["17.3", "19.1", "19.2", "19.3"] }
  ]
}
```
