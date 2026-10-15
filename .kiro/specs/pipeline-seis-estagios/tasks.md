# Implementation Plan: Pipeline de seis Estágios

## Overview

Plano de implementação da spec `pipeline-seis-estagios`: reestrutura o pipeline de **cinco** para **seis Estágios** (1 Seleção da base STIX, 2 Seleção da campanha, 3 Geração do DAG, 4 Operacionalização, 5 Criação do ambiente, 6 Execução + medição + relatório). A **alavanca principal** é ampliar o range de Estágios de 1..5 para 1..6 em `stage_service.py` (constantes + papéis + docstrings/mensagens), sem renomear as classes históricas `Stage1*`/`Stage2*`/`Stage3*` (dívida técnica fora de escopo, Req. 18.5). O trabalho vai do backend (range → Estágios 1→6 reusando `StixService`/`DagService`/`CaseService`/`containment`/`OperationRunner`, com os DELTAS tipados novos) para a persistência/migração Alembic, depois API/WS, e por fim o frontend (wizard de 6 Estágios, telas novas, i18n). Checkpoints asseguram as suítes verdes.

Títulos de tarefa em inglês, descrições em pt-BR. Backend em `ui/backend` (FastAPI + SQLAlchemy + Alembic + **PostgreSQL exclusivamente**), frontend em `ui/frontend` (React 19 + TS + Vite + i18next). Testes de propriedade usam Hypothesis (backend) e fast-check (frontend), ≥100 iterações, uma propriedade por teste, com a tag `Feature: pipeline-seis-estagios, Property {n}`. Testes de ambiente REAL/Caldera/Docker ficam marcados `lab` (deselecionados por padrão, guardados por `PIPELINE_UI_LAB`), seguindo o precedente do projeto.

## Tasks

- [x] 1. Generalizar o range de Estágios para 1..6 (alavanca principal)
  - [x] 1.1 Ampliar o intervalo e nomear os papéis em `ui/backend/app/services/stage/stage_service.py`
    - Definir `FIRST_STAGE=1`, `LAST_STAGE=6`, `VALID_STAGES=(1,2,3,4,5,6)` e as constantes de papel `ESTAGIO_SELECAO_BASE=1`, `ESTAGIO_SELECAO_CAMPANHA=2`, `ESTAGIO_GERACAO_DAG=3`, `ESTAGIO_OPERACIONALIZACAO=4`, `ESTAGIO_AMBIENTE=5`, `ESTAGIO_EXECUCAO=6`; exportar em `__all__` e manter aliases dos nomes anteriores (`ESTAGIO_MODELAGEM`/`ESTAGIO_PROVISIONAMENTO` etc.) para não quebrar imports
    - NÃO renomear as classes DTO `Stage1*`/`Stage2*`/`Stage3*` (Req. 18.5); confirmar que `is_stage_startable`/`get_stage_states`, `aggregate_progress`, `_build_stage_state_view` e `stage_progress_event` continuam range-agnósticos (derivam de `VALID_STAGES`) e passam a cobrir 1..6 sem reescrita de algoritmo
    - Atualizar a mensagem de `_validate_stage` e as docstrings de "1..5"/"1, 2 ou 3" para citar `(1, 2, 3, 4, 5, 6)`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 3.1, 3.2, 3.3, 18.5_
  - [x] 1.2 Escrever property test do bloqueio sequencial em 1..6
    - **Property 1: Bloqueio sequencial em 1..6**
    - **Validates: Requirements 1.3, 3.1, 3.2, 3.3**
  - [x] 1.3 Escrever property test do progresso agregado sobre 6 Estágios
    - **Property 2: Campanha totalmente concluída sse os seis Estágios concluido**
    - **Validates: Requirements 1.5**
  - [x] 1.4 Escrever property test da derivação de estado por StageRun em 1..6
    - **Property 3: Derivação de estado por StageRun em 1..6**
    - **Validates: Requirements 1.2, 1.4**
  - [x] 1.5 Escrever testes de exemplo das constantes e `_validate_stage`
    - Verificar `FIRST_STAGE==1`, `LAST_STAGE==6`, `VALID_STAGES==(1,2,3,4,5,6)`, os seis nomes de papel e que `_validate_stage` aceita 1..6 e rejeita 0/7
    - _Requirements: 1.1, 1.2_

