# Requirements Document

## Introduction

Esta spec detalha o **Estágio 5 — Criação do ambiente de simulação (Provisionamento do
ambiente)** do pipeline de seis Estágios de STICKS. Ela **refina e aprofunda** o papel do
Estágio 5 já definido em `pipeline-seis-estagios` (requirements/design), transformando-o de
uma verificação **somente-leitura** da topologia e do isolamento (o que o código atual faz em
`ui/backend/app/services/environment/provisionador.py` e na tela
`ui/frontend/src/components/EnvironmentStageView.tsx`) em um **provisionamento ativo e
automático** do ambiente contido, com **feedback de execução ao vivo** e **verificação de
pré-requisitos** antes de liberar o Estágio 6.

### Problema que esta spec resolve

O Estágio 5 precisa, de fato, **subir o ambiente de emulação** (não apenas inspecioná-lo). Hoje
o laboratório é o `docker/docker-compose.yml` curado pelo colaborador. Neste primeiro momento o
sistema sobe esse ambiente **automaticamente** quando o Estágio 5 inicia — sem o Pesquisador ter
de clicar em "provisionar". No futuro, um **agente de IA** criará a topologia sob medida (Docker
Compose para contêineres, ou Vagrant para máquinas virtuais) e fará o provisionamento; o desenho
desta spec deve acomodar essa substituição **sem redesenho**, por meio do ponto de extensão por
`origem` já existente (`ProvisionadorDeAmbiente`, `curadoria_humana` hoje, `gerado_ia` no futuro).

Como provisionar é uma fase **demorada**, o Pesquisador precisa de **retorno contínuo** do que
está acontecendo: um **terminal/console na tela** que exibe os comandos sendo executados e sua
saída (stdout/stderr) ao vivo. Ao final, o Pesquisador precisa de uma **confirmação explícita** de
que o ambiente está pronto: contêineres no ar (e, no futuro, máquinas virtuais no ar), Abilities
carregadas no Caldera, isolamento verificado — todos os pré-requisitos de execução **preenchidos e
verificados**. Se algo falhar, o sistema precisa **mostrar na tela o que falhou** e **bloquear** o
avanço para o Estágio 6. Ao término da campanha (Estágio 6), o ambiente é **desprovisionado**,
liberando o laboratório para a próxima campanha.

Esta é uma **solução de transição**. Para a campanha curada **ShadowRay** (primeiro caso a rodar
ponta a ponta), o Estágio 5 usa o `docker-compose.yml` que o colaborador já produziu. Se cada
campanha deve ou não ter o seu próprio arquivo de composição — e se um mesmo compose pode servir a
múltiplas campanhas — é uma **questão em aberto** registrada nesta spec (ver Questões em aberto),
não resolvida aqui: no primeiro momento há **um único** laboratório curado e **uma** campanha
(ShadowRay) executada por vez.

### Escopo e não-objetivos

- **No escopo:** provisionamento automático do ambiente curado (ShadowRay) via o
  `docker-compose.yml` existente; terminal/console de logs ao vivo na tela do Estágio 5;
  verificação de pré-requisitos (contêineres saudáveis + Abilities carregadas no Caldera +
  isolamento aprovado) como pré-condição de conclusão; mensagem de falha na tela que bloqueia o
  avanço; saída tipada do Estágio 5 com os parâmetros do ambiente que o Estágio 6 consome;
  desprovisionamento ao término da campanha.
- **Fora de escopo (não-objetivo):** a implementação do **agente de IA** que cria a topologia sob
  medida; o suporte a **máquinas virtuais / Vagrant** (admitido no desenho via `PlataformaAmbiente`,
  mas **não implementado** aqui); a **otimização** de reaproveitar um provisionamento para executar
  múltiplas campanhas (registrada como questão em aberto; o primeiro momento provisiona e
  desprovisiona por campanha); a reescrita da contenção/isolamento já existente em
  `ui/backend/app/services/containment/` (é **reusada e preservada**).

### Plataforma e stack

