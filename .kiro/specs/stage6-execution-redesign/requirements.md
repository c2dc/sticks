# Requirements Document

## Introduction

Esta spec redesenha o **Estágio 6 — Execução, medição e relatório** do pipeline de seis
Estágios do STICKS e corrige um **bug de coleta** da execução, no qual o backend marca a
Operação como finalizada antes de o Caldera concluir todos os comandos, congelando o relatório
num estado parcial e inconsistente.

O redesenho tem três objetivos de produto, levantados em uso real com a campanha curada
**ShadowRay** (base ATT&CK v18.0):

1. **Execução automática e sem pop-up.** Ao entrar no Estágio 6, a execução da campanha deve
   iniciar **automaticamente**, sem o Pesquisador clicar em "provisionar", "solicitar emulação"
   ou "executar". Nenhuma janela/modal deve ser aberta para acompanhar a execução — todo o
   acompanhamento acontece **embutido na própria página do Estágio 6**.
2. **Acompanhamento técnica por técnica, ao vivo.** O Pesquisador precisa ver cada Ability
   (técnica) sendo executada em tempo real, com estado/progresso (pendente → em execução →
   sucesso/falha), o **comando concreto** executado, o **alvo** (contêiner + IP), **quem executa**
   o ataque (host atacante) e os **logs/saída** daquela técnica — tudo na mesma tela, sem
   duplicação de informação.
3. **Separação de responsabilidades entre Estágios 5 e 6.** Toda verificação de **ambiente**
   (provisionamento, saúde dos contêineres, isolamento, prontidão dos pré-requisitos) pertence ao
   **Estágio 5**. Quando o fluxo chega ao Estágio 6, o ambiente já está pronto e verificado; o
   Estágio 6 cuida **apenas** da execução propriamente dita, da medição e do relatório.

### Problema de coleta a corrigir (contexto técnico observado)

Numa execução real do ShadowRay, a Operação no Caldera progrediu até `state=finished` com
`chain=11` (todas as 11 Abilities executadas), mas o backend persistiu a Operação como
`FINISHED` cedo, com apenas 6 resultados coletados. O relatório resultante ficou inconsistente:
veredito "Falha", "0 nós falharam", porém com técnicas eternamente "em execução"/"pendente". O
`OperationRunner` chama `poll_until_complete` uma única vez e depois itera `state_result.links`;
quando o Caldera demora para avançar de uma leitura à seguinte, a coleta não acompanha até o
estado terminal real. Observou-se também **agentes órfãos** no Caldera (dois agentes no host
`kali`, um com `alive=false`), compatível com a hipótese de **imagens/contêineres reconstruídos**
cujos agentes antigos ficaram registrados — o que pode atrasar beacons e confundir o alvo da
Operação.

### Escopo

- Estágio 6 do frontend (`apps/frontend/src/components/*` do Estágio 6) e seu contrato REST/WS.
- Backend do Estágio 6: `OperationRunner`, `CalderaClient` (polling/estado), `ExecutionStageService`,
  medição e relatório, e o endpoint/canal de progresso.
- Fronteira Estágio 5 ↔ Estágio 6: mover verificações de ambiente para o Estágio 5.
- Investigação e correção dos agentes órfãos / estado desatualizado do Caldera relacionado às
  imagens.

### Não-objetivos

- Reescrever o provisionamento do Estágio 5 (já coberto por `estagio5-provisionamento-ambiente`);
  aqui apenas garantimos que a verificação de ambiente **mora** no Estágio 5.
- Implementar o agente de IA (`gerado_ia`); apenas preservar o ponto de extensão por origem.
- Retentativa automática de execução (continua sendo não-objetivo; execução é única).
- Alterar a semântica de contenção/isolamento de segurança (apenas onde a verificação é
  apresentada, não a regra em si).

## Glossary

- **Ability (técnica):** unidade de ataque executável no Caldera, correspondente a uma técnica
  ATT&CK da campanha (ex.: T1190). Carrega comando, executor e alvo.
- **Operation (Operação):** execução orquestrada no Caldera de um conjunto de Abilities (um
  Adversary) contra os alvos; produz uma cadeia (`chain`) de links.
- **Link:** execução de um comando de uma Ability em um agente, dentro de uma Operation.
- **Agent (agente):** processo do Caldera rodando no host atacante (kali) que faz beacon e executa
  os comandos; pode estar `alive=true` (ativo) ou órfão/morto.
- **Alvo (target):** contêiner-vítima que a campanha ataca (ex.: nginx, db), com nome de contêiner
  e IP internos.