- [x] 2. Backend Estágio 1: Seleção da base STIX (reuso de `StixService`)
  - [x] 2.1 Adicionar a flag de versão curada em `ui/backend/app/services/stix/stix_service.py`
    - Definir a constante `VERSAO_CURADA = "18.0"` (fonte única) e o helper `versao_eh_curada(versao) -> bool`; a versão 18 carrega os 8 casos curados e o apontamento dos artefatos curados
    - _Requirements: 5.5, 5.6, 11.5_
  - [x] 2.2 Criar o serviço fino do Estágio 1 reusando `StixService`
    - Serviço novo que orquestra: `listar_bases()` (flag `baixada`/`selecionavel` checada contra disco; nenhuma versão baixada por padrão), `baixar_base(versao)` quando ausente (único egresso de rede, valida `versao` contra `VERSOES_CONHECIDAS` antes de tocar disco/rede, escrita atômica, `StixDownloadError` sem arquivo parcial) e `ler_bundle`/dashboard (contagens por tipo + campanhas); concluir o Estágio 1 quando o bundle está em disco e o dashboard foi montado; falha de download mantém o Estágio não concluído com a causa reportada
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.7, 5.8, 5.9, 5.10, 2.2_
  - [x] 2.3 Adicionar os endpoints REST do Estágio 1 em `ui/backend/app/api/casos.py`
    - `GET /api/stix/bases` (lista versões com `baixada`/`selecionavel`), `POST /api/stix/bases/{versao}/baixar` (download), `GET /api/casos/{caso}/estagio/1` (dashboard do bundle); contratos JSON pt-BR
    - _Requirements: 5.1, 5.2, 5.7, 5.8_
  - [x] 2.4 Escrever testes de exemplo da flag curada e do download
    - `versao_eh_curada("18.0")` verdadeiro e falso para demais; estender os testes de `baixar_base` (sucesso, versão desconhecida, HTTP erro, conteúdo inválido, falha de escrita) e do dashboard (contagem por tipo + campanhas)
    - _Requirements: 5.2, 5.4, 5.5, 5.7, 5.9_

- [x] 3. Backend Estágio 2: Seleção da campanha (reuso de `StixService` + `CampanhaStixEntrada`)
  - [x] 3.1 Implementar a listagem e a seleção de campanha do Estágio 2
    - Reusar `StixService` para listar as campanhas do bundle marcando `curada`/`slug_curado` via `SLUG_PARA_STIX` (8 campanhas selecionáveis na versão 18) e `obter_campanha_por_slug(versao, slug)` + o DTO histórico `CampanhaStixEntrada`/`StageService._build_campanha_stix` (defensivo, sem rede nos caminhos curados) para extrair os atributos (identidade, aliases, técnicas) ao selecionar; concluir quando a extração tem sucesso; falha mantém o Estágio não concluído com a causa reportada
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 2.3_
  - [x] 3.2 Adicionar os endpoints REST do Estágio 2 em `ui/backend/app/api/casos.py`
    - `GET /api/casos/{caso}/estagio/2` (lista de campanhas + atributos extraídos) e `POST /api/casos/{caso}/estagio/2/selecionar` (seleciona campanha → extrai atributos)
    - _Requirements: 6.2, 6.3, 6.4_
  - [x] 3.3 Escrever testes de exemplo da seleção de campanha
    - Versão 18 lista as 8 campanhas curadas selecionáveis; selecionar extrai os atributos e conclui; slug inexistente/extração falha mantém não concluído com causa
    - _Requirements: 6.2, 6.5, 6.6, 6.7_