A aplicação vive em `ui/`: `ui/backend` (FastAPI + SQLAlchemy + Alembic + **PostgreSQL
exclusivamente**) e `ui/frontend` (React 19 + TypeScript + Vite + i18next pt-BR). O provisionamento
sobe o laboratório Docker descrito em `docker/docker-compose.yml` (projeto Compose `sticks`), cujas
três redes de ataque são `internal: true` (`192.168.10.0/24`, `192.168.20.0/24`,
`192.168.30.0/24`). O MITRE Caldera (contêiner `sticks-caldera-attacker`, publicado em
`127.0.0.1:8888`) executa as Abilities dentro do ambiente contido no Estágio 6. Contratos JSON em
pt-BR são a fonte da verdade; paridade de chaves i18n pt-BR/en é exigida no frontend.

## Glossary

- **Provisionamento do ambiente / Estágio 5**: Ato de **criar e subir** o ambiente de simulação
  contido para uma campanha, carregar as Abilities no Caldera e verificar que todos os
  pré-requisitos de execução estão satisfeitos. É a Saída que o Estágio 6 consome.
- **Provisionamento automático**: O provisionamento inicia **sem** uma ação manual de "provisionar"
  do Pesquisador — ao entrar no Estágio 5, o sistema sobe o ambiente por conta própria.
- **Desprovisionamento / Teardown**: Remoção/parada do ambiente provisionado ao término da campanha
  (Estágio 6), liberando os recursos de hardware para a próxima campanha.
- **Ambiente curado**: O laboratório definido no `docker/docker-compose.yml` do repositório,
  produzido por curadoria humana. Usado pela campanha curada ShadowRay neste primeiro momento.
- **Topologia**: A descrição do ambiente — hosts (contêineres/VMs), redes e sua configuração — que
  o provisionamento sobe. Hoje descrita pelo `docker-compose.yml`; no futuro gerada pela IA (Docker
  Compose para contêineres ou Vagrant para VMs).
- **Terminal de provisionamento / Console de logs**: Área na tela do Estágio 5 que exibe, ao vivo,
  os comandos executados pelo provisionamento e sua saída (stdout/stderr), para o Pesquisador
  acompanhar uma fase demorada.
- **Linha de log**: Uma entrada textual emitida pelo provisionamento (um comando, uma linha de
  stdout/stderr, ou uma marcação de etapa), transmitida ao terminal da tela.
- **Pré-requisito de execução**: Uma condição que precisa estar satisfeita e verificada para o
  Estágio 6 poder executar — contêineres saudáveis (e, no futuro, VMs no ar), Abilities carregadas
  no Caldera, e isolamento aprovado dos contêineres-alvo.
- **Verificação / Homologação do ambiente**: A confirmação de que **todos** os pré-requisitos de
  execução estão preenchidos e verificados. É a condição de conclusão do Estágio 5.
- **Contêiner saudável**: Um contêiner do laboratório que está no estado `running` e, quando o
  serviço define `healthcheck` no compose, com status de saúde `healthy`.
- **Abilities carregadas no Caldera**: As Abilities ordenadas da campanha empurradas para o Caldera
  (`POST /api/v2/abilities`) na ordem do DAG, seguidas do Adversary, prontas para a Operação do
  Estágio 6.
- **Isolamento de contêiner**: Condição em que um contêiner-alvo está anexado **exclusivamente** a
  redes `internal: true`, sem bridge externo e sem DNS externo. Verificada por inspeção read-only do
  Docker (reuso de `containment/isolation`).
- **Saída do Estágio 5 / Parâmetros do ambiente para execução**: O artefato tipado que o Estágio 5
  produz quando conclui — a identificação do ambiente provisionado (plataforma, contêineres-alvo,
  arquivo de composição), o resumo de isolamento, o indicador de Abilities carregadas e os demais
  parâmetros que a Execução do Estágio 6 consome.
- **ProvisionadorDeAmbiente**: A costura (interface/serviço) de extensão por `origem` do Estágio 5,
  já existente em `provisionador.py`. Implementação de **curadoria** agora (sobe/verifica o
  laboratório estático); admite implementação de **IA** encaixável depois, sem redesenho.
- **Origem**: Atributo explícito e extensível que indica **como** o ambiente foi criado:
  `curadoria_humana` (hoje) ou `gerado_ia` (futuro).
- **PlataformaAmbiente**: A plataforma do ambiente — `conteiner` (hoje). `maquina_virtual` é
  admitida no desenho (futuro), não implementada aqui.
