# Requirements Document

## Introduction

Esta funcionalidade define uma interface gráfica (a "Pipeline_UI") para o projeto **sticks**, que acompanha o artigo *"The Procedural Semantics Gap in ATT&CK-in-STIX: Measuring Procedural Sufficiency for APT Emulation"*. O objetivo científico do projeto é medir se a inteligência de ameaças em STIX (ATT&CK-in-STIX) contém informação procedural suficiente para reproduzir campanhas de APT num ambiente de laboratório controlado.

O sistema atual é um pipeline Python de três estágios orquestrado por `main.py`:

- **Estágio 1 — Modelagem Estrutural Automatizada**: parsing de datasets STIX, extração de técnicas, relacionamentos, indicadores, infraestrutura, malware e metadados de campanha; também baixa dados do Atomic Red Team.
- **Estágio 2 — Tradução com Humano no Loop (curadoria humana)**: um analista converte descrições abstratas de comportamento (técnicas ATT&CK) em passos executáveis mínimos — comandos concretos, ordem, dependências, privilégios e infraestrutura necessária. É aqui que reside o gap procedural que o projeto quer medir.
- **Estágio 3 — Emulação de Adversário**: os passos traduzidos viram abilities/adversaries e são executados via MITRE Caldera dentro de um ambiente Docker isolado (containers caldera, kali, nginx, db em redes internas estáticas), acessível em http://localhost:8888.

Existem **8 casos já curados manualmente** por colegas (Roth e Sidnei), representados por arquivos `*_dag-ability.json` e `*_dag-adversary.json` em `sticks/data/api/`: APT41-DUST, ATT&CK Campaign C0010, ATT&CK Campaign C0026, CostaRicto, Operation MidnightEclipse, Operation Outer Space, Salesforce Data Exfiltration e ShadowRay.

Esta é a **primeira entrega** e seu escopo é: (a) uma interface moderna que torna o pipeline evidente, com telas por fase mostrando **entrada, processamento e saída**; (b) condução passo a passo da análise/replicação dos 8 casos curados existentes; (c) execução **contida com segurança** de pelo menos 1 ou 2 casos curados dentro do Docker Desktop, com garantias de isolamento para que nenhum comando de adversário toque a máquina do pesquisador ou alvos externos; (d) tema claro/escuro com paleta blue/red/purple team; (e) internacionalização pt-BR/en; e (f) persistência de estado para continuidade entre máquinas.

### Fora de Escopo (Fase Futura)

- Os **agentes de IA** que substituirão a curadoria humana do Estágio 2 são uma funcionalidade separada e posterior. Esta spec trata apenas da interface e da replicação/execução contida dos casos já curados. A UI, entretanto, deve ser projetada de modo que o Estágio 2 (hoje manual) possa no futuro ser assistido por agentes de IA.
- A escolha concreta de tecnologias de frontend, backend e banco de dados **não** é decidida nesta fase; será resolvida no design. Os requisitos apenas capturam restrições não-funcionais ("moderno e bom para desenvolvimento assistido por IA").

## Glossary

- **Pipeline_UI**: A interface gráfica web (frontend + backend + banco de dados) que expõe e conduz o pipeline de três estágios.
- **Backend**: O serviço de aplicação que orquestra o pipeline existente, expõe APIs para a interface e persiste o estado.
- **Frontend**: A camada de apresentação com a qual o Pesquisador interage no navegador.
- **Base_de_Dados**: O mecanismo de persistência que armazena estado de sessões, progresso, casos, execuções e resultados.
- **Pesquisador**: O usuário humano que opera a Pipeline_UI para conduzir e avaliar as campanhas.
- **Estágio**: Uma das três fases do pipeline (Estágio 1, Estágio 2, Estágio 3).
- **Caso_Curado**: Um dos 8 casos previamente traduzidos manualmente, representado por um par de arquivos ability/adversary em `data/api/`.
- **Ability**: Uma ação executável derivada de uma técnica ATT&CK, contendo identificador, técnica, tática, descrição e executores com comandos concretos.
- **Adversary**: Um agrupamento ordenado de Abilities que representa uma campanha executável na plataforma de emulação.
- **Caldera**: A plataforma MITRE Caldera de emulação de adversário, executada em container Docker e acessível em http://localhost:8888.
- **Ambiente_Docker**: O conjunto de containers (caldera, kali, nginx, db) e redes internas estáticas definidas em `docker/docker-compose.yml`.
- **Rede_Interna**: Rede Docker configurada com `internal: true`, sem rota para o host ou para a internet.
- **Operação**: Uma execução de um Adversary contra o Ambiente_Docker orquestrada pela Caldera.
- **Estado_de_Sessão**: O conjunto persistido de progresso, seleções e resultados que permite retomar o trabalho em outra máquina.
- **Modo_Claro**: Tema visual corporativo.
- **Modo_Escuro**: Tema visual com estética de cultura hacker.
- **Idioma**: O idioma da interface, sendo pt-BR ou en.

