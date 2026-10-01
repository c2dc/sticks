# Requirements Document

## Introduction

O **Wizard de Execução Guiada** transforma a navegação atual (chips e botões soltos no `PipelineOverview`) em um fluxo linear estilo assistente ("Avançar → Avançar → Executar") para uma campanha curada escolhida (ex.: ShadowRay). A partir da campanha selecionada, o Pesquisador caminha pelos 3 estágios — Estágio 1 (Modelagem Estrutural), Estágio 2 (Tradução Curada) e Estágio 3 (Emulação de Adversário) — vendo Entrada/Processamento/Saída de cada um e, no Estágio 3, disparando a emulação real no Caldera.

O estado de avanço é lido do backend/SQLite (fonte da verdade), respeitando o bloqueio sequencial já existente (um estágio só inicia se o anterior estiver concluído). O escopo é enxuto: destravar a navegação guiada e ligar as pontas que faltam, sem reescrever as telas Stage1/2/3View nem o modal de emulação, que já funcionam.

As pontas que faltam no backend são: um endpoint REST para concluir o Estágio 2 (a tradução já é curada, basta persistir a conclusão) e a persistência da conclusão do Estágio 3 após a emulação bem-sucedida. O serviço `SessionStateService.persist_stage_completion(caso_id, estagio)` já existe e é a única via de persistência de conclusão.

PostgreSQL é fase futura (fora de escopo); o design não deve exigir mudança no contrato da API para essa migração.

## Glossary

- **Wizard**: Componente de frontend que conduz o Pesquisador sequencialmente pelos 3 estágios de uma campanha, com controles "Avançar" e "Voltar".
- **Campanha**: Caso curado do pipeline (ex.: ShadowRay), identificado por um slug.
- **Estágio**: Uma das 3 etapas do pipeline por campanha (1 = Modelagem Estrutural, 2 = Tradução Curada, 3 = Emulação de Adversário).
- **Backend**: A API FastAPI em `pipeline_ui/backend` que expõe os endpoints de casos, estágios e emulação.
- **Estado_de_Sessão**: Registro persistido no SQLite (via `SessionStateService`) do caso atual e dos estágios concluídos por caso.
- **Estado_do_Estágio**: Estado derivado de um estágio para um caso: `nao_iniciado`, `em_execucao`, `concluido`, `erro` ou `bloqueado`.
- **Bloqueio_Sequencial**: Regra pela qual o Estágio N só pode iniciar se o Estágio N-1 estiver `concluido`.
- **Endpoint_Conclusao_Estagio2**: Novo endpoint `POST /api/casos/{caso}/estagio/2/concluir` que persiste a conclusão do Estágio 2.
- **Emulação**: Execução real das Abilities da campanha contra o Caldera, disparada por `POST /api/casos/{caso}/emulacao` com `confirmado=true`, após o pre-flight de contenção.
- **Pre_Flight_Contencao**: Verificação inegociável e já existente (destinos internos + isolamento) que antecede a emulação; o wizard apenas a aciona via o endpoint de emulação.
- **Reset_Campanha**: Ação que limpa o estado persistido de uma campanha (estágios concluídos e resultados de operação) para que a demo recomece do zero.

## Requirements

### Requirement 1 — Navegação guiada sequencial

**User Story:** Como Pesquisador, quero caminhar pelos 3 estágios de uma campanha com botões "Avançar" e "Voltar", para conduzir a demonstração de forma linear e previsível.

#### Acceptance Criteria

1. WHEN o Pesquisador seleciona uma Campanha no Wizard, THE Wizard SHALL exibir o Estágio 1 da Campanha com suas seções Entrada, Processamento e Saída.
2. WHEN o Estágio atual exibido é o Estágio N com N menor que 3 e o Estágio N está `concluido`, THE Wizard SHALL habilitar o controle "Avançar para o próximo estágio".
3. WHEN o Pesquisador aciona "Avançar para o próximo estágio" a partir do Estágio N, THE Wizard SHALL exibir o Estágio N+1 da mesma Campanha.
4. WHILE o Estágio atual exibido é o Estágio N com N maior que 1, THE Wizard SHALL habilitar o controle "Voltar".
5. WHEN o Pesquisador aciona "Voltar" a partir do Estágio N, THE Wizard SHALL exibir o Estágio N-1 da mesma Campanha sem alterar o Estado_de_Sessão.
6. IF o Estado_do_Estágio do próximo estágio é `bloqueado`, THEN THE Wizard SHALL manter o controle "Avançar para o próximo estágio" desabilitado.

### Requirement 2 — Estágio 1: modelagem estrutural

**User Story:** Como Pesquisador, quero disparar a modelagem estrutural do Estágio 1 pelo Wizard, para que a conclusão do Estágio 1 libere o avanço ao Estágio 2.

#### Acceptance Criteria

1. WHILE o Estágio 1 da Campanha não está `concluido`, THE Wizard SHALL exibir um controle "Executar modelagem" no Estágio 1.
2. WHEN o Pesquisador aciona "Executar modelagem", THE Wizard SHALL enviar `POST /api/casos/{caso}/estagio/1/executar` e exibir o Estado_do_Estágio retornado.
3. WHEN o `POST /api/casos/{caso}/estagio/1/executar` retorna o Estágio 1 com `estado` igual a `concluido`, THE Wizard SHALL habilitar o avanço para o Estágio 2.
4. IF o `POST /api/casos/{caso}/estagio/1/executar` retorna o Estágio 1 com `estado` igual a `erro`, THEN THE Wizard SHALL exibir a mensagem de erro retornada e manter o avanço para o Estágio 2 desabilitado.