- **Pesquisador**: Pessoa usuária da ferramenta STICKS.
- **ShadowRay**: A campanha curada (slug `ShadowRay`) usada como primeiro caso executado ponta a
  ponta nesta entrega.

## Requirements

### Requisito 1: Provisionamento automático ao iniciar o Estágio 5

**História de usuário:** Como Pesquisador, quero que o ambiente de simulação suba automaticamente
quando o Estágio 5 inicia, para que eu não precise acionar manualmente um "provisionar" e o sistema
conduza o provisionamento por conta própria.

#### Critérios de Aceitação

1. WHEN o Estágio 5 de uma campanha é iniciado (o Estágio 4 está `concluido` e o Pesquisador avança
   para o Estágio 5), THE Backend SHALL iniciar o provisionamento do ambiente dessa campanha sem
   exigir uma ação manual adicional de "provisionar".
2. THE Estágio 5 SHALL ter como Entrada as Abilities ordenadas e os parâmetros de ambiente
   produzidos no Estágio 4.
3. WHEN a campanha selecionada é a campanha curada ShadowRay, THE Backend SHALL provisionar o
   ambiente a partir do `docker/docker-compose.yml` curado do repositório.
4. THE Backend SHALL conduzir o provisionamento por meio da costura `ProvisionadorDeAmbiente`,
   registrando a origem como `curadoria_humana` para o ambiente curado.
5. WHILE o agente de IA não existir, THE Backend SHALL NÃO invocar nenhum agente de IA durante o
   provisionamento de uma campanha curada.
6. IF o provisionamento já foi concluído com sucesso para a campanha e o ambiente permanece
   provisionado, THEN THE Backend SHALL reaproveitar o ambiente provisionado sem subir um novo.

### Requisito 2: Terminal de logs ao vivo durante o provisionamento

**História de usuário:** Como Pesquisador, quero ver na tela um terminal com os comandos sendo
executados e sua saída ao vivo durante o provisionamento, para que eu acompanhe uma fase demorada e
saiba se está funcionando ou não.

#### Critérios de Aceitação

1. WHILE o provisionamento do Estágio 5 está em andamento, THE Pipeline_UI SHALL exibir um terminal
   (console de logs) na tela do Estágio 5.
2. WHILE o provisionamento está em andamento, THE Backend SHALL transmitir ao terminal da tela, em
   tempo quase real, as linhas de log do provisionamento — os comandos executados e sua saída
   (stdout e stderr).
3. THE Pipeline_UI SHALL apresentar as linhas de log na ordem em que foram emitidas, preservando a
   sequência dos comandos e de sua saída.
4. THE Pipeline_UI SHALL distinguir visualmente, no terminal, as etapas do provisionamento (por
   exemplo: subir contêineres, aguardar saúde, carregar Abilities, verificar isolamento).
5. WHEN o Pesquisador abre a tela do Estágio 5 enquanto o provisionamento já está em andamento ou já
   terminou, THE Pipeline_UI SHALL exibir as linhas de log já emitidas, sem perder o histórico da
   execução corrente.
6. THE Backend SHALL transmitir as linhas de log por meio do canal de progresso de Estágios já
   existente (WebSocket de Estágios), estendido para carregar as linhas de log do provisionamento.
7. IF a conexão do terminal ao vivo é interrompida e restabelecida, THEN THE Pipeline_UI SHALL
   recuperar o estado atual do provisionamento e as linhas de log já emitidas.

### Requisito 3: Subir os contêineres do ambiente

**História de usuário:** Como Pesquisador, quero que o provisionamento suba os contêineres do
laboratório contido, para que o ambiente da campanha fique disponível para a execução.

#### Critérios de Aceitação

1. WHEN o provisionamento inicia para a campanha curada, THE Backend SHALL subir os contêineres do
   laboratório definidos no `docker/docker-compose.yml` curado.
2. THE Backend SHALL subir **apenas contêineres** neste primeiro momento, usando a plataforma
   `conteiner`.
3. THE Backend SHALL subir o laboratório preservando as três redes internas (`192.168.10.0/24`,
   `192.168.20.0/24`, `192.168.30.0/24`) marcadas `internal: true`.