## Requirements

### Requisito 1 — Visualização do pipeline por fases

**User Story:** Como Pesquisador, quero uma visão geral que torne os três estágios do pipeline evidentes, para que eu compreenda o fluxo da inteligência STIX até a emulação.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL apresentar os três Estágios (Estágio 1, Estágio 2, Estágio 3) simultaneamente como etapas distintas, nomeadas e visualmente separadas em uma visão de pipeline.
2. THE Pipeline_UI SHALL exibir, para cada Estágio, uma seção rotulada de Entrada, uma seção rotulada de Processamento e uma seção rotulada de Saída, visualmente separadas entre si.
3. WHEN o Pesquisador seleciona um Estágio, THE Pipeline_UI SHALL exibir a tela dedicada daquele Estágio contendo suas seções de Entrada, Processamento e Saída em até 2 segundos.
4. WHEN o estado de um Estágio muda, THE Pipeline_UI SHALL atualizar o indicador visual daquele Estágio para exatamente um dos valores "não iniciado", "em andamento" ou "concluído" em até 2 segundos.
5. WHILE um Estágio está no estado "em andamento", THE Pipeline_UI SHALL exibir o progresso do processamento daquele Estágio como um percentual entre 0% e 100%, atualizado ao menos a cada 5 segundos.
6. IF o processamento de um Estágio falha, THEN THE Pipeline_UI SHALL exibir um estado de erro distinto para aquele Estágio com uma mensagem que indica a causa, preservando o estado dos demais Estágios.

### Requisito 2 — Tela do Estágio 1 (Modelagem Estrutural)

**User Story:** Como Pesquisador, quero ver a modelagem estrutural automatizada de um caso, para que eu entenda quais elementos foram extraídos do STIX.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL exibir, como Entrada do Estágio 1, o dataset STIX de origem associado ao Caso_Curado selecionado.
2. WHEN o Pesquisador inicia o Estágio 1 para um Caso_Curado, THE Backend SHALL executar a modelagem estrutural que extrai técnicas, relacionamentos, indicadores, infraestrutura, malware e metadados de campanha.
3. THE Pipeline_UI SHALL exibir, como Saída do Estágio 1, as técnicas ATT&CK extraídas, os relacionamentos entre ações e, quando presentes na extração, os indicadores, a infraestrutura, o malware e os metadados de campanha do Caso_Curado.
4. WHEN a modelagem estrutural de um Caso_Curado é concluída com sucesso, THE Backend SHALL persistir os elementos extraídos associados ao Caso_Curado e THE Pipeline_UI SHALL marcar o Estágio 1 desse Caso_Curado como "concluído".
5. IF a modelagem estrutural de um Caso_Curado é concluída sem extrair nenhuma técnica ATT&CK, THEN THE Pipeline_UI SHALL exibir, como Saída do Estágio 1, uma indicação de que nenhum elemento estrutural foi extraído para o Caso_Curado afetado.
6. IF a modelagem estrutural de um Caso_Curado falha, THEN THE Backend SHALL registrar a falha sem persistir extração parcial e THE Pipeline_UI SHALL exibir uma mensagem de erro que identifica o Caso_Curado afetado e a etapa da modelagem em que a falha ocorreu.

### Requisito 3 — Tela do Estágio 2 (Tradução Curada)

**User Story:** Como Pesquisador, quero revisar a tradução curada de comportamentos abstratos em passos executáveis, para que eu possa replicar o trabalho de curadoria existente.

#### Critérios de Aceitação