- [x] 4. Backend Estágio 3: Geração do DAG (reuso da costura `GeradorDeDag`/`DagService`)
  - [x] 4.1 Costurar o Estágio 3 sobre `GeradorDeDag`/`CuradoriaGeradorDeDag`/`DagService`
    - Reusar `CuradoriaGeradorDeDag.gerar(caso, modelagem)` (lê `data/dag/{slug}_dag.json` via `CaseService`, ignora a modelagem, devolve 1 `DagProduzido` `origem=curadoria_humana`, sem rede e sem IA) e `DagService` (persiste com backfill idempotente, lista, monta `DagView`/`DagDetalhe`/Mermaid); a Entrada são os atributos do Estágio 2, a Saída é o DAG pronto com `origem` explícita; concluir quando o DAG é produzido; artefato ausente mantém o Estágio não concluído reportando a ausência
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 11.1, 11.3, 11.4, 11.6, 16.1_
  - [x] 4.2 Adicionar os endpoints REST do Estágio 3 em `ui/backend/app/api/casos.py`
    - `GET /api/casos/{caso}/estagio/3` (DagView/Mermaid), e manter/reusar `GET /api/casos/{caso}/dags` e `GET /api/casos/{caso}/dags/{dag_id}` (grafo + Mermaid) para os DAGs persistidos
    - _Requirements: 7.2, 7.3_
  - [x] 4.3 Escrever property test da idempotência da leitura curada do DAG
    - **Property 5: Idempotência da leitura dos artefatos curados** (foco no DAG do Estágio 3; backfill não duplica)
    - **Validates: Requirements 7.4, 11.1, 11.3, 11.4**
  - [x] 4.4 Escrever property test da origem explícita e extensível (Estágio 3)
    - **Property 13: Origem explícita e extensível nos Estágios 3, 4 e 5** (parte Estágio 3; `CuradoriaGeradorDeDag` carimba `curadoria_humana`; conformidade do `Protocol`)
    - **Validates: Requirements 7.5, 16.1, 16.4**

- [x] 5. Backend Estágio 4: Operacionalização — Abilities + parâmetros de ambiente (DTOs novos)
  - [x] 5.1 Definir os DTOs novos de parâmetros de ambiente
    - Em `ui/backend/app/services/` (pacote de operacionalização/ambiente), criar `PlataformaAmbiente(str, enum.Enum)` com `CONTAINER = "conteiner"` (comentário/espaço para `maquina_virtual` futuro, sem implementar — Req. 17.2), e os modelos Pydantic `HostAlvo` (rotulo, endereco, container, host_type, vulnerabilidades), `ServicoRequerido` (descricao, tecnica_origem) e `ParametrosDeAmbiente` (caso_id, plataforma, hosts, servicos, network_context, origem `TranslationSource`)
    - _Requirements: 8.3, 8.4, 17.1, 17.2_
  - [x] 5.2 Implementar a costura `OperacionalizadorPorOrigem` + `CuradoriaOperacionalizador`
    - Definir o `Protocol` `@runtime_checkable` `OperacionalizadorPorOrigem` (`origem() -> TranslationSource`, `operacionalizar(caso, dag) -> OperacionalizacaoProduzida`) e `CuradoriaOperacionalizador` que reusa `assemble_stage2`/`CaseService` para as Abilities ordenadas (`atomic_ordering`, `origem_traducao=curadoria_humana`, sem rede/IA) e DERIVA os `ParametrosDeAmbiente` dos nós do DAG curado: `attacker_commands` via `command_parser`/`parse_command` → `container_for_ip` resolve `endereco`/`container`; `target_vuln_host_a`/`_b` → `vulnerabilidades`; `source_host_type`/`target_host_type` → `host_type`; `network_context` → notas; serviços inferidos dos comandos; `plataforma = conteiner`; a Saída são os dois artefatos tipados separados
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 11.3, 11.4, 16.2_
  - [x] 5.3 Adicionar os endpoints REST do Estágio 4 em `ui/backend/app/api/casos.py`
    - `GET /api/casos/{caso}/estagio/4` (Abilities + `ParametrosDeAmbiente`) e `POST /api/casos/{caso}/estagio/4/concluir` (conclui quando ambos os artefatos são produzidos)
    - _Requirements: 8.4, 8.7, 8.8_
  - [x] 5.4 Escrever property test da derivação dos parâmetros de ambiente
    - **Property 6: Derivação dos parâmetros de ambiente a partir dos nós curados**
    - **Validates: Requirements 8.3, 8.4, 17.1**
  - [x] 5.5 Escrever property test da origem extensível (Estágio 4)
    - **Property 13: Origem explícita e extensível** (parte Estágio 4; conformidade de `OperacionalizadorPorOrigem`; stub `gerado_ia` também satisfaz)
    - **Validates: Requirements 8.6, 16.2, 16.4**

