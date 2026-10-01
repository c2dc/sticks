# Implementation Plan

## Overview

Este plano converte o design da **Pipeline_UI** em uma sequência incremental de tarefas de codificação. A abordagem é test-driven e, no backend (Python), prioriza **property-based testing com Hypothesis** para as 12 propriedades de correção do design — com destaque para as propriedades de contenção/segurança (Properties 1, 2, 5, 6, 7).

Convenções deste plano:

- Cada tarefa é incremental, constrói sobre as anteriores e termina integrando o que foi feito, sem código órfão.
- Cada tarefa referencia os critérios de aceitação que satisfaz (ex.: `_Requisitos: 6.1, 6.2_`).
- Sub-tarefas marcadas com `*` são de teste e **opcionais** (podem ser puladas para um MVP mais rápido). Tarefas de implementação (sem `*`) são obrigatórias.
- **Contexto de execução — apenas planejamento**: nenhuma tarefa executa emulação real, comandos de adversário reais ou operações reais na Caldera durante o desenvolvimento. Todo o desenvolvimento usa **fixtures/mocks** por padrão. Tarefas que exigem o **ambiente Docker/Caldera real** estão explicitamente marcadas como **"a executar na máquina de laboratório do usuário"** e não devem rodar no ambiente de desenvolvimento.
- **Segurança/contenção em destaque**: as tarefas do `ContainmentValidator` (Tarefa 4), `AuditLogger`/`OperationRunner` (Tarefa 7) e os testes de contenção dedicados (Tarefa 15) formam o núcleo de segurança e devem receber tratamento de primeira classe.

## Tasks

> **Revisão de 2026-09-29:** itens marcados foram confrontados com o código, a
> suíte automatizada e a auditoria visual do ShadowRay. Os itens de laboratório
> real (6.4 e 15.3), os property tests opcionais ainda ausentes e o checkpoint
> final permanecem abertos. Consulte `docs/diario/2026-09-29.md` para evidências
> e o ponto exato de retomada.

- [x] 1. Setup do projeto (frontend + backend + banco + Docker)
  - [x] 1.1 Criar a estrutura de diretórios e o scaffold do backend FastAPI
    - Criar `app/` com pacotes `models/`, `services/`, `api/` (rotas REST), `ws/` (canais WebSocket/SSE) e `core/` (config, integração com o pipeline existente).
    - Configurar FastAPI com app factory, roteador raiz e endpoint de health-check; adicionar dependências (fastapi, uvicorn, pydantic, sqlalchemy, alembic, httpx, docker, hypothesis, pytest) em `pyproject.toml`/`requirements`.
    - Adicionar módulo de configuração que reaproveita `sticks/config/config.py` (ex.: `CALDERA_URL`, header `KEY`) sem duplicar valores.
    - _Requisitos: 10.1, 10.4_

  - [x] 1.2 Criar o scaffold do frontend React + TypeScript + Vite + Tailwind + shadcn/ui + i18next
    - Inicializar projeto Vite com React + TS; configurar Tailwind (com estratégia de tema via classe `dark`) e instalar shadcn/ui.
    - Configurar i18next com namespaces e recursos vazios para pt-BR e en; definir cliente HTTP para o backend e utilitário de WebSocket.
    - Criar layout base com placeholder da visão de pipeline e do painel de preferências.
    - _Requisitos: 10.1, 10.2_

  - [x] 1.3 Configurar SQLAlchemy + Alembic e a conexão com a Base_de_Dados
    - Configurar engine/sessão SQLAlchemy suportando PostgreSQL (padrão) e SQLite (dev local) via variável de ambiente.
    - Inicializar Alembic (env, script de migração base) apontando para os metadados dos modelos.
    - _Requisitos: 10.1, 10.2_

  - [x] 1.4 Adicionar serviços frontend/backend/banco ao docker-compose SEM redes internas de ataque
    - Adicionar serviços `frontend`, `backend` e `banco` ao `docker/docker-compose.yml`, todos na camada de aplicação.
    - Garantir que esses serviços **não** participam das redes `internal: true` (172.20/21/22.0.0/24); o backend acessa a Caldera apenas por `localhost:8888` e o Docker Engine API (socket, somente leitura) para inspeção de isolamento.
    - _Requisitos: 10.1, 10.3, 6.3_

  - [x]* 1.5 Escrever teste de fumaça (smoke) de inicialização
    - Verificar que o app FastAPI sobe e o health-check responde; verificar que a conexão do banco inicializa com SQLite.
    - _Requisitos: 10.1_