1. WHEN o Pesquisador acessa a tela do Estágio 2 para um Caso_Curado selecionado, THE Pipeline_UI SHALL exibir, como Entrada do Estágio 2, as técnicas ATT&CK abstratas resultantes do Estágio 1 desse Caso_Curado.
2. WHEN o Pesquisador acessa a tela do Estágio 2 para um Caso_Curado selecionado, THE Pipeline_UI SHALL exibir, como Saída do Estágio 2, as Abilities curadas do Caso_Curado, incluindo para cada Ability o identificador de técnica, a tática, a descrição e o comando concreto de cada executor.
3. WHEN o Pesquisador acessa a tela do Estágio 2 para um Caso_Curado selecionado, THE Pipeline_UI SHALL exibir a ordem sequencial e as dependências entre as Abilities do Caso_Curado.
4. WHEN o Pesquisador acessa a tela do Estágio 2 para um Caso_Curado selecionado, THE Pipeline_UI SHALL exibir um indicador textual associado à Saída do Estágio 2 informando que a tradução foi produzida por curadoria humana.
5. WHERE um Caso_Curado possui um Adversary associado, WHEN o Pesquisador acessa a tela do Estágio 2 desse Caso_Curado, THE Pipeline_UI SHALL exibir o agrupamento de Abilities que compõe esse Adversary.
6. IF o Estágio 1 do Caso_Curado selecionado não produziu técnicas ATT&CK, THEN THE Pipeline_UI SHALL exibir uma mensagem indicando ausência de Entrada do Estágio 2 e SHALL suprimir a exibição da Saída do Estágio 2.
7. IF o Caso_Curado selecionado não possui Abilities curadas, THEN THE Pipeline_UI SHALL exibir uma mensagem indicando ausência de Saída do Estágio 2.
8. IF o Caso_Curado selecionado não possui Adversary associado, THEN THE Pipeline_UI SHALL exibir uma indicação de que não há agrupamento de Abilities em Adversary para esse Caso_Curado.

### Requisito 4 — Tela do Estágio 3 (Emulação de Adversário)

**User Story:** Como Pesquisador, quero disparar e acompanhar a emulação de um caso curado, para que eu verifique se os comportamentos derivados do CTI podem ser reproduzidos.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL exibir, como Entrada do Estágio 3, o Adversary e as Abilities curadas do Caso_Curado selecionado.
2. WHEN o Pesquisador solicita a emulação de um Caso_Curado, THE Backend SHALL carregar as Abilities e o Adversary do Caso_Curado na Caldera e iniciar uma Operação contra o Ambiente_Docker.
3. WHILE uma Operação está em execução, THE Pipeline_UI SHALL exibir, para cada Ability da Operação, o status de execução como um dos valores "pendente", "em execução", "sucesso" ou "falha".
4. WHEN o status de execução de uma Ability é atualizado durante a Operação, THE Pipeline_UI SHALL exibir, como Saída do Estágio 3, o resultado dessa Ability, incluindo o status de sucesso ou falha e a saída do comando executado.
5. WHEN uma Operação atinge seu estado final, THE Pipeline_UI SHALL exibir o resultado agregado da Operação, indicando a quantidade de Abilities com status "sucesso" e a quantidade com status "falha".
6. IF a Caldera não responde no endereço configurado dentro de 10 segundos quando o Pesquisador solicita uma emulação, THEN THE Backend SHALL não iniciar nenhuma Operação e THE Pipeline_UI SHALL exibir uma mensagem indicando que o Ambiente_Docker não está disponível.

### Requisito 5 — Condução passo a passo dos 8 casos curados

**User Story:** Como Pesquisador, quero ser conduzido passo a passo pelos 8 casos curados, para que eu consiga replicar os resultados dos colegas Roth e Sidnei.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL listar os 8 Casos_Curados: APT41-DUST, ATT&CK Campaign C0010, ATT&CK Campaign C0026, CostaRicto, Operation MidnightEclipse, Operation Outer Space, Salesforce Data Exfiltration e ShadowRay.
2. WHEN o Pesquisador seleciona um Caso_Curado, THE Pipeline_UI SHALL conduzir o Pesquisador pelos Estágios daquele Caso_Curado na ordem Estágio 1, depois Estágio 2 e depois Estágio 3.
3. THE Pipeline_UI SHALL indicar, para cada Estágio de cada Caso_Curado, o estado do Estágio entre os valores "não iniciado", "em andamento" e "concluído".
4. WHILE um Estágio de um Caso_Curado diferente do Estágio 1 possui seu Estágio imediatamente anterior em estado diferente de "concluído", THE Pipeline_UI SHALL sinalizar esse Estágio como bloqueado e SHALL impedir que o Pesquisador o inicie.
5. THE Pipeline_UI SHALL exibir o progresso agregado de replicação como a quantidade de Casos_Curados com os 3 Estágios em estado "concluído", em relação ao total de 8 Casos_Curados.
6. IF o par de arquivos ability/adversary de um Caso_Curado selecionado está ausente ou não pode ser lido, THEN THE Backend SHALL registrar a falha e THE Pipeline_UI SHALL exibir uma mensagem de erro descritiva identificando o Caso_Curado afetado, mantendo os demais Casos_Curados disponíveis para seleção.

### Requisito 6 — Contenção e isolamento de segurança