- [x] 6. Checkpoint - garantir Estágios 1..4 do backend verdes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Backend Estágio 5: Criação do ambiente (costura nova + reuso `containment/isolation`)
  - [x] 7.1 Definir os DTOs `AmbienteProvisionado`/`IsolationResumo` e a costura `ProvisionadorDeAmbiente`
    - Criar os modelos Pydantic `IsolationResumo` (veredito por container + geral) e `AmbienteProvisionado` (caso_id, plataforma, containers_alvo, compose_file, isolamento, abilities_carregadas, origem); definir o `Protocol` `@runtime_checkable` `ProvisionadorDeAmbiente` (`origem() -> TranslationSource`, `provisionar(params) -> AmbienteProvisionado`, `desprovisionar(...)`)
    - _Requirements: 9.1, 9.8, 16.3_
  - [x] 7.2 Implementar `CuradoriaProvisionadorDeAmbiente`
    - Usar o ambiente curado (`docker/docker-compose.yml` com as três redes internas, apenas contêineres — Req. 9.3/9.4), resolver os `containers_alvo` a partir dos `ParametrosDeAmbiente`, carregar as Abilities ordenadas no Caldera na ordem do DAG (reusar `CalderaClient.load_abilities`/`load_adversary`) e VERIFICAR o isolamento (read-only) via `inspect_isolation` ANTES de concluir (fail-closed: lista vazia/daemon indisponível reprova); se algum container-alvo não está isolado, o Estágio 5 não conclui e o container é reportado com a razão
    - _Requirements: 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 14.1, 14.2, 14.3, 14.4, 11.1, 11.3, 11.4_
  - [x] 7.3 Adicionar os endpoints REST do Estágio 5 em `ui/backend/app/api/casos.py`
    - `GET /api/casos/{caso}/estagio/5` (topologia + isolamento) e `POST /api/casos/{caso}/estagio/5/provisionar` (provisiona + carrega Abilities + verifica isolamento; não conclui sem isolamento)
    - _Requirements: 9.6, 9.7, 9.8, 9.9_
  - [x] 7.4 Escrever property test do isolamento como pré-condição do Estágio 5
    - **Property 8: Isolamento é pré-condição de conclusão do Estágio 5** (inspector fake isolado/with-bridge/with-external-dns)
    - **Validates: Requirements 9.6, 9.7, 14.1, 14.2, 14.3**
  - [x] 7.5 Escrever property test da origem extensível (Estágio 5)
    - **Property 13: Origem explícita e extensível** (parte Estágio 5; conformidade de `ProvisionadorDeAmbiente`; `CuradoriaProvisionadorDeAmbiente` carimba `curadoria_humana`)
    - **Validates: Requirements 16.3, 16.4**
  - [x] 7.6 Escrever teste de integração com Caldera/Docker reais do Estágio 5 (lab)
    - Marcar `@pytest.mark.lab` + `skipif(PIPELINE_UI_LAB != "1")`: provisionar o ambiente curado, carregar Abilities e verificar isolamento read-only contra o Docker real
    - _Requirements: 9.4, 9.5, 9.6_