4. THE Backend SHALL NÃO subir, como parte do ambiente de ataque da campanha, os serviços da camada
   de aplicação da própria Pipeline_UI (frontend/backend/postgres) nem outros serviços alheios ao
   laboratório de ataque.
5. IF um ou mais contêineres do laboratório não sobem, THEN THE Backend SHALL manter o Estágio 5 não
   concluído, identificar o(s) contêiner(es) que falharam ao subir e reportar a causa ao Pesquisador
   (Requisito 7).
6. THE desenho SHALL admitir que uma futura origem `gerado_ia` suba o ambiente por outra plataforma
   (ex.: `maquina_virtual` via Vagrant) sob a mesma costura, sem redesenho; a implementação dessa
   plataforma é não-objetivo desta spec.

### Requisito 4: Carregar as Abilities no Caldera

**História de usuário:** Como Pesquisador, quero que o provisionamento carregue as Abilities da
campanha no Caldera na ordem do DAG, para que o Estágio 6 possa executar a Operação.

#### Critérios de Aceitação

1. WHEN os contêineres do laboratório estão saudáveis, THE Backend SHALL carregar as Abilities
   ordenadas da campanha no Caldera na ordem estipulada pelo DAG (`atomic_ordering`), seguidas do
   Adversary curado.
2. THE Backend SHALL reusar o cliente Caldera existente
   (`CalderaClient.load_abilities`/`load_adversary`) para o carregamento.
3. WHEN a campanha é curada, THE Backend SHALL ler as Abilities e o Adversary dos artefatos curados
   em disco, sem invocar IA.
4. IF o carregamento das Abilities no Caldera falha (Caldera indisponível, erro de API, ou artefato
   ausente), THEN THE Backend SHALL manter o Estágio 5 não concluído e reportar a causa ao
   Pesquisador (Requisito 7).
5. THE Backend SHALL registrar na saída do Estágio 5 que as Abilities foram carregadas apenas quando
   o carregamento concluiu com sucesso.

### Requisito 5: Verificação de pré-requisitos antes de concluir (homologação do ambiente)

**História de usuário:** Como Pesquisador, quero que o Estágio 5 só conclua quando todos os
pré-requisitos de execução estiverem preenchidos e verificados, para que eu tenha certeza de que a
campanha pode ser executada no Estágio 6.

#### Critérios de Aceitação

1. BEFORE concluir o Estágio 5, THE Backend SHALL verificar que **todos** os contêineres-alvo do
   ambiente estão saudáveis (no estado `running`, e `healthy` quando o serviço define
   `healthcheck`).
2. BEFORE concluir o Estágio 5, THE Backend SHALL verificar que as Abilities ordenadas da campanha
   foram carregadas no Caldera.
3. BEFORE concluir o Estágio 5, THE Backend SHALL verificar o isolamento de todos os
   contêineres-alvo por meio de `containment/isolation`, usando somente inspeção read-only do Docker,
   produzindo um resultado por contêiner e um veredito geral de aprovação ou reprovação.
4. THE Backend SHALL considerar um contêiner-alvo isolado somente quando ele estiver anexado
   exclusivamente a redes `internal: true`, sem bridge externo e sem DNS externo.
5. WHEN os contêineres estão saudáveis, as Abilities estão carregadas no Caldera e o isolamento está
   aprovado para todos os contêineres-alvo, THE Backend SHALL permitir concluir o Estágio 5.
6. THE Pipeline_UI SHALL apresentar ao Pesquisador uma confirmação explícita de que o ambiente está
   pronto, listando cada pré-requisito verificado (contêineres saudáveis, Abilities carregadas,
   isolamento aprovado) e seu resultado.
7. THE verificação de isolamento SHALL ser fail-closed: uma lista vazia de contêineres-alvo ou um
   daemon Docker indisponível reprova o isolamento.

### Requisito 6: Saída do Estágio 5 — parâmetros do ambiente para a execução

**História de usuário:** Como Pesquisador, quero que o Estágio 5 produza, como saída, todos os
parâmetros do ambiente que a execução precisa, para que o Estágio 6 receba uma entrada completa e
rastreável.

#### Critérios de Aceitação