**User Story:** Como Pesquisador operando apenas com Docker Desktop, quero garantia de que a emulação fica contida no ambiente Docker isolado, para que os comandos de adversário nunca atinjam minha máquina nem alvos externos.

#### Critérios de Aceitação

1. THE Backend SHALL restringir toda execução de comando de adversário aos containers do Ambiente_Docker conectados às Redes_Internas.
2. IF uma Ability contém um comando cujo endereço de destino não pertence às Redes_Internas do Ambiente_Docker, THEN THE Backend SHALL recusar a execução dessa Ability, THE Backend SHALL impedir o início da Operação e THE Pipeline_UI SHALL exibir uma mensagem identificando a Ability recusada e o comando com destino externo.
3. WHEN o Pesquisador solicita uma emulação, THE Backend SHALL verificar, para cada container de destino da Operação, que ele está conectado somente a Redes_Internas (configuradas como internas, sem rota para o host e sem rota para a internet) antes de iniciar a Operação.
4. IF a verificação de isolamento de qualquer container de destino falha, THEN THE Backend SHALL abortar a Operação, THE Backend SHALL não executar nenhum comando de adversário e THE Pipeline_UI SHALL exibir uma mensagem de contenção não satisfeita identificando o container que falhou na verificação.
5. WHEN o Pesquisador solicita a execução de uma emulação, THE Pipeline_UI SHALL exibir, antes de qualquer confirmação, a lista completa dos comandos concretos que serão executados e o container de destino de cada comando.
6. WHEN o Pesquisador solicita a execução de uma emulação, THE Pipeline_UI SHALL requerer uma ação de confirmação explícita do Pesquisador antes de o Backend iniciar a Operação.
7. IF o Pesquisador não fornece a confirmação explícita ou cancela a solicitação, THEN THE Backend SHALL não iniciar a Operação e THE Pipeline_UI SHALL retornar ao estado anterior à solicitação sem executar nenhum comando de adversário.
8. WHILE uma Operação está em execução, THE Backend SHALL registrar cada comando executado, o container de destino e o resultado em um log de auditoria persistido na Base_de_Dados.
9. IF a persistência de um registro de auditoria na Base_de_Dados falha durante uma Operação, THEN THE Backend SHALL abortar a Operação e THE Pipeline_UI SHALL exibir uma mensagem indicando que a trilha de auditoria não pôde ser registrada.

### Requisito 7 — Tema claro e escuro com paleta blue/red/purple team

**User Story:** Como Pesquisador, quero alternar entre modo claro e modo escuro com uma identidade visual de blue/red/purple team, para que a interface fique agradável e alinhada ao domínio de segurança.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL oferecer dois temas selecionáveis e mutuamente exclusivos: um Modo_Claro corporativo com fundo de tonalidade clara e texto de tonalidade escura, e um Modo_Escuro de cultura hacker com fundo de tonalidade escura e texto de tonalidade clara.
2. WHEN o Pesquisador alterna o tema, THE Pipeline_UI SHALL aplicar o tema selecionado a todos os elementos visíveis da interface sem recarregar a página e sem exibir estado de carregamento intermediário.
3. THE Pipeline_UI SHALL aplicar, em ambos os temas, uma paleta em que elementos de contexto defensivo utilizam a cor associada ao blue team, elementos de contexto ofensivo ou de adversário utilizam a cor associada ao red team, e elementos de contexto combinado defensivo-ofensivo utilizam a cor associada ao purple team.
4. THE Pipeline_UI SHALL persistir a preferência de tema do Pesquisador no Estado_de_Sessão.
5. WHEN o Pesquisador retorna à Pipeline_UI e existe uma preferência de tema persistida, THE Pipeline_UI SHALL aplicar o último tema persistido do Pesquisador.
6. WHEN o Pesquisador acessa a Pipeline_UI pela primeira vez sem preferência de tema persistida, THE Pipeline_UI SHALL adotar o Modo_Claro como tema padrão.

### Requisito 8 — Internacionalização pt-BR e en

**User Story:** Como Pesquisador, quero usar a interface em português do Brasil ou em inglês, para que a ferramenta atenda a diferentes públicos.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL disponibilizar exatamente os Idiomas pt-BR e en como opções selecionáveis.
2. WHEN o Pesquisador seleciona um Idioma, THE Pipeline_UI SHALL exibir todo o texto de interface visível no Idioma selecionado e aplicar a mudança sem recarregar a página em até 2 segundos.
3. IF um texto de interface não possui tradução no Idioma selecionado, THEN THE Pipeline_UI SHALL exibir esse texto em pt-BR como Idioma de fallback.
4. THE Pipeline_UI SHALL persistir a preferência de Idioma do Pesquisador no Estado_de_Sessão.
5. WHEN o Pesquisador retorna à Pipeline_UI com uma preferência de Idioma persistida, THE Pipeline_UI SHALL aplicar o último Idioma persistido do Pesquisador.
6. WHEN o Pesquisador acessa a Pipeline_UI sem preferência de Idioma persistida, THE Pipeline_UI SHALL adotar pt-BR como Idioma padrão.
7. IF a preferência de Idioma persistida não corresponde a pt-BR nem a en, THEN THE Pipeline_UI SHALL adotar pt-BR como Idioma padrão.