- [x] 8. Backend Estágio 6: Execução, medição e relatório (reuso `OperationRunner` + métricas novas)
  - [x] 8.1 Definir os DTOs de métricas e relatório
    - Criar os modelos Pydantic `MetricaTecnica` (technique_id, node_id, status, etapa_falha, tempo_execucao_s, telemetria), `MetricasSuficiencia` (total_tecnicas_dag, tecnicas_sucesso, taxa_sucesso, cobertura_procedural, tempo_total_s, por_tecnica, nos_falhos, veredito_sucesso) e `RelatorioExecucao` (caso_id, metricas, resultados `Stage3AbilityResult`, ambiente_desprovisionado, gerado_em)
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8_
  - [x] 8.2 Costurar a execução do Estágio 6 reusando `OperationRunner` com os quatro gates
    - Reusar `OperationRunner.run(...)`: (1) confirmação explícita; (2) contenção de destinos (`validate_abilities` → recusa se houver destino externo, identifica Ability/comando); (3) isolamento dos containers-alvo (read-only); (4) disponibilidade do Caldera; execução ÚNICA (sem retry — Req. 18.3); persistir `AbilityResult` + `AuditLogEntry` por comando; o preview (`preview_abilities`) é pré-condição visível antes de qualquer execução
    - _Requirements: 10.1, 10.2, 10.3, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 14.4, 14.5, 18.3_
  - [x] 8.3 Implementar a medição de suficiência procedural e o relatório
    - Derivar `MetricasSuficiencia` dos `AbilityResult`/links e do DAG: `taxa_sucesso = tecnicas_sucesso / total_tecnicas_dag`, `cobertura_procedural ∈ [0,1]`, `nos_falhos` = nós com status falha + etapa do DAG, tempo por técnica/total, telemetria por técnica, `veredito_sucesso` pelo critério definido; consolidar tudo em `RelatorioExecucao`
    - _Requirements: 10.4, 10.5, 10.6, 10.7, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7_
  - [x] 8.4 Implementar o desprovisionamento ao final do Estágio 6
    - Após medir e gerar o relatório, SEMPRE desprovisionar o ambiente via `ProvisionadorDeAmbiente.desprovisionar` (remove os contêineres criados), em sucesso ou falha de técnicas; falha de desprovisionamento é reportada identificando os recursos remanescentes; concluir o Estágio 6 quando a execução concluiu, o ambiente foi desprovisionado e os resultados + relatório estão persistidos
    - _Requirements: 10.8, 10.9, 12.1, 12.2, 12.3_
  - [x] 8.5 Adicionar os endpoints REST do Estágio 6 e do relatório em `ui/backend/app/api/casos.py`
    - `POST /api/casos/{caso}/estagio/6/execucao/preview` (comandos + container-alvo + flags de destino externo), `POST /api/casos/{caso}/estagio/6/execucao` (confirmação + execução com os gates; 409 contenção/isolamento, 503 Caldera indisponível, 200 não-confirmada) e `GET /api/casos/{caso}/relatorio` (relatório + métricas)
    - _Requirements: 13.6, 10.2, 10.7, 15.7_
  - [x] 8.6 Escrever property test da segurança (nenhuma execução com destino externo ou contêiner não isolado)
    - **Property 7: Nenhuma execução com destino externo ou contêiner não isolado**
    - **Validates: Requirements 10.2, 13.4, 13.5, 14.2, 14.4, 14.5, 9.6, 9.7**
  - [x] 8.7 Escrever property test do desprovisionamento sempre após a execução
    - **Property 9: Desprovisionamento sempre ocorre após a execução** (mock de provisionador; teardown chamado antes de concluir, em sucesso e em falha)
    - **Validates: Requirements 10.8, 12.1, 12.2**
  - [x] 8.8 Escrever property test das métricas de suficiência consistentes
    - **Property 10: Métricas de suficiência consistentes com os resultados**
    - **Validates: Requirements 15.1, 15.2, 15.3, 15.6, 15.7, 10.4, 10.5, 10.6**
  - [x] 8.9 Escrever teste de integração com Caldera/Docker reais do Estágio 6 (lab)
    - Marcar `@pytest.mark.lab` + `skipif(PIPELINE_UI_LAB != "1")`: executar 1–2 casos curados contra o Caldera real com contenção/isolamento satisfeitos, medir e desprovisionar
    - _Requirements: 10.3, 10.8, 12.1_