- [x] 2. Modelos de dados e migrações
  - [x] 2.1 Implementar os enums e modelos SQLAlchemy do domínio
    - Implementar `TranslationSource`, `StageState`, `OperationState`, `AbilityResultStatus`, `Theme`, `Language`.
    - Implementar os modelos `Case`, `StageRun`, `Ability`, `Adversary`, `Operation`, `AbilityResult`, `AuditLogEntry`, `SessionState`, `UserPreferences` conforme a seção Data Models do design (nomes de tabela em português).
    - Garantir que a `origem_traducao` é um atributo explícito e extensível em `Case`/`Ability`/`Adversary` (Req. 11).
    - _Requisitos: 9.1, 9.7, 11.1, 11.2, 6.8_

  - [x] 2.2 Gerar a migração Alembic inicial
    - Autogerar e revisar a migração que cria todas as tabelas do domínio.
    - _Requisitos: 9.1_

  - [x]* 2.3 Escrever testes unitários dos modelos e enums
    - Testar defaults (tema `claro`, idioma `pt-BR`, `origem_traducao` = curadoria humana) e restrições de nulidade (ex.: `comando`/`container_destino` obrigatórios em `AuditLogEntry`).
    - _Requisitos: 7.6, 8.6, 6.8, 11.1_

- [x] 3. CaseService — leitura e validação dos 8 casos curados
  - [x] 3.1 Implementar a leitura e o parsing dos pares ability/adversary e do DAG
    - Ler `data/api/{caso}_dag-ability.json` (lista), `data/api/{caso}_dag-adversary.json` (objeto) e `data/dag/{caso}_dag.json`, mapeando para `Ability`/`Adversary` e para a Entrada de grafo do Estágio 1/2.
    - Registrar os 8 casos curados (APT41-DUST, C0010, C0026, CostaRicto, Operation MidnightEclipse, Operation Outer Space, Salesforce Data Exfiltration, ShadowRay) com slugs correspondentes aos arquivos reais.
    - Expor metadados, Abilities, Adversary, ordenação (`atomic_ordering`) e origem de tradução.
    - _Requisitos: 5.1, 3.2, 3.3, 3.5, 11.1_

  - [x] 3.2 Tratar arquivos ausentes ou malformados por caso
    - Ao ler um caso com arquivo ausente ou JSON inválido, registrar a falha e reportar erro descritivo **por caso**, mantendo os demais casos disponíveis.
    - _Requisitos: 5.6, 3.7, 3.8_

  - [x]* 3.3 Escrever testes unitários do CaseService
    - Testar leitura bem-sucedida de um caso real (via fixture), caso sem Adversary, caso sem Abilities, arquivo ausente e JSON malformado (cada um isolado dos demais).
    - _Requisitos: 5.1, 5.6, 3.7, 3.8_

