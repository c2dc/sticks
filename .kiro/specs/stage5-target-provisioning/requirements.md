# Requirements Document

## Introduction

Esta spec refina o **Estágio 5 — Criação do ambiente de emulação (Provisionamento do
ambiente)** do pipeline de seis Estágios de STICKS, subdividindo-o em **duas sub-etapas
explícitas e orquestradas** para que o próprio sistema **prepare os hosts-alvo** antes de
liberar a Execução do Estágio 6.

### Problema que esta spec resolve

Hoje o Estágio 5 é implementado por `CurationEnvironmentProvisioner`
(`apps/backend/app/services/environment/provisioner.py`) de forma **somente-leitura**: ele
(a) resolve os contêineres-alvo, (b) carrega no Caldera as Abilities curadas ordenadas e o
Adversary, e (c) **verifica** o isolamento. Ele **não configura de fato** os contêineres-alvo
(nginx/db) com o setup que a campanha precisa.

Entretanto, o Estágio 4 ("Operacionalização") já **deriva e repassa adiante** um artefato tipado
`EnvironmentParameters` (`apps/backend/app/services/environment/operationalization.py`) contendo,
por host-alvo: `label`, `address` (IP interno), `container` (contêiner do laboratório),
`host_type` e `vulnerabilities` (os comandos de setup `target_vuln_host_a`/`target_vuln_host_b`
derivados dos nós do DAG curado), além de `services` e `network_context` inferidos. Ou seja, os
parâmetros de que os alvos precisam **já fluem** do Estágio 4 para o Estágio 5 — o Estágio 5
simplesmente **nunca os aplica**.

A consequência é concreta: quando uma campanha curada (ex.: ShadowRay) é emulada, técnicas cujo
setup de alvo exige **baixar/instalar da internet** (nmap, xmrig via `wget`/`git`/`apt`/`pip`)
**falham**, porque as redes de ataque/alvo são `internal: true` (sem egresso) — e esse **sem
egresso** é a **propriedade central de contenção** do projeto. O diário de desenvolvimento
(`docs/journal/2026-10-01.md`) registra que a equipe contornou isso movendo a instalação de
pacotes para o **build-time** do Docker.

### A funcionalidade desejada

Subdividir o Estágio 5 em duas sub-etapas explícitas e orquestradas, de modo que o sistema prepare
os alvos:

- **5.1 Build (online):** com acesso à internet, instalar pacotes e baixar os binários/artefatos
  pesados de que cada host-alvo precisa (derivados de `vulnerabilities` / `EnvironmentParameters`
  vindos do Estágio 4). **Esta é a única fase autorizada a tocar a internet.**
- **5.2 Runtime (isolado):** **RE-ISOLAR** o ambiente (reatar os alvos às redes de ataque
  `internal: true`, **sem egresso**) e, então, aplicar a configuração/setup **offline** restante
  (criar usuários, escrever payloads, iniciar serviços), deixando os alvos em estado atacável.
  **Somente depois disso** o portão de isolamento passa e a Execução do Estágio 6 é liberada.

### Invariante crítico de segurança (requisito duro)

Em **nenhum momento** um contêiner-alvo pode estar **simultaneamente** (a) conectado à internet
**E** (b) anexado às redes de ataque junto com os demais alvos do laboratório. A **janela de build
online** e a **janela de ataque isolada** devem ser **mutuamente exclusivas e verificáveis**. O
portão de isolamento existente (`inspect_isolation` / `IsolationResult`) deve confirmar o
isolamento completo (**fail-closed**) **antes** de a 5.2 concluir e **antes** de o Estágio 6 poder
rodar. A perda de egresso deve ser **positivamente verificada**, não presumida.

### Escopo e não-objetivos