- [x] 9. Checkpoint - garantir Estágios 5 e 6 do backend verdes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Persistência e migração Alembic para 1..6
  - [x] 10.1 Atualizar os modelos de domínio e adicionar as tabelas novas em `ui/backend/app/models/domain.py`
    - Atualizar a docstring de `StageRun.estagio` para "1..6" (coluna `Integer` pura, sem `CHECK` de intervalo a mexer); adicionar `versoes_stix` (`versao` PK, `curada` Boolean, `artefatos_curados_path` String nullable), `parametros_ambiente` (`id` PK, `caso_id` FK, `plataforma` via `_portable_enum(PlataformaAmbiente, "plataforma_ambiente")` já aceitando `maquina_virtual` no CHECK sem migração futura — precedente `OrigemDag`, `conteudo` JSON, `origem` via `translation_source`, `criado_em`) e `relatorios_execucao` (`id` PK, `caso_id` FK, `operacao_id` FK nullable, `metricas` JSON, `veredito_sucesso` Boolean, `ambiente_desprovisionado` Boolean, `gerado_em`)
    - _Requirements: 1.2, 5.5, 5.6, 8.3, 12.1, 15.7, 16.4, 17.1, 17.2_
  - [x] 10.2 Criar a migração Alembic encadeada sobre `1cc7b8b24bd2`
    - PostgreSQL-only, `op.get_bind()` + SQL textual portável. `upgrade()`: (1) guarda que aborta com `RuntimeError` descritivo se houver `estagio` fora de 1..5, sem alterar dados; (2) capturar `caso_id` cujo antigo Estágio 1 (Modelagem) estava `concluido`; (3) remapear `n → n+1` (faixa temporária `+100`/`-100`, ordem decrescente) preservando `estado`/`progresso`/`mensagem_erro`/`etapa_falha`/`caso_id`/`dag_escolhido_id`; (4) criar a linha de Seleção da base (novo 1) `concluido`/`progresso=100` para cada `caso_id` capturado; (5) `CREATE TABLE versoes_stix`/`parametros_ambiente`/`relatorios_execucao` (enums via `sa.Enum(..., native_enum=False)`) e semear `versoes_stix` com a versão 18 `curada=true` (`VERSAO_CURADA`). `downgrade()` simétrico: remover as linhas de Seleção da base (todas em `estagio=1`), inverter `n → n-1`, remover as três tabelas
    - _Requirements: 1.2, 1.5, 5.5, 5.6, 8.3, 15.7_
  - [x] 10.3 Escrever property test da migração 1..6
    - **Property 11: Ampliar o range para 6 não quebra a derivação de estado/progresso** (remap `n→n+1` preserva atributos; cria a linha de Seleção da base quando a Modelagem concluiu; `up∘down` identidade; guarda aborta sem mutar)
    - **Validates: Requirements 1.2, 1.5**
  - [x] 10.4 Escrever property test da origem persistida extensível (`gerado_ia` sem migração)
    - **Property 13: Origem explícita e extensível** (persistir `parametros_ambiente.origem = gerado_ia` e `plataforma = maquina_virtual` sem alteração de esquema)
    - **Validates: Requirements 16.4, 17.2**

- [x] 11. Backend WebSocket de Estágios em 1..6
  - [x] 11.1 Estender o canal `WS /ws/estagios/{caso}` para 1..6
    - Garantir que `run_stage_progress_channel`/`ws/channels.py` valide `estagio` contra `VALID_STAGES` (1..6) e empurre `StageProgressEvent`; atualizar docstrings/limites
    - _Requirements: 1.2, 3.1_
  - [x] 11.2 Escrever testes de integração dos contratos REST 1..6 e WS
    - `GET /api/casos` e `GET /api/casos/{caso}` retornam 6 Estágios; cada `GET .../estagio/{1..6}` responde o contrato; `POST .../estagio/6/execucao` respeita os gates (409/503/200); WS aceita 1..6; i18n paridade pt-BR/en com "Estágio"/"Stage"
    - _Requirements: 1.1, 1.2, 10.2, 13.6_