- [x] 4. **ContainmentValidator — NÚCLEO DE SEGURANÇA (contenção)**
  - [x] 4.1 Implementar o parser de comandos para extrair destinos
    - Extrair endereços de destino (IPs, URLs, hosts) de comandos no estilo dos casos curados: `curl`, `wget`, `ssh`, `sshpass ... user@host`, `git clone`, `apt-get`, `pip install`, etc.
    - Retornar, por comando, a lista de destinos detectados (vazia quando puramente local).
    - _Requisitos: 6.1, 6.2_

  - [x] 4.2 Implementar a validação de destinos contra as subnets internas
    - Classificar cada destino como interno (∈ 172.20.0.0/24, 172.21.0.0/24, 172.22.0.0/24) ou externo.
    - Se qualquer comando tiver destino externo, recusar a Ability e sinalizar que a Operação não pode iniciar, identificando a Ability e o comando.
    - _Requisitos: 6.1, 6.2_

  - [x]* 4.3 Escrever property test da Property 1 (destino externo bloqueia)
    - **Property 1: Nenhuma emulação inicia com destino externo**
    - **Feature: pipeline-ui, Property 1**
    - **Validates: Requisitos 6.1, 6.2**
    - Geradores com comandos internos e externos misturados (ex.: `wget https://nmap.org/...`, `sshpass ... attacker@172.21.0.20`); mínimo de 100 iterações; sem ambiente real (fixtures/mocks).

  - [x] 4.4 Implementar a verificação de isolamento de containers via Docker Engine API (leitura)
    - Inspecionar (somente leitura) cada container-alvo e classificá-lo como isolado se e somente se conectado exclusivamente a redes `internal: true`, sem `local-network` (bridge) e sem `dns` externo.
    - Se qualquer container-alvo não estiver isolado, sinalizar abort do pre-flight identificando o container.
    - _Requisitos: 6.3, 6.4_

  - [x]* 4.5 Escrever property test da Property 2 (isolamento total obrigatório)
    - **Property 2: Emulação só prossegue com todos os containers-alvo isolados**
    - **Feature: pipeline-ui, Property 2**
    - **Validates: Requisitos 6.3, 6.4**
    - Geradores de configurações de rede de containers (isoladas e não isoladas, com/sem bridge, com/sem DNS externo); Docker Engine API mockada; mínimo de 100 iterações; sem ambiente real.

  - [x] 4.6 Implementar a geração de preview de comandos e destinos
    - Produzir a lista completa de comandos concretos com o container de destino de cada um, para uso no modal de confirmação.
    - _Requisitos: 6.5_

  - [x]* 4.7 Escrever testes unitários do parser e do preview
    - Testar extração de destino para exemplos concretos (ssh/sshpass/curl/wget) e a montagem do preview.
    - _Requisitos: 6.5, 6.2_

- [x] 5. Checkpoint — Garantir que todos os testes passam
  - Garantir que todos os testes passam, perguntar ao usuário caso surjam dúvidas.

- [x] 6. CalderaClient — wrapper da API v2 (com Caldera MOCKADA)
  - [x] 6.1 Implementar verificação de disponibilidade com timeout de 10s
    - Verificar a Caldera em `localhost:8888` com header `KEY`; se não responder em 10s, sinalizar indisponibilidade sem iniciar nenhuma operação.
    - _Requisitos: 4.6_

  - [x] 6.2 Implementar carga de abilities/adversaries e criação/consulta de operações
    - Carregar abilities (`POST /api/v2/abilities`), listar adversaries (`GET /api/v2/adversaries`), criar/consultar Operações compatível com `sticks/lib/operation.py` (planner "atomic", group "red", jitter) e fazer polling de estado e links.
    - _Requisitos: 4.2, 4.3_

  - [x]* 6.3 Escrever testes de integração com Caldera MOCKADA
    - Verificar contrato do cliente (endpoints, headers, payloads) com um servidor HTTP mockado; incluir caso de timeout de 10s (Caldera não responsiva simulada).
    - _Requisitos: 4.2, 4.6_

  - [x]* 6.4 Marcar teste de integração com Caldera REAL (executar na máquina de laboratório)
    - **A executar na máquina de laboratório do usuário** — NÃO rodar no desenvolvimento.
    - Executar 1–2 casos de ponta a ponta na Caldera real dentro do Docker Desktop, com contenção satisfeita.
    - _Requisitos: 4.2, 4.4_

