# Requirements Document

## Introduction

No fluxo guiado do wizard (`ExecutionWizard`), ao concluir o Estágio 1 (Modelagem
Estrutural) de um caso **curado**, o Estágio 2 (Geração do DAG) aparece
imediatamente como "concluído" na trilha de badges — antes de o pesquisador
navegar até a janela do Estágio 2. Isso ocorre porque o backend **auto-conclui**
o Estágio 2 para casos curados assim que o Estágio 1 conclui (o DAG curado já
existe em disco). O comportamento é funcionalmente correto, mas gera estranheza
na experiência: o pesquisador espera ver cada estágio sendo marcado como
concluído **à medida que avança** pelas janelas, não um estágio adiante já verde.

Esta especificação define requisitos para **melhorar a UX de progresso/revelação
dos estágios** no wizard, preservando duas invariantes inegociáveis: (1) o
backend continua sendo a fonte da verdade sobre a conclusão real dos estágios; e
(2) o gate sequencial de avanço (um estágio N>1 só é alcançável quando N−1 está
concluído) permanece intacto.

Esta spec trata **apenas da camada de apresentação de progresso no wizard**. Ela
não altera a lógica de auto-conclusão do backend, a geração/escolha de DAG, nem o
contrato dos endpoints de estado.

**Ponto em aberto deliberado (a resolver na fase de Design):** a escolha do
mecanismo entre três opções discutidas — (A) revelação gradual apenas na
apresentação; (B) etapa explícita de "revisar/confirmar o DAG curado"; (C)
manter o estado real e comunicar visualmente a distinção entre "pré-concluído
(curado)" e "concluído por você". O Requisito 7 mantém essa decisão em aberto, de
modo que os requisitos descrevem o **comportamento desejado** sem travar
prematuramente a implementação.

## Glossary

- **Wizard**: O componente de fluxo guiado `ExecutionWizard`
  (`ui/frontend/src/components/ExecutionWizard.tsx`), que conduz o pesquisador
  pelos cinco estágios de uma campanha.
- **Pesquisador**: Usuário que opera o Wizard para replicar/emular uma campanha.
- **Estágio**: Uma das cinco etapas do pipeline (1 Modelagem Estrutural, 2
  Geração do DAG, 3 Operacionalização, 4 Provisionamento do Ambiente, 5 Execução
  da Campanha).
- **Backend**: O serviço de aplicação que mantém e reporta o estado por estágio
  de cada caso; fonte da verdade sobre conclusão real.
- **Estado_Real**: O estado por estágio reportado pelo Backend (`concluido`,
  `nao_iniciado`, `em_andamento`, `erro`, `bloqueado`) para um caso, obtido via
  `GET /api/casos/{caso}`.
- **Badge_De_Progresso**: O indicador visual de estado de um estágio na trilha de
  badges exibida no cabeçalho do Wizard (renderizado por `StageStateBadge`).
- **Estado_Apresentado**: O estado que o Wizard exibe na Badge_De_Progresso de um
  estágio, que pode diferir temporariamente do Estado_Real conforme a opção de
  design escolhida.
- **Caso_Curado**: Um caso cujo DAG já existe curado em disco, para o qual o
  Backend auto-conclui o Estágio 2 assim que o Estágio 1 conclui
  (`DagService.auto_concluir_estagio2`).
- **Caso_Nao_Curado**: Um caso (ex.: campanha STIX não curada) para o qual o
  Backend **não** auto-conclui o Estágio 2 (`sem_dag=True`).
- **Auto_Conclusao_Estagio2**: O comportamento do Backend que marca o Estágio 2
  como `concluido` para um Caso_Curado assim que o Estágio 1 conclui, de forma
  idempotente e respeitando o gate sequencial.
- **Gate_Sequencial**: A regra que impede alcançar/avançar para um estágio N>1
  enquanto o estágio N−1 não estiver concluído no Estado_Real
  (`derivarEstados` em `ui/frontend/src/lib/wizard.ts`).