### Requisito 9 — Persistência de estado e continuidade entre máquinas

**User Story:** Como Pesquisador que alterna entre máquinas e ferramentas, quero que meu progresso seja persistido, para que eu possa continuar de onde parei.

#### Critérios de Aceitação

1. THE Base_de_Dados SHALL persistir o Estado_de_Sessão, incluindo o Caso_Curado atual, os Estágios concluídos por Caso_Curado e, para cada Operação, o status de sucesso ou falha e a saída do comando de cada Ability executada.
2. WHEN o Pesquisador conclui um Estágio de um Caso_Curado, THE Backend SHALL persistir essa conclusão no Estado_de_Sessão antes de indicar a conclusão ao Pesquisador.
3. IF a persistência da conclusão de um Estágio na Base_de_Dados falha, THEN THE Backend SHALL preservar o Estado_de_Sessão anteriormente persistido sem alteração e THE Pipeline_UI SHALL exibir uma mensagem de erro indicando que a conclusão não foi salva.
4. WHEN o Pesquisador abre a Pipeline_UI em qualquer máquina, THE Pipeline_UI SHALL recuperar o Estado_de_Sessão persistido na Base_de_Dados e exibir o Caso_Curado atual e os Estágios concluídos por Caso_Curado já registrados.
5. WHEN o Pesquisador abre a Pipeline_UI e não existe Estado_de_Sessão persistido, THE Pipeline_UI SHALL exibir todos os Casos_Curados com todos os Estágios no estado "não iniciado".
6. IF o Estado_de_Sessão persistido está indisponível ou não pode ser lido quando o Pesquisador abre a Pipeline_UI, THEN THE Pipeline_UI SHALL exibir uma mensagem indicando que o progresso não pôde ser restaurado, sem descartar o Estado_de_Sessão persistido.
7. THE Backend SHALL persistir os resultados de cada Operação de forma associada ao Caso_Curado correspondente.

### Requisito 10 — Restrições não-funcionais de stack

**User Story:** Como Pesquisador, quero que a solução use tecnologias modernas e adequadas ao desenvolvimento assistido por IA, para que a manutenção e a evolução sejam produtivas.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL ser composta por uma camada de Frontend, uma camada de Backend e uma Base_de_Dados.
2. THE Frontend, THE Backend e THE Base_de_Dados SHALL ser implementados exclusivamente com tecnologias cuja versão adotada esteja em suporte oficial do mantenedor (não descontinuada / não em fim de vida) e que possuam ao menos uma versão publicada nos últimos 24 meses, com documentação pública oficial disponível.
3. WHILE o Pesquisador conduz os Casos_Curados, THE Pipeline_UI SHALL operar integralmente em uma instalação local de Docker Desktop sem realizar acesso de rede à internet para conduzir os Casos_Curados.
4. THE Backend SHALL integrar-se ao pipeline Python existente e às ferramentas de `sticks/tools/` para conduzir os Estágios.
5. IF a condução de um Caso_Curado requer um serviço acessível apenas fora da instalação local de Docker Desktop, THEN THE Backend SHALL interromper a condução desse Caso_Curado e THE Pipeline_UI SHALL exibir uma mensagem indicando qual dependência externa impediu a operação, preservando o Estado_de_Sessão já registrado.

### Requisito 11 — Extensibilidade futura do Estágio 2 (fora de escopo de implementação)

**User Story:** Como Pesquisador, quero que a interface do Estágio 2 seja projetada para futura assistência por agentes de IA, para que a próxima feature possa ser adicionada sem redesenhar a interface.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL estruturar a tela do Estágio 2 de modo que a origem da tradução seja um atributo explícito exibido na tela, cujo valor pertence a um conjunto extensível de origens, com "curadoria humana" como valor das traduções dos 8 Casos_Curados existentes.
2. WHERE uma futura origem de tradução assistida por agente de IA for adicionada ao conjunto de origens, THE Pipeline_UI SHALL exibir essa origem na tela do Estágio 2 sem exigir alteração do modelo de dados persistido de Ability e Adversary.