- **No escopo:** subdivisão orquestrada do Estágio 5 em 5.1 (Build/online) e 5.2
  (Runtime/isolado); consumo do `EnvironmentParameters` do Estágio 4 para derivar os
  pré-requisitos online de cada host; re-isolamento verificado antes do setup offline;
  reaproveitamento do portão de isolamento existente como pré-condição de conclusão; o invariante
  de mútua exclusão (internet × rede-de-ataque) com verificação positiva de perda de egresso;
  observabilidade por linhas de log transmitidas ao terminal do Estágio 5; idempotência de re-runs;
  tratamento de falhas sem estado inseguro remanescente; desprovisionamento ao término do
  Estágio 6; costura por origem (`curadoria_humana` hoje, `gerado_ia` depois).
- **Fora de escopo (não-objetivo):** a implementação do agente de IA (`gerado_ia`); o suporte a
  **máquinas virtuais** (admitido no desenho via `EnvironmentPlatform`, sem redesenho, mas não
  implementado aqui); a reescrita da contenção/isolamento em
  `apps/backend/app/services/containment/` (é **reusada e preservada**); as mecânicas Docker
  concretas da janela de egresso e do re-isolamento (detalhe de desenho).

### Plataforma e módulos de referência

- Estágio 4 (artefato de entrada): `apps/backend/app/services/environment/operationalization.py`
  (`EnvironmentParameters`, `TargetHost`, `RequiredService`) e
  `apps/backend/app/services/environment/operationalizer.py`.
- Estágio 5 (costura a estender): `apps/backend/app/services/environment/provisioner.py`
  (`EnvironmentProvisioner`, `CurationEnvironmentProvisioner`, `ProvisionedEnvironment`,
  `DeprovisioningResult`, `LogLine`/`on_log`).
- Contenção/isolamento (reuso): `apps/backend/app/services/containment/isolation.py`
  (`inspect_isolation`, `IsolationResult`, verdito por contêiner, fail-closed).
- Plataforma: `EnvironmentPlatform` (`CONTAINER` implementado; `maquina_virtual` admitida no
  desenho, não implementada).

## Glossary

- **Estágio 5 / Provisionamento do ambiente**: Ato de **preparar e deixar atacável** o ambiente
  de emulação contido de uma campanha, agora subdividido nas sub-etapas 5.1 (Build/online) e 5.2
  (Runtime/isolado). É a Saída que o Estágio 6 consome.
- **Sub-etapa 5.1 (Build/online)**: Fase do Estágio 5 que, **com acesso à internet**, instala
  pacotes e baixa os binários/artefatos pesados de que cada host-alvo precisa, derivados do
  `EnvironmentParameters`. É a **única** fase autorizada a acessar a internet.
- **Sub-etapa 5.2 (Runtime/isolado)**: Fase do Estágio 5 que **re-isola** os alvos nas redes de
  ataque `internal: true` (sem egresso), **verifica** a perda de egresso e, então, aplica o setup
  **offline** (usuários, serviços, payloads) e carrega as Abilities/Adversary no Caldera.
- **Janela de egresso / Janela de build online**: O intervalo em que um host-alvo tem acesso à
  internet, exclusivo da sub-etapa 5.1.
- **Janela de ataque isolada**: O intervalo em que os alvos estão anexados às redes de ataque
  `internal: true` junto com os demais alvos, sem egresso, próprio da sub-etapa 5.2 em diante.
- **EnvironmentParameters**: O artefato tipado (2ª Saída do Estágio 4) com, por host-alvo,
  `label`, `address`, `container`, `host_type` e `vulnerabilities`, além de `services` e
  `network_context`. É a Entrada que a 5.1 consome.
- **vulnerabilities (de um host)**: Os comandos de setup `target_vuln_host_a`/`target_vuln_host_b`
  do nó do DAG curado associado ao host; alguns exigem internet (ex.: `apt`, `pip`, `wget`, `git`).
- **Portão de isolamento**: A verificação read-only de isolamento (`inspect_isolation` →
  `IsolationResult`) que aprova somente quando **todos** os contêineres-alvo estão isolados; é
  **fail-closed** (lista vazia ou daemon indisponível reprova).
- **Isolamento de contêiner**: Condição em que um contêiner-alvo está anexado **exclusivamente** a
  redes `internal: true`, sem bridge externo e sem DNS externo.