- **Pre_Concluido**: Um estágio cujo Estado_Real é `concluido` por efeito da
  Auto_Conclusao_Estagio2 (concluído pelo sistema), em contraste com um estágio
  concluído por ação direta do Pesquisador.
- **Namespace_I18n**: O namespace `pipeline` de traduções, com arquivos pareados
  `pt-BR/pipeline.json` e `en/pipeline.json`.

## Requirements

### Requirement 1: Progresso coerente ao avançar pelos estágios

**User Story:** Como pesquisador, quero perceber o progresso dos estágios de
forma coerente conforme avanço pelas janelas do Wizard, para que a trilha de
badges reflita minha jornada passo a passo em vez de mostrar um estágio adiante
já concluído.

#### Acceptance Criteria

1. WHEN o Pesquisador conclui o Estágio 1 de um Caso_Curado e permanece na janela
   do Estágio 1, THE Wizard SHALL apresentar o Estágio 2 na Badge_De_Progresso
   em um Estado_Apresentado que não seja "concluído".
2. WHEN o Pesquisador navega até a janela do Estágio 2 de um Caso_Curado cujo
   Estágio 2 está Pre_Concluido, THE Wizard SHALL apresentar o Estágio 2 na
   Badge_De_Progresso com o Estado_Apresentado "concluído".
3. WHILE o Pesquisador avança sequencialmente pelas janelas 1 a 5, THE Wizard
   SHALL apresentar cada Badge_De_Progresso em ordem não decrescente de
   progresso, de modo que nenhum estágio ainda não visitado seja apresentado como
   "concluído" antes de um estágio anterior já visitado.
4. THE Wizard SHALL derivar o Estado_Apresentado de cada estágio a partir do
   Estado_Real reportado pelo Backend combinado com o avanço do Pesquisador pelas
   janelas, sem manter uma fonte de verdade concorrente sobre a conclusão real.

### Requirement 2: Comunicação clara de estágio pré-concluído

**User Story:** Como pesquisador, quando um estágio é pré-concluído
automaticamente pelo sistema (caso curado), quero que isso seja comunicado de
forma clara e não confusa, para que eu entenda que o estágio já estava pronto por
curadoria e não por uma ação minha.

#### Acceptance Criteria

1. WHEN o Pesquisador visualiza a Badge_De_Progresso de um estágio Pre_Concluido,
   THE Wizard SHALL comunicar que o estágio foi concluído automaticamente pelo
   sistema de forma distinguível de um estágio concluído por ação do Pesquisador.
2. WHEN o Pesquisador abre a janela de um estágio Pre_Concluido, THE Wizard SHALL
   exibir uma mensagem explicativa indicando que o resultado já existia por
   curadoria.
3. THE Wizard SHALL manter a distinção entre Pre_Concluido e concluído por ação
   do Pesquisador apenas na camada de apresentação, sem alterar o Estado_Real
   reportado pelo Backend.

### Requirement 3: Preservação do gate sequencial e da corretude do backend

**User Story:** Como mantenedor do sistema, quero que a melhoria de UX de
progresso preserve o gate sequencial e a corretude do backend, para que o
comportamento funcional e as invariantes existentes permaneçam intactos.

#### Acceptance Criteria

1. THE Wizard SHALL tratar o Estado_Real reportado pelo Backend como a única
   fonte da verdade sobre a conclusão real de cada estágio.
2. WHILE o estágio N−1 não está `concluido` no Estado_Real, THE Wizard SHALL
   impedir o avanço para o estágio N, para todo N no intervalo de 2 a 5.
3. THE Wizard SHALL preservar o resultado de `derivarEstados` para a habilitação
   do avanço e para o bloqueio sequencial, mantendo os invariantes verificados em
   `wizard.test.ts`.
4. IF a melhoria de apresentação de progresso introduz divergência temporária
   entre o Estado_Apresentado e o Estado_Real de um estágio, THEN THE Wizard
   SHALL manter a habilitação do botão de avanço governada pelo Estado_Real, e
   não pelo Estado_Apresentado.

### Requirement 4: Comportamento preservado para casos não curados