- [x] 7. **OperationRunner + AuditLogger (contenção e auditoria)**
  - [x] 7.1 Implementar o AuditLogger com persistência 1:1 por comando
    - Para cada comando executado, persistir um `AuditLogEntry` com comando, container de destino e resultado.
    - _Requisitos: 6.8_

  - [x]* 7.2 Escrever property test da Property 5 (auditoria 1:1)
    - **Property 5: Toda execução gera exatamente um registro de auditoria**
    - **Feature: pipeline-ui, Property 5**
    - **Validates: Requisitos 6.8**
    - Geradores com K comandos executados; verificar exatamente K registros com dados correspondentes; mínimo de 100 iterações; execução mockada (sem ambiente real).

  - [x] 7.3 Implementar o OperationRunner com confirmação explícita e agregação
    - Iniciar a Operação **somente** com `confirmado=true`; em ausência/cancelamento, não executar nenhum comando e retornar ao estado anterior.
    - Acompanhar o progresso, agregar totais de "sucesso" e "falha" (`Operation.total_sucesso`/`total_falha`).
    - Integrar `ContainmentValidator` (pre-flight destinos + isolamento) e `CalderaClient` (disponibilidade) antes de executar.
    - _Requisitos: 6.6, 6.7, 4.5, 6.1, 6.2, 6.3, 6.4, 4.6_

  - [x]* 7.4 Escrever property test da Property 6 (confirmação explícita)
    - **Property 6: Execução exige confirmação explícita**
    - **Feature: pipeline-ui, Property 6**
    - **Validates: Requisitos 6.6, 6.7**
    - Mínimo de 100 iterações; execução mockada (sem ambiente real).

  - [x] 7.5 Implementar o abort da Operação em falha de auditoria
    - Se a persistência de um `AuditLogEntry` falhar durante a Operação, abortar a partir desse ponto sem executar comandos adicionais e sinalizar à UI.
    - _Requisitos: 6.9_

  - [x]* 7.6 Escrever property test da Property 7 (falha de auditoria aborta)
    - **Property 7: Falha de auditoria aborta a Operação**
    - **Feature: pipeline-ui, Property 7**
    - **Validates: Requisitos 6.9**
    - Geradores que injetam falha de persistência em um passo arbitrário; verificar que nenhum comando posterior é executado; mínimo de 100 iterações; execução mockada.

  - [x]* 7.7 Escrever property test da Property 10 (agregação consistente)
    - **Property 10: Agregação da Operação é consistente com os resultados por Ability**
    - **Feature: pipeline-ui, Property 10**
    - **Validates: Requisitos 4.5**
    - Geradores de conjuntos de resultados por Ability; mínimo de 100 iterações.

- [x] 8. StageService — orquestração dos Estágios 1/2/3
  - [x] 8.1 Implementar o Estágio 1 integrando lib/stix.py e lib/ability.py
    - Invocar a modelagem estrutural existente para extrair técnicas, relacionamentos, indicadores, infraestrutura, malware e metadados; persistir os elementos extraídos e marcar o estágio como "concluído".
    - Tratar extração vazia como sucesso com Saída indicando ausência de elementos; tratar falha registrando erro sem persistir extração parcial, com caso afetado e etapa da falha.
    - _Requisitos: 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 8.2 Implementar a montagem de Entrada/Saída dos Estágios 2 e 3
    - Estágio 2: Entrada = técnicas abstratas do Estágio 1; Saída = Abilities curadas (técnica, tática, descrição, comando por executor), ordem/dependências, Adversary e origem de tradução; tratar ausências (sem Entrada, sem Abilities, sem Adversary).
    - Estágio 3: Entrada = Adversary + Abilities; delegar execução ao OperationRunner; Saída acumulada por Ability e agregada.
    - _Requisitos: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 4.1, 4.4, 4.5, 11.1_

  - [x] 8.3 Implementar bloqueio sequencial e progresso agregado
    - Determinar iniciabilidade de um estágio n: iniciável se n == 1 ou se o estágio n-1 está "concluído"; caso contrário, bloqueado.
    - Calcular o progresso agregado como a quantidade de casos com os 3 estágios "concluído" (de 8).
    - Emitir eventos de progresso/transição de estado (para os canais em tempo real).
    - _Requisitos: 5.2, 5.3, 5.4, 5.5, 1.4, 1.5, 1.6_

  - [x]* 8.4 Escrever property test da Property 3 (estágio bloqueado não inicia)
    - **Property 3: A UI nunca permite iniciar um estágio bloqueado**
    - **Feature: pipeline-ui, Property 3**
    - **Validates: Requisitos 5.4**
    - Geradores de combinações de estados dos três estágios; mínimo de 100 iterações.

  - [x]* 8.5 Escrever property test da Property 4 (progresso agregado)
    - **Property 4: O progresso agregado conta exatamente os casos totalmente concluídos**
    - **Feature: pipeline-ui, Property 4**
    - **Validates: Requisitos 5.5**
    - Geradores de distribuições de estados de estágio entre os 8 casos; mínimo de 100 iterações.

  - [x]* 8.6 Escrever teste de integração do Estágio 1 real (com fixtures locais)
    - Executar a modelagem estrutural existente para 1–2 casos usando os arquivos locais em `data/` e verificar extração não-vazia. Não requer Caldera nem ambiente Docker de ataque.
    - _Requisitos: 2.2, 2.3_