1. WHEN o Estágio 5 conclui, THE Backend SHALL produzir uma saída tipada do ambiente provisionado
   contendo, no mínimo: a plataforma (`conteiner`), os contêineres-alvo resolvidos, o arquivo de
   composição usado, o resumo de isolamento (veredito por contêiner e geral), o indicador de
   Abilities carregadas e a origem (`curadoria_humana`).
2. THE Saída do Estágio 5 SHALL ser a Entrada do Estágio 6.
3. THE Backend SHALL persistir a saída do Estágio 5 de modo que o Estágio 6 e a tela a consumam após
   a conclusão.
4. THE Backend SHALL manter o Estágio 6 bloqueado enquanto o Estágio 5 não estiver `concluido`.

### Requisito 7: Mensagem de falha na tela e bloqueio do fluxo

**História de usuário:** Como Pesquisador, quero que, se o provisionamento falhar, o sistema me
mostre na tela o que falhou e me impeça de seguir, para que eu não execute uma campanha em um
ambiente incompleto.

#### Critérios de Aceitação

1. IF qualquer etapa do provisionamento falha (subir contêineres, aguardar saúde, carregar
   Abilities, ou verificar isolamento), THEN THE Backend SHALL manter o Estágio 5 não concluído e
   registrar a etapa que falhou e a causa.
2. WHEN uma etapa do provisionamento falha, THE Pipeline_UI SHALL exibir na tela do Estágio 5 uma
   mensagem de erro que identifica **o que** falhou (etapa e recurso) e **por quê** (causa legível).
3. WHILE o Estágio 5 não estiver `concluido`, THE Pipeline_UI SHALL impedir o avanço para o Estágio
   6 (controle "Próximo" indisponível ou bloqueado).
4. WHEN o isolamento de um contêiner-alvo é reprovado, THE Pipeline_UI SHALL identificar o contêiner
   não isolado e a razão da reprovação.
5. THE mensagem de falha na tela SHALL ser consistente com as linhas de erro exibidas no terminal de
   logs ao vivo (Requisito 2).

### Requisito 8: Desprovisionamento ao término da campanha

**História de usuário:** Como Pesquisador, quero que o ambiente seja desprovisionado ao término da
campanha, para que o laboratório seja liberado e uma nova campanha possa ser executada.

#### Critérios de Aceitação

1. WHEN a execução da campanha conclui no Estágio 6, THE Backend SHALL desprovisionar o ambiente que
   foi provisionado para essa campanha.
2. THE desprovisionamento SHALL remover/parar os contêineres do laboratório que foram subidos para a
   campanha, liberando os recursos de hardware.
2a. THE desprovisionamento SHALL **preservar as imagens** dos contêineres do laboratório (não executar
   `--rmi`), de modo que uma campanha seguinte reaproveite a imagem já construída sem novo build
   (Requisito 11).
3. THE Backend SHALL conduzir o desprovisionamento pela mesma costura `ProvisionadorDeAmbiente`
   (método de teardown), mantendo a origem coesa com o provisionamento.
4. IF o desprovisionamento falha, THEN THE Backend SHALL reportar a falha ao Pesquisador e
   identificar os recursos que permaneceram provisionados.
5. THE Backend SHALL provisionar e desprovisionar **uma campanha por vez** neste primeiro momento;
   a otimização de reaproveitar um provisionamento para executar múltiplas campanhas é não-objetivo
   desta spec (ver Questões em aberto).

### Requisito 9: Reuso e preservação da contenção/isolamento existente

**História de usuário:** Como Pesquisador, quero que o provisionamento reutilize as proteções de
contenção já implementadas, para que o isolamento continue sendo verificado da mesma forma
confiável.

#### Critérios de Aceitação

1. THE Backend SHALL reusar o pacote `ui/backend/app/services/containment/` (parser de destinos,
   sub-redes internas, validação e verificação de isolamento) sem duplicar a sua lógica.
2. THE verificação de isolamento SHALL usar somente inspeção read-only do Docker, sem iniciar,
   parar, criar ou modificar contêineres ou redes como parte da verificação em si.
3. THE Backend SHALL reusar o mapa estático de hosts/IPs/sub-redes de
   `ui/backend/app/services/environment/topologia_builder.py` como fonte única da topologia exibida.

### Requisito 10: Extensibilidade por origem (preparação para o agente de IA)