- **Verificação positiva de perda de egresso**: A confirmação ativa de que o host-alvo **não** tem
  mais acesso à internet, obtida por verificação (não presumida pela ausência de ação).
- **Setup online**: Parte do setup do host que exige internet (download/instalação de pacotes e
  binários); pertence à 5.1.
- **Setup offline**: Parte do setup do host que **não** exige internet (criar usuários, escrever
  payloads, iniciar serviços); pertence à 5.2.
- **failed_step**: Campo da Saída do Estágio 5 que identifica a sub-etapa/etapa que falhou
  (ex.: `build_online`, `reisolamento`, `setup_offline`, `verificar_isolamento`), ao lado da causa.
- **Linha de log / `LogLine`**: Entrada textual (comando, stdout/stderr ou marcação de etapa)
  emitida pelo provisionamento e transmitida ao terminal do Estágio 5 via `on_log`.
- **EnvironmentProvisioner**: A costura por `origem` do Estágio 5 (`provisioner.py`);
  implementação de curadoria hoje, implementação de IA encaixável depois, sem redesenho.
- **Origem / TranslationSource**: Atributo que indica **como** o ambiente foi criado:
  `curadoria_humana` (hoje) ou `gerado_ia` (futuro).
- **EnvironmentPlatform**: Plataforma do ambiente — `CONTAINER` (implementada). `maquina_virtual`
  é admitida no desenho (futuro), não implementada aqui.
- **Desprovisionamento / Teardown**: Remoção/parada do ambiente provisionado ao término do
  Estágio 6, incluindo tudo que 5.1/5.2 criaram.
- **Pesquisador**: Pessoa usuária da ferramenta STICKS.
- **ShadowRay**: Campanha curada (slug `ShadowRay`) usada como primeiro caso executado ponta a
  ponta.

## Requirements

### Requisito 1: Subdivisão orquestrada do Estágio 5 em 5.1 (Build/online) e 5.2 (Runtime/isolado)

**História de usuário:** Como Pesquisador, quero que o Estágio 5 seja dividido em duas sub-etapas
explícitas e executadas em ordem estrita — primeiro o Build online, depois o Runtime isolado —,
para que o sistema prepare os alvos de forma previsível e segura antes de liberar a Execução.

#### Critérios de Aceitação

1. THE Estágio 5 SHALL ser composto por duas sub-etapas nomeadas e distintas: 5.1 (Build/online) e
   5.2 (Runtime/isolado).
2. WHEN o Estágio 5 de uma campanha inicia, THE Backend SHALL executar a sub-etapa 5.1 antes da
   sub-etapa 5.2.
3. IF a sub-etapa 5.1 não concluiu com sucesso, THEN THE Backend SHALL NÃO iniciar a sub-etapa 5.2.
4. THE Backend SHALL conduzir ambas as sub-etapas por meio da costura `EnvironmentProvisioner`,
   registrando a origem como `curadoria_humana` para o ambiente curado.
5. THE Estágio 5 SHALL ser considerado concluído somente quando a sub-etapa 5.1 e a sub-etapa 5.2
   concluírem com sucesso.

### Requisito 2: Sub-etapa 5.1 — provisionamento dos pré-requisitos online a partir do Estágio 4

**História de usuário:** Como Pesquisador, quero que a sub-etapa 5.1 consuma os parâmetros vindos
do Estágio 4 e baixe/instale, com internet, tudo que cada host-alvo precisa, para que os artefatos
pesados estejam presentes antes do isolamento.

#### Critérios de Aceitação

1. THE sub-etapa 5.1 SHALL ter como Entrada o `EnvironmentParameters` produzido pelo Estágio 4,
   incluindo, por host-alvo, os campos `vulnerabilities`, `services`, `host_type`, `container` e
   `address`.
2. WHEN a sub-etapa 5.1 executa para um host-alvo, THE Backend SHALL derivar os pré-requisitos
   online desse host a partir do seu `vulnerabilities` (comandos de setup `target_vuln_host_a`/
   `target_vuln_host_b`) e dos `services` inferidos.