- [x] 12. Checkpoint - garantir backend verde ponta a ponta
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Frontend: tipos, wrappers de API e reseleção
  - [x] 13.1 Atualizar tipos e wrappers em `ui/frontend/src/lib`
    - `NumeroEstagio = 1|2|3|4|5|6`, `PIPELINE_STAGES = [1,2,3,4,5,6]`; wrappers tipados dos endpoints novos (bases STIX + baixar, estágio 1..6, selecionar campanha, concluir operacionalização, provisionar, preview/execução, relatório); `connectEstagios` aceita 1..6; tipos `ParametrosDeAmbiente`/`HostAlvo`/`AmbienteProvisionado`/`IsolationResumo`/`RelatorioExecucao`/`MetricasSuficiencia`
    - _Requirements: 1.2, 4.2, 4.3, 5.7, 6.3, 8.4, 9.8, 15.7_
  - [x] 13.2 Implementar a navegação Próximo/Voltar com reseleção no fluxo
    - Em `ui/frontend/src/lib/flow.ts`/estado do wizard: "Próximo" avança, "Voltar" retorna; enquanto não acionar "Voltar", a seleção confirmada permanece; ao Voltar + alterar + "Próximo", adotar a nova seleção; o reset dos `StageRun` dos Estágios seguintes é derivado do backend (reseleção de um Estágio anterior reseta os posteriores para `nao_iniciado`)
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.6_
  - [x] 13.3 Escrever property test da reseleção ao Voltar (reset dos Estágios seguintes)
    - **Property 12: Reseleção ao Voltar recalcula os Estágios seguintes**
    - **Validates: Requirements 4.4, 4.5, 4.6**

- [x] 14. Frontend: wizard de 6 Estágios e telas por Estágio
  - [x] 14.1 Criar `BaseStixView` (Estágio 1)
    - `ui/frontend/src/components/BaseStixView.tsx`: listar versões, baixar quando ausente e exibir o dashboard do bundle (reusar a tela de bases/dashboard do fluxo `fluxo-stix-dashboard`); habilitar "Próximo" quando o bundle está em disco e o dashboard foi exibido
    - _Requirements: 4.1, 5.7, 5.8, 5.10_
  - [x] 14.2 Criar `CampanhaView` (Estágio 2)
    - `ui/frontend/src/components/CampanhaView.tsx`: listar as campanhas do bundle, selecionar uma e exibir os atributos reusando `Stage1View`; concluir quando os atributos foram extraídos
    - _Requirements: 4.1, 6.2, 6.3, 6.5_
  - [x] 14.3 Criar `ParametrosAmbienteView` (painel do Estágio 4) e `RelatorioView` (Estágio 6)
    - `ParametrosAmbienteView.tsx`: painel que exibe hosts-alvo, serviços/vulnerabilidades, `network_context` e plataforma ao lado de `Stage2View` (Abilities); `RelatorioView.tsx`: taxa de sucesso, cobertura procedural, tempo, nós falhos, telemetria por técnica e veredito a partir do `RelatorioExecucao`
    - _Requirements: 4.1, 8.3, 8.4, 10.6, 10.7, 15.7_
  - [x] 14.4 Expandir `ExecutionWizard.tsx` para 6 Estágios e costurar os corpos
    - `ui/frontend/src/components/ExecutionWizard.tsx`: `stepIndicator` com `total: 6` e trilha de 6 badges; mapear 1 `BaseStixView`, 2 `CampanhaView`, 3 `DagStageView`, 4 `Stage2View` + `ParametrosAmbienteView`, 5 `EnvironmentStageView` (conclui quando isolado), 6 `Stage3View` + `EmulationConfirmModal` + `RelatorioView`; derivar o bloqueio sequencial do backend
    - _Requirements: 4.1, 4.2, 4.3_
  - [x] 14.5 Unificar as chaves i18n dos seis Estágios em `ui/frontend/src/i18n`
    - Substituir `stages.1..5` pelos nomes dos 6 Estágios (Seleção da base STIX / Seleção da campanha / Geração do DAG / Operacionalização / Criação do ambiente de simulação / Execução, medição e relatório) e equivalentes em inglês, adicionar `stages.6` e as chaves novas `parametrosAmbiente.*`, `relatorio.*`, `metricas.*`; manter paridade pt-BR/en
    - _Requirements: 1.1, 4.1, 8.3, 15.7_
  - [x] 14.6 Escrever testes de UI (Vitest) do wizard e das telas novas
    - "Passo X de 6", 6 badges, mapeamento dos corpos, Próximo/Voltar + reseleção; `BaseStixView` (baixar/dashboard), `CampanhaView` (seleção), `ParametrosAmbienteView`, `EnvironmentStageView` (isolamento bloqueia avanço), `RelatorioView`
    - _Requirements: 4.1, 4.2, 4.3, 5.7, 6.2, 8.3, 9.7, 15.7_
  - [x] 14.7 Escrever property test da paridade i18n pt-BR/en (fast-check)
    - **Property: paridade de chaves pt-BR/en** para os seis Estágios e as chaves novas (`parametrosAmbiente.*`, `relatorio.*`, `metricas.*`); termos "Estágio"/"Stage"
    - _Requirements: 1.1, 4.1_