**História de usuário:** Como Pesquisador, quero que o desenho do Estágio 5 já esteja preparado para
um agente de IA que criará a topologia e proverá o ambiente, para que a substituição ocorra sem
redesenho.

#### Critérios de Aceitação

1. THE Backend SHALL expor o provisionamento por meio da costura `ProvisionadorDeAmbiente` com um
   ponto de extensão por `origem` (`curadoria_humana` hoje, `gerado_ia` no futuro).
2. THE desenho SHALL permitir que uma futura implementação `gerado_ia` **crie** a topologia sob
   medida (Docker Compose para contêineres ou Vagrant para máquinas virtuais) e suba o ambiente sob
   a mesma assinatura, sem alterar a saída tipada do Estágio 5.
3. THE implementação do agente de IA, do suporte a máquinas virtuais / Vagrant, e da criação
   dinâmica de topologia SHALL ser não-objetivo desta spec.

### Requisito 11: Reuso de imagem e build condicional (sem rebuild quando a imagem já existe)

**História de usuário:** Como Pesquisador, quero que o provisionamento **reuse as imagens de contêiner
já construídas** quando elas já existem na máquina, e só execute o build quando faltarem, para que o
Estágio 5 não dependa de internet/registro a cada campanha e não quebre por um build desnecessário.

#### Critérios de Aceitação

1. WHEN o provisionamento vai subir o laboratório, THE Backend SHALL verificar, para cada serviço do
   laboratório de ataque, se a sua **imagem já existe** na Docker Engine local (inspeção read-only por
   nome de imagem estável).
2. IF todas as imagens dos serviços do laboratório já existem, THEN THE Backend SHALL subir o ambiente
   **sem executar build** (reaproveitando as imagens já construídas), sem exigir internet nem registro.
3. IF uma ou mais imagens dos serviços do laboratório **não existem**, THEN THE Backend SHALL executar o
   build dessas imagens **antes** de subir (etapa que exige internet para instalar pacotes/ferramentas),
   emitindo as linhas de log do build ao terminal (Requisito 2), e então subir o ambiente.
4. THE Backend SHALL identificar as imagens do laboratório por um **nome de imagem estável** por serviço
   (ex.: `sticks-kali-attacker`, `sticks-caldera-attacker`, `sticks-nginx-target`, `sticks-mariadb-target`),
   de modo que a mesma imagem seja reconhecida/reaproveitada entre campanhas e entre execuções do fluxo,
   independentemente do nome do projeto/pasta de build.
5. WHEN o Caldera está no ar, THE Backend SHALL inserir as Abilities da campanha **por API**
   (`POST /api/v2/abilities`), sem reconstruir a imagem do Caldera para trocar de campanha (Requisito 4);
   trocar de campanha SHALL afetar apenas os parâmetros/comandos inseridos em runtime, não a imagem.
6. THE desprovisionamento (Requisito 8) SHALL **preservar as imagens** construídas/reaproveitadas, para que
   a campanha seguinte as reuse sem novo build.
7. THE build condicional e o reuso de imagem SHALL valer para a origem `curadoria_humana` (ambiente curado
   do `docker/docker-compose.yml`); uma futura origem `gerado_ia` poderá adotar a mesma política de
   reuso/build sob a mesma costura, sem redesenho (Requisito 10).

## Questões em aberto

Estas questões foram levantadas pelo Pesquisador e **não são resolvidas** nesta spec; ficam
registradas para decisão futura (serão endereçadas quando o agente de IA for planejado):

1. **Um compose por campanha?** Ainda não está decidido se cada campanha terá o seu próprio arquivo
   de composição de contêineres, ou se um mesmo compose pode ser reaproveitado por campanhas
   diferentes. No primeiro momento há **um único** laboratório curado (`docker/docker-compose.yml`)
   e **uma** campanha executada por vez (ShadowRay).
2. **Reaproveitar um provisionamento para múltiplas campanhas?** No primeiro momento o fluxo
   provisiona e desprovisiona **por campanha**. Quando o agente de IA fizer provisionamento e
   execução, será preciso estudar se é possível subir o ambiente uma vez e executar mais de uma
   campanha antes de desprovisionar, como otimização de recursos.