3. WHILE a janela de egresso de um host-alvo está aberta na sub-etapa 5.1, THE Backend SHALL
   executar os passos de instalação/download online desse host (ex.: `apt`, `pip`, `wget`, `git`,
   downloads de binários).
4. THE sub-etapa 5.1 SHALL ser a **única** fase do Estágio 5 autorizada a conceder acesso à internet
   a um host-alvo.
5. WHEN a sub-etapa 5.1 conclui para um host-alvo, THE Backend SHALL encerrar a janela de egresso
   desse host antes de prosseguir para a sub-etapa 5.2.
6. IF a sub-etapa 5.1 falha ao obter um pré-requisito online de qualquer host-alvo, THEN THE Backend
   SHALL manter o Estágio 5 não concluído, registrar a etapa que falhou e a causa, e NÃO iniciar a
   sub-etapa 5.2 (Requisito 8).

### Requisito 3: Sub-etapa 5.2 — re-isolamento, verificação de egresso e setup offline

**História de usuário:** Como Pesquisador, quero que a sub-etapa 5.2 re-isole os alvos, confirme
que o acesso à internet acabou e só então aplique o setup offline e carregue as Abilities no
Caldera, para que o ambiente fique atacável sem jamais vazar para a internet.

#### Critérios de Aceitação

1. WHEN a sub-etapa 5.2 inicia, THE Backend SHALL reatar cada host-alvo às redes de ataque
   `internal: true` junto com os demais alvos do laboratório.
2. AFTER reatar os alvos às redes de ataque e BEFORE aplicar qualquer setup offline, THE Backend
   SHALL verificar positivamente que cada host-alvo não tem mais acesso à internet (perda de
   egresso confirmada, não presumida).
3. WHEN a perda de egresso de todos os host-alvo está confirmada, THE Backend SHALL aplicar o setup
   offline de cada host (ex.: criar usuários, escrever payloads, iniciar serviços), sem acessar a
   internet.
4. WHEN o setup offline conclui, THE Backend SHALL carregar as Abilities ordenadas da campanha no
   Caldera na ordem do DAG (`atomic_ordering`), seguidas do Adversary curado.
5. IF a verificação positiva de perda de egresso reprova para qualquer host-alvo, THEN THE Backend
   SHALL interromper a sub-etapa 5.2 sem aplicar o setup offline, manter o Estágio 5 não concluído e
   reportar a causa (Requisito 8).
6. IF o setup offline ou o carregamento das Abilities falha, THEN THE Backend SHALL manter o
   Estágio 5 não concluído e reportar a etapa que falhou e a causa (Requisito 8).

### Requisito 4: Portão de isolamento como pré-condição de conclusão e de liberação do Estágio 6

**História de usuário:** Como Pesquisador, quero que o Estágio 5 só conclua e o Estágio 6 só
desbloqueie quando o portão de isolamento aprovar todos os alvos, para que nenhuma campanha execute
em um ambiente não contido.

#### Critérios de Aceitação

1. BEFORE considerar a sub-etapa 5.2 concluída, THE Backend SHALL verificar o isolamento de
   **todos** os contêineres-alvo reusando a semântica existente (`inspect_isolation` →
   `IsolationResult`), com inspeção read-only do Docker.
2. THE Backend SHALL considerar um contêiner-alvo isolado somente quando ele estiver anexado
   exclusivamente a redes `internal: true`, sem bridge externo e sem DNS externo.
3. THE verificação de isolamento SHALL ser fail-closed: uma lista vazia de contêineres-alvo ou um
   daemon Docker indisponível reprova o isolamento.
4. IF o portão de isolamento reprova qualquer contêiner-alvo, THEN THE Backend SHALL manter o
   Estágio 5 não concluído, identificar o contêiner não isolado e a razão da reprovação, e manter o
   Estágio 6 bloqueado.
5. WHEN o portão de isolamento aprova todos os contêineres-alvo e as demais etapas da 5.2 concluíram,
   THE Backend SHALL permitir concluir o Estágio 5 e desbloquear o Estágio 6.