- [x] 15. Final checkpoint - garantir todas as suítes verdes
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tarefas marcadas com `*` são opcionais (testes) e podem ser puladas para um MVP mais rápido; as tarefas de implementação principal nunca são opcionais.
- A **alavanca principal** (tarefa 1) é ampliar o range 1..5 → 1..6 sem renomear as classes históricas `Stage*` (dívida técnica fora de escopo, Req. 18.5); os algoritmos de bloqueio/progresso/derivação já são range-agnósticos.
- Cada tarefa referencia cláusulas específicas dos requisitos para rastreabilidade; os checkpoints asseguram validação incremental.
- Testes de propriedade (Hypothesis/fast-check, ≥100 iterações) validam as Properties 1..13 do design; testes de exemplo/integração/snapshot cobrem download do bundle, dashboard, contratos REST (409/503), i18n e UI.
- Testes de ambiente REAL/Caldera/Docker ficam marcados `lab` (deselecionados por padrão, guardados por `PIPELINE_UI_LAB`), seguindo o precedente do projeto.
- **PostgreSQL exclusivamente.** A migração roda contra o Postgres do produto; os property tests da migração a exercitam in-process dirigindo `upgrade()`/`downgrade()` reais.
- **Não-objetivos (Req. 18):** não implementar o agente de IA; não implementar seletor/parametrizador do Atomic Red Team; sem ciclos de retry automáticos; sem otimização de provisionamento/teardown; sem renomear/migrar as classes `Stage*`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5", "2.2", "5.1"] },
    { "id": 2, "tasks": ["2.3", "2.4", "3.1", "7.1", "8.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "4.1", "7.2"] },
    { "id": 4, "tasks": ["4.2", "4.3", "4.4", "5.2", "7.4", "7.5", "7.6"] },
    { "id": 5, "tasks": ["5.3", "5.4", "5.5", "8.2"] },
    { "id": 6, "tasks": ["8.3", "8.6", "8.8"] },
    { "id": 7, "tasks": ["8.4", "8.7", "8.9", "10.1"] },
    { "id": 8, "tasks": ["8.5", "10.2", "11.1"] },
    { "id": 9, "tasks": ["10.3", "10.4", "11.2", "13.1"] },
    { "id": 10, "tasks": ["13.2", "14.1", "14.2", "14.3", "14.5"] },
    { "id": 11, "tasks": ["13.3", "14.4"] },
    { "id": 12, "tasks": ["14.6", "14.7"] }
  ]
}
```