- **Host atacante:** host que origina o ataque (ex.: caldera/kali).
- **Estado terminal:** estado final de uma Operation no Caldera (`finished`, `out_of_time` ou
  `cleanup`) que sinaliza o fim da coleta.
- **Pré-requisitos de execução:** condições verificadas no Estágio 5 (contêineres saudáveis,
  Abilities carregadas, isolamento aprovado, agente ativo) necessárias para liberar o Estágio 6.
- **Gate de isolamento:** verificação fail-closed que confirma que todos os contêineres-alvo estão
  em redes `internal: true`, sem rota ao host ou à internet, antes de permitir qualquer comando de
  ataque.
- **Canal de progresso:** canal ao vivo (WebSocket/stream) pelo qual o backend do Estágio 6
  publica mudanças de estado por Ability para a página do Estágio 6.
- **Estágio 5 (Estagio_5):** etapa do pipeline STICKS responsável por provisionar e verificar o
  ambiente de emulação.
- **Estágio 6 (Estagio_6):** etapa do pipeline STICKS responsável pela execução da campanha,
  medição e relatório.

## Requirements

### Requirement 1: Execução automática ao entrar no Estágio 6

**User Story:** Como Pesquisador, quero que a execução da campanha comece automaticamente ao entrar no Estágio 6, para não precisar acionar provisionamento ou execução manualmente.

#### Acceptance Criteria

1. WHEN o Pesquisador entra no Estágio 6 com o Estágio 5 concluído E o gate de isolamento do Requisito 6 retorna aprovado, THE sistema SHALL iniciar a execução da campanha automaticamente em até 3 segundos após a renderização da página, sem exigir clique em qualquer controle de "provisionar", "solicitar emulação" ou "executar".
2. IF o gate de isolamento do Requisito 6 não retorna aprovado, THEN THE sistema SHALL NOT iniciar a execução e SHALL exibir, embutida na página do Estágio 6, uma indicação de que o isolamento não foi validado.
3. WHEN o Estágio 6 inicia a execução, THE sistema SHALL NOT abrir qualquer janela, modal ou pop-up, e SHALL exibir todo o acompanhamento da execução embutido na página do Estágio 6.
4. IF o Estágio 5 não está concluído (ambiente não pronto) quando o Pesquisador chega ao Estágio 6, THEN THE sistema SHALL manter o Estágio 6 em estado bloqueado, SHALL NOT iniciar execução, e SHALL redirecionar o Pesquisador para o Estágio 5 em até 3 segundos, exibindo uma indicação do motivo do bloqueio.
5. WHILE existe um resultado de execução persistido para a campanha, THE sistema SHALL, ao reabrir o Estágio 6, reexibir esse resultado persistido e SHALL NOT reexecutar a campanha automaticamente (idempotência).
6. WHERE uma confirmação de segurança é exigida por política para execução de ataque real, THE sistema SHALL apresentar essa confirmação embutida na página do Estágio 6, SHALL NOT utilizar pop-up, e SHALL NOT disparar qualquer comando de execução até que o Pesquisador registre consentimento explícito nessa confirmação.

### Requirement 2: Acompanhamento técnica por técnica ao vivo

**User Story:** Como Pesquisador, quero acompanhar cada técnica (Ability) sendo executada ao vivo na mesma tela, para entender o andamento e o resultado de cada passo da campanha.

#### Acceptance Criteria

1. WHILE a execução da campanha está em andamento THE sistema SHALL exibir, para cada Ability, exatamente um estado visível dentre os quatro estados mutuamente exclusivos: `pendente`, `em execução`, `sucesso` e `falha`.
2. WHILE uma Ability está no estado `em execução` THE sistema SHALL exibir um indicador de atividade (ex.: rótulo "em execução"/carregando) associado àquela Ability específica, e SHALL remover esse indicador quando a Ability transitar para `sucesso` ou `falha`.
3. WHEN uma Ability muda de estado no backend THEN o sistema SHALL atualizar o estado dessa Ability na tela em até 2 segundos após a publicação da mudança no canal de progresso, sem recarregar a página.
4. WHEN o Pesquisador visualiza uma Ability THEN o sistema SHALL exibir, para aquela Ability, os seguintes dados: o comando concreto executado, o alvo (nome do contêiner e IP), o host atacante que executa, e os logs/saída (stdout/stderr) coletados.
5. IF um dos dados de uma Ability (comando, alvo, host atacante ou logs/saída) ainda não foi coletado ou está vazio THEN o sistema SHALL exibir um indicador explícito de "não disponível" para aquele dado, SHALL NOT exibir campo em branco sem rótulo, e SHALL preencher o dado quando ele for recebido pelo canal de progresso em até 2 segundos.
6. THE sistema SHALL apresentar as Abilities na ordem de execução (atomic ordering) definida pela campanha, da primeira à última, sem reordenar enquanto a execução estiver em andamento.
7. THE sistema SHALL NOT duplicar a mesma informação de alvo/estado de uma Ability em múltiplos blocos da tela; cada dado de uma Ability SHALL aparecer uma única vez de forma canônica.
8. IF o canal de progresso é interrompido durante a execução (ex.: WebSocket cai) THEN o sistema SHALL exibir um indicador de conexão perdida ao Pesquisador e SHALL retomar a atualização dos estados por Ability ao restabelecer o canal, sem recarregar a página.