6. THE Backend SHALL registrar na Saída do Estágio 5 o resumo de isolamento (verdito por contêiner e
   veredito geral).

### Requisito 5: Invariante de contenção — internet e rede-de-ataque mutuamente exclusivos

**História de usuário:** Como Pesquisador, quero a garantia dura de que um alvo jamais está, ao
mesmo tempo, conectado à internet e anexado às redes de ataque com os demais alvos, para que a
contenção do laboratório nunca seja violada.

#### Critérios de Aceitação

1. THE Backend SHALL garantir que, em nenhum instante, um contêiner-alvo esteja simultaneamente (a)
   com acesso à internet e (b) anexado às redes de ataque junto com os demais alvos do laboratório.
2. THE janela de build online (5.1) e a janela de ataque isolada (5.2 em diante) SHALL ser
   mutuamente exclusivas para cada host-alvo.
3. BEFORE anexar um host-alvo às redes de ataque na sub-etapa 5.2, THE Backend SHALL ter encerrado a
   janela de egresso desse host.
4. THE Backend SHALL verificar positivamente a perda de egresso de cada host-alvo antes de qualquer
   setup offline (Requisito 3.2), tratando o acesso à internet como presente até que a ausência seja
   comprovada.
5. IF a mútua exclusão não puder ser comprovada para qualquer host-alvo (egresso ainda presente,
   coexistência de internet e rede-de-ataque, ou verificação inconclusiva), THEN THE Backend SHALL
   tratar o Estágio 5 como falho e NÃO desbloquear o Estágio 6 (Requisito 8).

### Requisito 6: Observabilidade das sub-etapas no terminal do Estágio 5

**História de usuário:** Como Pesquisador, quero acompanhar no terminal do Estágio 5 as linhas de
log ordenadas de ambas as sub-etapas, para que eu veja o que está acontecendo em uma fase demorada
e distinga Build de Runtime.

#### Critérios de Aceitação

1. WHILE qualquer sub-etapa do Estágio 5 está em andamento, THE Backend SHALL emitir linhas de log
   (`LogLine`) por meio do mecanismo `on_log` existente.
2. THE Backend SHALL emitir as linhas de log na ordem em que os passos ocorrem, preservando a
   sequência dos comandos e de sua saída (stdout e stderr).
3. THE Backend SHALL marcar nas linhas de log a qual sub-etapa cada bloco pertence (5.1 Build/online
   ou 5.2 Runtime/isolado) e a etapa corrente (ex.: build online, re-isolamento, verificar egresso,
   setup offline, carregar Abilities, verificar isolamento).
4. THE Pipeline_UI SHALL apresentar as linhas de log das duas sub-etapas no terminal de
   provisionamento do Estágio 5, preservando a ordem de emissão.

### Requisito 7: Idempotência e segurança de re-execução

**História de usuário:** Como Pesquisador, quero poder re-executar o Estágio 5 sobre um ambiente já
provisionado sem gerar estado duplicado ou quebrado, para que uma nova tentativa seja sempre segura.

#### Critérios de Aceitação

1. WHEN o Estágio 5 é re-executado para uma campanha cujo ambiente já foi provisionado com sucesso,
   THE Backend SHALL deixar o ambiente em um estado final equivalente ao de uma execução única bem
   sucedida, sem duplicar recursos nem corromper o estado existente.
2. WHEN a sub-etapa 5.1 é re-executada e um pré-requisito online já está presente em um host-alvo,
   THE Backend SHALL reaproveitá-lo sem recriá-lo de forma duplicada ou conflitante.
3. WHEN a sub-etapa 5.2 é re-executada, THE Backend SHALL reaplicar o setup offline de modo que o
   resultado final seja o mesmo de uma primeira aplicação bem sucedida (sem usuários/serviços/payloads
   duplicados ou inconsistentes).
4. THE re-execução do Estágio 5 SHALL preservar o invariante de contenção (Requisito 5) em todos os
   momentos.