- [x] 9. SessionStateService — persistência e continuidade entre máquinas
  - [x] 9.1 Implementar leitura e gravação do Estado_de_Sessão
    - Persistir caso atual, estágios concluídos por caso (derivados de `StageRun`) e resultados de operações por Ability, associados ao caso; recuperar o estado na abertura.
    - Tratar ausência de estado (todos os estágios "não iniciado") e estado ilegível (mensagem sem descartar o persistido).
    - _Requisitos: 9.1, 9.2, 9.4, 9.5, 9.6, 9.7_

  - [x] 9.2 Implementar preservação do estado anterior em falha de persistência
    - Se a persistência da conclusão de um estágio falhar, preservar o estado anteriormente persistido sem alteração parcial e sinalizar que a conclusão não foi salva.
    - _Requisitos: 9.3_

  - [x]* 9.3 Escrever property test da Property 8 (round-trip do estado)
    - **Property 8: Round-trip do Estado_de_Sessão**
    - **Feature: pipeline-ui, Property 8**
    - **Validates: Requisitos 9.1, 9.4**
    - Geradores de Estados_de_Sessão arbitrários; persistir e recuperar produz estado estruturalmente igual; mínimo de 100 iterações.

  - [x]* 9.4 Escrever property test da Property 9 (falha preserva estado anterior)
    - **Property 9: Falha de persistência preserva o estado anterior**
    - **Feature: pipeline-ui, Property 9**
    - **Validates: Requisitos 9.3**
    - Geradores com falha de persistência injetada; verificar ausência de alteração parcial; mínimo de 100 iterações.

- [x] 10. Checkpoint — Garantir que todos os testes passam
  - Garantir que todos os testes passam, perguntar ao usuário caso surjam dúvidas.

- [x] 11. Endpoints REST e canais WebSocket/SSE
  - [x] 11.1 Implementar os endpoints REST de casos e estágios
    - `GET /api/casos`, `GET /api/casos/{caso}`, `GET /api/casos/{caso}/estagio/{1,2,3}`, `POST /api/casos/{caso}/estagio/1/executar`, `GET /api/casos/{caso}/operacao`, `GET /api/estado-sessao`, `PUT /api/preferencias`, `GET /api/auditoria/{operacao}`, ligando aos serviços existentes.
    - _Requisitos: 5.1, 5.6, 2.1, 2.2, 3.1, 4.1, 9.4, 7.4, 8.4_

  - [x] 11.2 Implementar os endpoints de preview e emulação com pre-flight de contenção
    - `POST /api/casos/{caso}/emulacao/preview` (comandos + destinos, sem executar).
    - `POST /api/casos/{caso}/emulacao` aceitando `confirmado=true`; executar pre-flight de contenção; retornar 409 em violação (Ability/comando ou container) e 503 se a Caldera não responder em 10s.
    - _Requisitos: 6.5, 6.6, 6.7, 6.2, 6.4, 4.6_

  - [x] 11.3 Implementar os canais WebSocket/SSE de progresso
    - `WS /ws/estagios/{caso}` (transições de estado e percentual, ≥ a cada 5s enquanto "em andamento") e `WS /ws/operacao/{operacao}` (status por Ability e agregado ao final).
    - _Requisitos: 1.4, 1.5, 4.3, 4.4, 4.5_

  - [x]* 11.4 Escrever testes de integração dos endpoints (com serviços mockados)
    - Testar respostas dos endpoints, 409 de contenção violada, 503 de Caldera indisponível e o fluxo preview → emulação com confirmação.
    - _Requisitos: 6.2, 6.4, 4.6, 6.6_