### Requirement 3: Coleta fiel até o estado terminal (correção do bug)

**User Story:** Como Pesquisador, quero que o relatório reflita o resultado real e completo da execução, para que métricas e veredito sejam confiáveis.

#### Acceptance Criteria

1. WHEN o backend conduz uma Operação THEN o sistema SHALL reconsultar o estado por Ability no Caldera em intervalos configuráveis entre 1 e 10 segundos (padrão 2 segundos) e SHALL continuar reconsultando até a Operação atingir um estado terminal real no Caldera (`finished`, `out_of_time` ou `cleanup`) ou até decorrer o timeout de coleta configurável contado a partir do início da Operação.
2. IF a Operação atinge o timeout de coleta sem alcançar um estado terminal THEN o sistema SHALL marcar a Operação com o estado `INCOMPLETE_TIMEOUT`, SHALL preservar os resultados parciais já coletados sem descartá-los, e SHALL NOT contabilizar a Operação entre as execuções finalizadas com sucesso.
3. WHEN a Operação atinge um estado terminal THEN o número de resultados por Ability persistidos SHALL ser igual ao número de links/comandos em estado terminal no Caldera para aquela Operação, sem permanecer numa contagem inferior à observada no Caldera.
4. WHEN um relatório é apresentado como final THEN o sistema SHALL garantir que: (a) o veredito seja derivado deterministicamente da taxa de sucesso calculada; (b) a soma das técnicas em cada estado (sucesso, falha) seja igual ao total de técnicas da campanha; (c) a contagem de nós que falharam seja igual ao número de técnicas em estado de falha; e (d) nenhuma técnica permaneça em estado não-terminal (pendente ou em execução).
5. IF existir estado persistido de uma execução anterior cujo identificador de execução ou esquema não corresponde à execução atual THEN o sistema SHALL descartar e recomputar esse estado antes de apresentar o relatório, e SHALL NOT apresentar um relatório que combine dados de execuções distintas ou com invariantes do critério 4 violadas.

### Requirement 4: Agentes e ambiente do Caldera saudáveis (imagens atualizadas)

**User Story:** Como Pesquisador, quero que a execução use agentes e apontamentos atuais do Caldera, para que a campanha não trave por causa de agentes órfãos de imagens antigas.

#### Acceptance Criteria

1. WHEN o Estágio 5 prepara o ambiente, THE sistema SHALL consultar a API do Caldera e verificar que existe pelo menos 1 agente com `alive=true` associado ao grupo esperado antes de liberar o avanço para o Estágio 6.
2. IF a verificação de agentes do grupo esperado não retornar nenhum agente com `alive=true` dentro de 30 segundos, THEN THE sistema SHALL bloquear o avanço para o Estágio 6 e SHALL exibir na tela do Estágio 5 uma mensagem indicando ausência de agente ativo.
3. IF existe pelo menos 1 agente registrado no Caldera com `alive=false` (agente órfão/morto) no grupo esperado, THEN THE sistema SHALL registrar essa condição na tela do Estágio 5 e SHALL impedir o início da Operação enquanto não houver pelo menos 1 agente com `alive=true`, de modo a evitar travamento sem indicação visível ao usuário.
4. THE sistema SHALL usar os endpoints/rotas da versão atual da API do Caldera em execução e os nomes de contêiner/host atuais do ambiente em execução, sem referenciar apontamentos de imagens antigas que não correspondam ao ambiente em execução.
5. IF algum endpoint/rota ou nome de contêiner/host configurado não corresponder ao ambiente em execução (resposta de erro da API ou host não resolvível), THEN THE sistema SHALL bloquear o avanço para o Estágio 6 e SHALL exibir na tela do Estágio 5 uma mensagem indicando a divergência de apontamento.
6. WHERE a condição de ambiente saudável não puder ser garantida, THE sistema SHALL reportar a causa específica (ausência de agente ativo, agente órfão, ou apontamento divergente) na tela do Estágio 5 (não no Estágio 6) e SHALL bloquear o avanço para o Estágio 6.