### Requisito 8: Tratamento de falhas sem estado inseguro remanescente

**História de usuário:** Como Pesquisador, quero que, se 5.1 ou 5.2 falhar, o Estágio 5 permaneça
não concluído com a causa reportada e sem deixar nenhum alvo com egresso, para que uma falha nunca
resulte em um ambiente inseguro.

#### Critérios de Aceitação

1. IF a sub-etapa 5.1 ou a sub-etapa 5.2 falha, THEN THE Backend SHALL manter o Estágio 5 não
   concluído e registrar o `failed_step` (a etapa que falhou) e a causa legível na Saída do
   Estágio 5.
2. WHEN o Estágio 5 falha, THE Pipeline_UI SHALL exibir a etapa que falhou e a causa, de forma
   consistente com as linhas de erro do terminal (Requisito 6).
3. IF uma falha ocorre com a janela de egresso de qualquer host-alvo ainda aberta, THEN THE Backend
   SHALL encerrar o acesso à internet desse host ou desprovisioná-lo, de modo que nenhum alvo
   permaneça com egresso após a falha.
4. WHEN o Estágio 5 falha, THE Backend SHALL deixar o ambiente em um de dois estados finais seguros:
   isolado (sem egresso) ou totalmente desprovisionado.
5. WHILE o Estágio 5 não estiver concluído, THE Backend SHALL manter o Estágio 6 bloqueado.

### Requisito 9: Extensibilidade por origem (curadoria_humana hoje, gerado_ia depois)

**História de usuário:** Como Pesquisador, quero que as duas sub-etapas respeitem a costura por
origem existente, para que uma futura origem de IA seja encaixável sem redesenho.

#### Critérios de Aceitação

1. THE Backend SHALL expor as sub-etapas 5.1 e 5.2 por meio da costura `EnvironmentProvisioner`,
   mantendo o ponto de extensão por `origem` (`TranslationSource`: `curadoria_humana` hoje,
   `gerado_ia` no futuro).
2. THE desenho SHALL permitir que uma futura origem `gerado_ia` execute o Build online e o Runtime
   isolado sob a mesma assinatura, sem alterar a Saída tipada do Estágio 5.
3. THE Backend SHALL manter a origem coesa entre o provisionamento (5.1/5.2) e o desprovisionamento
   (Requisito 10).

### Requisito 10: Desprovisionamento ao término do Estágio 6 cobrindo o que 5.1/5.2 criaram

**História de usuário:** Como Pesquisador, quero que o desprovisionamento ao término do Estágio 6
limpe tudo que as sub-etapas 5.1 e 5.2 criaram, para que o laboratório seja liberado sem resíduos.

#### Critérios de Aceitação

1. WHEN a execução da campanha conclui no Estágio 6, THE Backend SHALL desprovisionar o ambiente pela
   mesma costura `EnvironmentProvisioner` (teardown), mantendo a origem coesa.
2. THE desprovisionamento SHALL remover/parar os recursos criados pelas sub-etapas 5.1 e 5.2
   (contêineres, setup e artefatos introduzidos no provisionamento).
3. IF o desprovisionamento falha, THEN THE Backend SHALL reportar a falha e identificar os recursos
   que permaneceram provisionados (`remaining_resources`).

### Requisito 11: Escopo de plataforma — somente CONTAINER nesta spec

**História de usuário:** Como Pesquisador, quero que esta spec cubra apenas a plataforma de
contêineres, mas sem fechar a porta para máquinas virtuais no futuro, para que a adoção de VM
ocorra sem redesenho.

#### Critérios de Aceitação

1. THE Backend SHALL implementar as sub-etapas 5.1 e 5.2 apenas para a plataforma `CONTAINER`
   (`EnvironmentPlatform.CONTAINER`).
2. THE desenho SHALL admitir que uma futura plataforma `maquina_virtual` adote o mesmo fluxo de duas
   sub-etapas (Build online, depois Runtime isolado) sob a mesma costura, sem redesenho; a
   implementação dessa plataforma é não-objetivo desta spec.