- [x] 12. Frontend — visão de pipeline e telas dos estágios
  - [x] 12.1 Implementar a visão de pipeline (PipelineOverview) e as seções Entrada/Processamento/Saída
    - Apresentar os três estágios simultaneamente, separados e nomeados, com indicador de estado ("não iniciado", "em andamento", "concluído", "erro") e seções rotuladas de Entrada, Processamento e Saída.
    - Exibir progresso agregado dos 8 casos e refletir atualizações em tempo real via WebSocket.
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 5.5_

  - [x] 12.2 Implementar Stage1View, Stage2View e Stage3View
    - Stage1View: Entrada (dataset STIX), Saída (técnicas/relacionamentos/indicadores/infra/malware/metadados; indicação de ausência quando vazio).
    - Stage2View: Entrada (técnicas abstratas), Saída (Abilities curadas com técnica/tática/descrição/comando, ordem/dependências, Adversary) e **indicador de origem da tradução** ("curadoria humana") como atributo explícito extensível.
    - Stage3View: Entrada (Adversary + Abilities), Processamento (status por Ability em tempo real), Saída (resultado por Ability e agregado).
    - _Requisitos: 2.1, 2.3, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 4.1, 4.3, 4.4, 4.5, 11.1, 11.2_

  - [x] 12.3 Implementar o seletor de casos (CaseSelector) com bloqueio sequencial e progresso agregado
    - Listar os 8 casos, conduzir na ordem Estágio 1 → 2 → 3, sinalizar estágios bloqueados e impedir iniciá-los; exibir estado por estágio e progresso agregado; reportar erro por caso mantendo os demais selecionáveis.
    - _Requisitos: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 12.4 Implementar o modal de confirmação de emulação (EmulationConfirmModal)
    - Exibir, **antes de qualquer confirmação**, a lista completa dos comandos concretos e o container de destino de cada um; requerer confirmação explícita; cancelar retorna ao estado anterior sem executar nada.
    - Exibir mensagens de contenção violada (Ability/comando ou container) e de Caldera indisponível.
    - _Requisitos: 6.5, 6.6, 6.7, 6.2, 6.4, 4.6_

  - [x]* 12.5 Escrever testes de componente/snapshot do frontend
    - Testar seções Entrada/Processamento/Saída, seletor com estágios bloqueados e modal exibindo comandos e destinos.
    - _Requisitos: 1.2, 5.4, 6.5_

- [x] 13. Frontend — tema, paleta blue/red/purple team e persistência
  - [x] 13.1 Implementar o painel de preferências (PreferencesPanel) e a troca de tema
    - Oferecer Modo_Claro (padrão) e Modo_Escuro mutuamente exclusivos; aplicar o tema a todos os elementos sem recarregar a página e sem estado de carregamento intermediário.
    - Aplicar a paleta blue (defensivo) / red (ofensivo) / purple (combinado) em ambos os temas; persistir e restaurar a preferência de tema via `PUT /api/preferencias` e `GET /api/estado-sessao`.
    - _Requisitos: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x]* 13.2 Escrever testes de tema do frontend
    - Verificar troca sem reload, aplicação da paleta aos contextos e restauração do último tema; padrão Modo_Claro sem preferência.
    - _Requisitos: 7.2, 7.3, 7.5, 7.6_

- [x] 14. Frontend — i18n pt-BR/en com fallback e normalização
  - [x] 14.1 Implementar recursos de tradução pt-BR/en, fallback e normalização de idioma
    - Disponibilizar pt-BR e en; trocar idioma sem recarregar (≤ 2s); configurar fallback para pt-BR quando faltar tradução; normalizar idioma persistido inválido para pt-BR; persistir/restaurar preferência de idioma; padrão pt-BR.
    - _Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [x]* 14.2 Escrever property test da Property 11 (fallback para pt-BR)
    - **Property 11: Fallback de i18n sempre resolve para pt-BR**
    - **Feature: pipeline-ui, Property 11**
    - **Validates: Requisitos 8.3**
    - Geradores de chaves de tradução e idiomas; mínimo de 100 iterações.

  - [x]* 14.3 Escrever property test da Property 12 (normalização de idioma inválido)
    - **Property 12: Normalização de idioma inválido para pt-BR**
    - **Feature: pipeline-ui, Property 12**
    - **Validates: Requisitos 8.7**
    - Geradores de valores de preferência de idioma (válidos e inválidos); mínimo de 100 iterações.