### Requirement 5: Verificações de ambiente pertencem ao Estágio 5

**User Story:** Como Pesquisador, quero que todas as verificações de ambiente aconteçam no Estágio 5, para que ao chegar ao Estágio 6 o ambiente já esteja pronto e o 6 cuide só da execução.

#### Acceptance Criteria

1. WHEN o Estágio 5 prepara o ambiente THEN o sistema SHALL executar, exclusivamente no Estágio 5, o conjunto completo de verificações de ambiente — provisionamento, saúde dos contêineres-alvo, isolamento dos alvos e prontidão dos pré-requisitos de execução (conforme o Glossário) — não deixando nenhuma dessas verificações para o Estágio 6.
2. WHEN o fluxo chega ao Estágio 6 THEN o sistema SHALL assumir o ambiente pronto e verificado, SHALL NOT reexecutar nenhuma das verificações de ambiente do critério 1 no Estágio 6, e SHALL restringir o Estágio 6 à execução, medição e relatório.
3. IF uma verificação de ambiente falha THEN o Estágio 5 SHALL bloquear o avanço ao Estágio 6, SHALL exibir a causa da falha na tela do Estágio 5 (não na do Estágio 6) e SHALL manter o fluxo no Estágio 5 (fail-closed), de modo que o Estágio 6 nunca seja alcançado com o ambiente não pronto.
4. THE painel do Estágio 6 SHALL NOT apresentar controles acionáveis nem estados de provisionamento ou de verificação de ambiente como responsabilidade do Estágio 6; WHERE o painel do Estágio 6 referenciar o resultado do Estágio 5 THE sistema SHALL exibi-lo de forma somente leitura (sem ações de provisionar/verificar), sem reexecutar nem duplicar a verificação.

### Requirement 6: Gate de isolamento obrigatório antes de qualquer ataque (fail-closed)

**User Story:** Como Pesquisador, quero que nenhum comando de ataque seja executado se o ambiente de emulação não estiver comprovadamente isolado, para nunca correr o risco de comprometer máquinas reais da rede.

#### Acceptance Criteria

1. WHEN a execução do Estágio 6 vai iniciar (inclusive no início automático) THEN o sistema SHALL verificar que todos os contêineres-alvo estão em redes `internal: true`, sem rota ao host ou à internet, e SHALL concluir essa verificação ANTES de disparar qualquer comando; o resultado "isolado" SHALL exigir que todos os alvos satisfaçam a regra.
2. IF o ambiente não está isolado OR o isolamento não pôde ser verificado (ex.: daemon Docker indisponível) THEN o sistema SHALL interromper o fluxo antes de disparar qualquer comando, SHALL NOT executar nenhum comando de ataque, e SHALL exibir embutida na página do Estágio 6 a causa identificando qual contêiner falhou ou que a verificação não pôde ser concluída.
3. WHILE a execução está em andamento THE sistema SHALL reconsultar a condição de isolamento em intervalos regulares de no máximo 30 segundos.
4. IF, durante a execução, o isolamento deixar de ser satisfeito THEN o sistema SHALL interromper a execução sem disparar novos comandos, SHALL preservar os resultados por Ability já coletados, e SHALL sinalizar a quebra de contenção identificando o contêiner afetado.
5. THE gate de isolamento SHALL ser inegociável: a execução automática do Requisito 1 e a confirmação de conveniência do Requisito 1.6 SHALL NOT contornar nem desabilitar esta verificação, e nenhum comando SHALL ser disparado enquanto o resultado do gate não for "isolado".
6. THE verificação de isolamento do Estágio 6 SHALL usar o mesmo conjunto de contêineres-alvo e a mesma regra da verificação do Estágio 5 (Requisito 5), de forma que, para o mesmo estado de ambiente, ambas produzam o resultado "isolado" de forma equivalente.

## Questões em aberto

- A confirmação de segurança para disparar o ataque real deve ser (a) um passo embutido único no
  Estágio 6 antes do start automático, ou (b) totalmente automática assumindo que o Estágio 5 já
  validou contenção/isolamento? (Impacta o Requisito 1.5.)
- Para o timeout de coleta: qual valor-alvo numérico (ex.: segundos) adotar como padrão
  configurável? O rótulo de estado foi fixado como `INCOMPLETE_TIMEOUT` no Requisito 3.2. (Impacta
  o Requisito 3.1/3.2.)
- Limpeza de agentes órfãos: o sistema deve apenas sinalizar, ou também desregistrar agentes mortos
  no Estágio 5? (Impacta o Requisito 4.2.)