**User Story:** Como pesquisador trabalhando com uma campanha não curada, quero
que o comportamento do Estágio 2 permaneça correto, para que a melhoria de UX de
progresso não afete casos em que o Estágio 2 não é auto-concluído.

#### Acceptance Criteria

1. WHEN o Pesquisador conclui o Estágio 1 de um Caso_Nao_Curado, THE Wizard SHALL
   apresentar o Estágio 2 na Badge_De_Progresso em um Estado_Apresentado que não
   seja "concluído".
2. WHILE o Estágio 2 de um Caso_Nao_Curado não está `concluido` no Estado_Real,
   THE Wizard SHALL impedir o avanço para o Estágio 3.
3. THE Wizard SHALL aplicar o tratamento de comunicação de Pre_Concluido somente
   a estágios cujo Estado_Real é `concluido` por efeito da
   Auto_Conclusao_Estagio2.

### Requirement 5: Paridade de internacionalização para rótulos novos

**User Story:** Como pesquisador que usa a interface em português ou em inglês,
quero que quaisquer rótulos novos de progresso estejam disponíveis nos dois
idiomas, para que a experiência seja consistente independentemente do idioma
selecionado.

#### Acceptance Criteria

1. WHERE a melhoria introduz um rótulo, mensagem ou texto de badge novo, THE
   Wizard SHALL obter esse texto do Namespace_I18n `pipeline`.
2. THE Namespace_I18n SHALL conter, para cada chave de texto nova introduzida por
   esta melhoria, uma entrada correspondente em `pt-BR/pipeline.json` e uma
   entrada correspondente em `en/pipeline.json`.
3. THE Wizard SHALL não exibir texto de progresso embutido em código (hardcoded)
   fora do Namespace_I18n.

### Requirement 6: Consistência da apresentação após releitura e reinício

**User Story:** Como pesquisador, quero que a apresentação de progresso
permaneça consistente quando o Wizard relê o estado do backend, troca de caso ou
reinicia a campanha, para que a trilha de badges nunca fique em um estado
enganoso.

#### Acceptance Criteria

1. WHEN o Wizard relê o Estado_Real do Backend após uma transição de estágio, THE
   Wizard SHALL recomputar o Estado_Apresentado de cada estágio a partir do
   Estado_Real relido e do avanço atual do Pesquisador.
2. WHEN o Pesquisador troca o caso selecionado, THE Wizard SHALL reiniciar a
   apresentação de progresso para o Estágio 1 e recomputar o Estado_Apresentado a
   partir do Estado_Real do novo caso.
3. WHEN o Pesquisador reinicia a campanha, THE Wizard SHALL reiniciar a
   apresentação de progresso de modo consistente com o Estado_Real após o
   reinício.

### Requirement 7: Decisão em aberto sobre o mecanismo de apresentação

**User Story:** Como responsável pelo produto, quero que a escolha entre
apresentação-apenas e uma etapa explícita de confirmação seja decidida na fase de
Design, para que os requisitos descrevam o comportamento desejado sem travar
prematuramente a implementação.

#### Acceptance Criteria

1. THE fase de Design SHALL resolver, junto ao usuário, a escolha entre as
   opções (A) revelação gradual apenas na apresentação, (B) etapa explícita de
   revisão/confirmação do DAG curado, e (C) manter o Estado_Real e comunicar
   visualmente a distinção entre Pre_Concluido e concluído por ação do
   Pesquisador.
2. WHERE a opção escolhida for a (B) etapa explícita de confirmação, THE Wizard
   SHALL conduzir o Pesquisador a confirmar o DAG curado na janela do Estágio 2
   antes de apresentar o Estágio 2 como concluído, sem contornar o
   Gate_Sequencial nem alterar o Estado_Real.
3. WHERE a opção escolhida for a (A) revelação gradual, THE Wizard SHALL manter a
   divergência entre Estado_Apresentado e Estado_Real limitada à camada de
   apresentação e governar o avanço pelo Estado_Real.
4. WHERE a opção escolhida for a (C) comunicação visual da distinção, THE Wizard
   SHALL preservar o Estado_Real como concluído e sinalizar a origem
   (Pre_Concluido) na Badge_De_Progresso.