### Requirement 3 — Estágio 2: conclusão da tradução curada

**User Story:** Como Pesquisador, quero que avançar do Estágio 2 marque o Estágio 2 como concluído, para destravar o Estágio 3, já que a tradução é curada e os artefatos já existem.

#### Acceptance Criteria

1. WHEN o Pesquisador aciona "Avançar para o próximo estágio" a partir do Estágio 2 e o Estágio 2 ainda não está `concluido`, THE Wizard SHALL enviar `POST /api/casos/{caso}/estagio/2/concluir`.
2. WHEN o Backend recebe `POST /api/casos/{caso}/estagio/2/concluir` para um caso conhecido, THE Backend SHALL persistir a conclusão do Estágio 2 via `SessionStateService.persist_stage_completion(caso, 2)` e retornar o Estado_do_Estágio resultante.
3. IF o Estágio 1 da Campanha não está `concluido`, THEN THE Backend SHALL recusar `POST /api/casos/{caso}/estagio/2/concluir` com HTTP 409 e não persistir a conclusão do Estágio 2.
4. IF o slug informado em `POST /api/casos/{caso}/estagio/2/concluir` não é uma Campanha conhecida, THEN THE Backend SHALL retornar HTTP 404.
5. WHEN a conclusão do Estágio 2 é persistida com sucesso, THE Wizard SHALL exibir o Estágio 3 da mesma Campanha.
6. IF a persistência da conclusão do Estágio 2 falha, THEN THE Backend SHALL retornar HTTP 500 com mensagem descritiva e preservar o estado anteriormente persistido sem alteração.

### Requirement 4 — Estágio 3: emulação e conclusão

**User Story:** Como Pesquisador, quero disparar a emulação real no Estágio 3 pelo Wizard e ver o Estágio 3 ser concluído ao fim, para encerrar a demonstração com os resultados por Ability visíveis.

#### Acceptance Criteria

1. WHILE o Estágio 3 da Campanha está `concluido` igual a falso e o Estágio 2 está `concluido`, THE Wizard SHALL exibir um controle "Executar emulação" (Finish) no Estágio 3.
2. WHEN o Pesquisador aciona "Executar emulação", THE Wizard SHALL abrir o modal de confirmação de emulação existente para a Campanha.
3. WHEN o Pesquisador confirma a emulação no modal, THE Wizard SHALL enviar `POST /api/casos/{caso}/emulacao` com `confirmado` igual a verdadeiro.
4. WHEN o `POST /api/casos/{caso}/emulacao` conclui com `estado` igual a `concluida`, THE Backend SHALL persistir a conclusão do Estágio 3 via `SessionStateService.persist_stage_completion(caso, 3)`.
5. IF o `POST /api/casos/{caso}/emulacao` termina sem `estado` igual a `concluida`, THEN THE Backend SHALL não persistir a conclusão do Estágio 3.
6. WHEN a emulação conclui com sucesso, THE Wizard SHALL exibir os resultados por Ability na seção Saída do Estágio 3.

### Requirement 5 — Progresso e badges em tempo real a partir da fonte da verdade

**User Story:** Como Pesquisador, quero que o progresso e os badges reflitam o avanço real lido do backend/SQLite, para que a tela nunca mostre um estado divergente do persistido.

#### Acceptance Criteria

1. WHEN o Wizard exibe um Estágio, THE Wizard SHALL derivar o Estado_do_Estágio exibido a partir dos dados retornados pelo Backend.
2. WHEN uma conclusão de estágio é persistida pelo Backend, THE Wizard SHALL atualizar o badge do estágio correspondente para `concluido` após reler o estado do Backend.
3. WHERE o Estágio N-1 de uma Campanha não está `concluido`, THE Wizard SHALL exibir o badge do Estágio N como `bloqueado`.
4. THE Wizard SHALL exibir o progresso agregado da Campanha com base na quantidade de estágios `concluido` reportada pelo Backend.

### Requirement 6 — Reset da demonstração

**User Story:** Como Pesquisador, quero reiniciar o estado de uma campanha, para reexecutar a demonstração do zero.

#### Acceptance Criteria

1. WHEN o Backend recebe `POST /api/casos/{caso}/reset` para um caso conhecido, THE Backend SHALL remover os estágios concluídos e os resultados de operação persistidos da Campanha e retornar o Estado_do_Estágio resultante com todos os estágios em `nao_iniciado` ou `bloqueado`.
2. IF o slug informado em `POST /api/casos/{caso}/reset` não é uma Campanha conhecida, THEN THE Backend SHALL retornar HTTP 404.
3. WHEN o reset de uma Campanha conclui com sucesso, THE Wizard SHALL exibir o Estágio 1 da Campanha com os demais estágios bloqueados.

### Requirement 7 — Preservação da suíte de testes

**User Story:** Como mantenedor, quero que a feature não quebre os testes existentes e adicione cobertura para os novos pontos, para manter a confiança na suíte.

#### Acceptance Criteria

1. THE suíte de testes do Backend SHALL continuar passando integralmente após a adição dos novos endpoints.
2. THE suíte de testes do Frontend SHALL continuar passando integralmente após a adição do Wizard.
3. THE suíte de testes SHALL incluir cobertura para o Endpoint_Conclusao_Estagio2, para a persistência da conclusão do Estágio 3 pós-emulação e para o fluxo de navegação do Wizard.