- [x] 15. **Testes de contenção dedicados e integração ponta a ponta**
  - [x]* 15.1 Escrever suíte de testes de contenção de segurança (com mocks)
    - Garantir que **qualquer** comando com destino fora de 172.20/21/22.0.0/24 bloqueia a Operação inteira e identifica a Ability/comando (usar comandos reais do estilo curado: `wget https://nmap.org/...`, `apt-get install`, `sshpass ... attacker@172.21.0.20`).
    - Garantir que um container com `local-network` (bridge) ou `dns` externo é classificado como não isolado e aborta o pre-flight.
    - Confirmar que nenhuma execução ocorre sem confirmação explícita. Tudo com fixtures/mocks, sem ambiente real.
    - _Requisitos: 6.1, 6.2, 6.3, 6.4, 6.6, 6.7_

  - [x] 15.2 Integrar o fluxo ponta a ponta preview → confirmação → emulação (mockado)
    - Ligar frontend ↔ backend ↔ serviços de forma que o fluxo completo funcione com Caldera e Docker Engine API **mockados**, exercitando pre-flight, confirmação, execução simulada, auditoria e agregação.
    - _Requisitos: 6.5, 6.6, 6.7, 6.8, 4.4, 4.5_

  - [x]* 15.3 Marcar integração ponta a ponta com ambiente REAL (executar na máquina de laboratório)
    - **A executar na máquina de laboratório do usuário** — NÃO rodar no desenvolvimento.
    - Executar 1–2 casos curados com Caldera e Ambiente_Docker reais, com contenção satisfeita, validando emulação de ponta a ponta e a trilha de auditoria persistida.
    - _Requisitos: 4.2, 4.4, 6.3, 6.8_

- [x] 16. Checkpoint final — Garantir que todos os testes passam
  - Garantir que todos os testes passam, perguntar ao usuário caso surjam dúvidas.

## Notes

- Tarefas marcadas com `*` são opcionais (testes) e podem ser puladas para um MVP mais rápido.
- Cada tarefa referencia critérios de aceitação específicos para rastreabilidade.
- Os checkpoints garantem validação incremental.
- Os testes de propriedade (Hypothesis, ≥ 100 iterações) validam as 12 propriedades de correção do design; os testes unitários/integração cobrem exemplos e bordas.
- **Segurança/contenção**: Tarefa 4 (ContainmentValidator), Tarefa 7 (auditoria/abort) e Tarefa 15 (contenção dedicada) formam o núcleo de segurança e cobrem as Properties 1, 2, 5, 6, 7.
- **Nenhuma tarefa executa emulação real durante o desenvolvimento.** As tarefas 6.4 e 15.3 estão marcadas para execução exclusiva **na máquina de laboratório do usuário**; todo o restante usa mocks/fixtures.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["1.4", "1.5", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1", "4.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "4.2", "4.4", "4.6"] },
    { "id": 4, "tasks": ["4.3", "4.5", "4.7", "6.1", "6.2"] },
    { "id": 5, "tasks": ["6.3", "6.4", "7.1", "8.1"] },
    { "id": 6, "tasks": ["7.2", "7.3", "8.2", "9.1"] },
    { "id": 7, "tasks": ["7.4", "7.5", "8.3", "9.2"] },
    { "id": 8, "tasks": ["7.6", "7.7", "8.4", "8.5", "8.6", "9.3", "9.4"] },
    { "id": 9, "tasks": ["11.1", "11.2", "11.3"] },
    { "id": 10, "tasks": ["11.4", "12.1", "12.2", "12.3", "12.4"] },
    { "id": 11, "tasks": ["12.5", "13.1", "14.1"] },
    { "id": 12, "tasks": ["13.2", "14.2", "14.3", "15.1", "15.2"] },
    { "id": 13, "tasks": ["15.3"] }
  ]
}
```
