# Requirements Document

## Introduction

O **Fluxo STIX + Dashboard** reorganiza a interface e o fluxo guiado do STICKS para que a jornada do Pesquisador comece pela inteligência bruta (os pacotes STIX do MITRE ATT&CK) e avance, passo a passo, até a emulação contida de uma campanha de adversário no laboratório Docker local.

A ferramenta STICKS acompanha a pesquisa sobre a "lacuna de semântica procedural em ATT&CK-in-STIX" — ou seja, medir se a inteligência CTI em STIX é procedimentalmente suficiente para reproduzir campanhas APT em um laboratório contido (MITRE Caldera + atacante Kali + alvos nginx/mariadb). Hoje a aplicação (`ui/`: frontend React+TS+Vite, backend FastAPI, Postgres) já expõe um wizard de 3 estágios: Estágio 1 (modelagem estrutural), Estágio 2 (tradução curada) e Estágio 3 (emulação de adversário contida).

Esta feature introduz uma nova sequência de experiência que antecede e envolve o wizard existente:

**Landing → Listagem de Bases STIX → Dashboard de Inteligência do pacote STIX → Seleção de Campanha/Ator → Geração/Visualização explícita do DAG → Tradução Curada → Emulação Contida.**

A mudança arquitetural central é tornar a **geração do DAG uma fase explícita e de primeira classe** do fluxo. Hoje essa geração está embutida no Estágio 1; a feature a separa em um passo próprio que: (a) carrega o DAG já curado quando a campanha já foi trabalhada por um colega (ex.: ShadowRay, com o arquivo `{slug}_dag.json` já em disco); (b) permite **visualizar** o DAG (grafo dirigido acíclico) antes de avançar, reutilizando a renderização Mermaid já existente na aplicação.

Os estágios já funcionais (tradução curada e emulação contida) e as garantias de contenção já implementadas (redes `internal: true`, confirmação explícita antes de execução real) **devem ser preservados integralmente**. A geração de DAG por Inteligência Artificial para casos ainda não curados é **não-objetivo desta iteração**: nesta versão o passo correspondente é um "passa-direto" (stub) que apenas sinaliza indisponibilidade, pois o primeiro alvo de validação é o fluxo curado do ShadowRay.

## Glossary

- **STICKS**: A ferramenta de pesquisa deste repositório que conduz do STIX à emulação contida.
- **STIX**: Formato estruturado de inteligência de ameaças (Structured Threat Information Expression). Aqui, a fonte é o `enterprise-attack` do MITRE ATT&CK.
- **Base_STIX** (ou Versão_STIX): Um pacote `enterprise-attack` de uma versão específica do MITRE ATT&CK (ex.: v18.x, v19.x), originado do repositório `mitre-attack/attack-stix-data`. As bases baixadas residem em `sticks/data/stix`.
- **Base_Baixada**: Uma Base_STIX cujo bundle já está presente em disco no sistema, pronta para análise.
- **Dashboard_Inteligencia**: Painel analítico que resume o conteúdo de uma Base_STIX selecionada (significado de cada campo e contagens por tipo de objeto).
- **Objeto_STIX**: Entidade do bundle STIX (ex.: campanha, técnica, tática, intrusion set, malware, relacionamento).
- **Campanha**: Objeto STIX do tipo campaign — uma campanha de adversário executável/selecionável (ex.: ShadowRay).
- **Ator_de_Ameaca** (Intrusion_Set): Objeto STIX do tipo intrusion-set — o grupo/ator associado a uma ou mais campanhas.
- **DAG**: Grafo dirigido acíclico derivado da inteligência abstrata STIX; representa a ordem procedimental de execução de uma Campanha. Em disco: `sticks/data/dag/{slug}_dag.json`.
- **Caso_Curado**: Campanha para a qual o DAG já foi produzido manualmente por um pesquisador (os 8 casos: apt41_dust, c0010, c0026, costaricto, operation_midnighteclipse, outer_space, salesforce_data_exfiltration, shadowray).
- **Geracao_DAG**: Fase explícita do fluxo que obtém o DAG de uma Campanha (carregando o curado ou, no futuro, gerando por IA) e permite sua visualização.
- **Geracao_IA_DAG**: Geração automática do DAG por Inteligência Artificial para casos não curados — não-objetivo desta iteração.
- **Ability**: Unidade executável do Caldera correspondente a uma técnica da Campanha. Em disco: `sticks/data/api/{slug}_dag-ability.json`.
- **Adversary**: Perfil de adversário do Caldera que agrega as Abilities da Campanha. Em disco: `sticks/data/api/{slug}_dag-adversary.json`.
- **Wizard**: Fluxo guiado existente que conduz o Pesquisador sequencialmente pelos estágios de tradução curada e emulação.
- **Emulacao_Contida**: Execução real das Abilities contra o Caldera dentro do laboratório Docker local isolado.
- **Contencao** (Isolamento): Garantia inegociável de que o ataque nunca sai do ambiente controlado — redes de ataque com `internal: true` e confirmação explícita antes de execução real.
- **Mermaid**: Capacidade de renderização de diagramas já presente no frontend (`MermaidDiagram`), usada com `securityLevel: "strict"`, reutilizada para visualizar o DAG.

## Requirements

### Requirement 1: Tela inicial (Landing)

**User Story:** Como Pesquisador, quero uma tela inicial atraente com a identidade do STICKS e um convite claro para iniciar o fluxo, para entrar na jornada de emulação de campanha de forma orientada.

#### Acceptance Criteria

1. WHEN o Pesquisador abre a aplicação, THE STICKS SHALL exibir a Landing como ponto de entrada, contendo a marca STICKS e uma mensagem de chamada à ação que convida o Pesquisador a executar o fluxo de campanha de adversário.
2. THE Landing SHALL apresentar um controle de início que conduz o Pesquisador à Listagem de Bases STIX.
3. WHEN o Pesquisador aciona o controle de início da Landing, THE STICKS SHALL exibir a Listagem de Bases STIX.
4. WHILE a Landing é exibida ou dela se transita para a Listagem de Bases STIX, THE STICKS SHALL aplicar transições de animação com duração não superior a 600 ms por transição.
5. WHERE o Pesquisador possui preferência de redução de movimento habilitada no sistema, THE STICKS SHALL exibir a Landing sem animações de transição.

### Requirement 2: Listagem de Bases STIX

**User Story:** Como Pesquisador, quero ver as versões do MITRE ATT&CK (bases STIX) ordenadas da mais nova para a mais antiga, sabendo quais já estão baixadas, para escolher a base que vou analisar.

#### Acceptance Criteria

1. WHEN a Listagem de Bases STIX é exibida, THE STICKS SHALL apresentar as Bases_STIX ordenadas da versão mais nova para a mais antiga.
2. WHERE uma Base_STIX já está presente em disco, THE STICKS SHALL marcar essa Base_STIX como "já baixada" e distingui-la visualmente das bases não baixadas.
3. WHEN a Listagem de Bases STIX é exibida, THE STICKS SHALL habilitar a seleção apenas das Bases_Baixadas para análise.
4. THE STICKS SHALL oferecer um controle de download para Bases_STIX ainda não presentes em disco.
5. WHEN o Pesquisador aciona o download de uma Base_STIX não presente, THE STICKS SHALL obter o bundle `enterprise-attack` da versão correspondente a partir do repositório `mitre-attack/attack-stix-data` e persistir o bundle em `sticks/data/stix`.
6. WHEN o download de uma Base_STIX conclui com sucesso, THE STICKS SHALL marcar essa Base_STIX como "já baixada" na Listagem de Bases STIX.
7. IF o download de uma Base_STIX falha, THEN THE STICKS SHALL exibir uma mensagem de erro descritiva e manter a Base_STIX marcada como não baixada.
8. WHEN o Pesquisador seleciona uma Base_Baixada, THE STICKS SHALL exibir o Dashboard de Inteligência dessa Base_STIX.

### Requirement 3: Dashboard de Inteligência do pacote STIX

**User Story:** Como Pesquisador, quero um dashboard analítico do conteúdo da base STIX selecionada, com contagens por tipo de objeto e o significado de cada campo, para entender o pacote antes de escolher uma campanha.

#### Acceptance Criteria

1. WHEN o Dashboard de Inteligência de uma Base_STIX é exibido, THE STICKS SHALL apresentar a identificação da Base_STIX, incluindo a versão do MITRE ATT&CK analisada.
2. WHEN o Dashboard de Inteligência de uma Base_STIX é exibido, THE STICKS SHALL apresentar a contagem de Objetos_STIX por tipo, incluindo ao menos: campanhas, técnicas, táticas, intrusion sets, malware e relacionamentos.
3. WHEN o Dashboard de Inteligência de uma Base_STIX é exibido, THE STICKS SHALL apresentar, para cada tipo de Objeto_STIX contabilizado, uma descrição do significado desse tipo de informação.
4. WHEN o Dashboard de Inteligência deriva suas contagens, THE STICKS SHALL computar os valores a partir do bundle da Base_STIX selecionada.
5. IF o bundle da Base_STIX selecionada não pode ser lido ou está malformado, THEN THE STICKS SHALL exibir uma mensagem de erro descritiva e não apresentar contagens parciais como se fossem completas.
6. THE Dashboard de Inteligência SHALL oferecer um controle que conduz o Pesquisador à Seleção de Campanha/Ator da mesma Base_STIX.

### Requirement 4: Seleção de Campanha ou Ator de Ameaça

**User Story:** Como Pesquisador, quero listar e selecionar o que vou executar a partir da base STIX, alternando entre uma visão por campanha e uma visão por ator de ameaça, para escolher o alvo da emulação da forma que me for mais clara.

#### Acceptance Criteria

1. WHEN a Seleção de Campanha/Ator de uma Base_STIX é exibida, THE STICKS SHALL oferecer um controle de alternância entre a visão por Campanha e a visão por Ator_de_Ameaca.
2. WHILE a visão por Campanha está ativa, THE STICKS SHALL listar as Campanhas presentes na Base_STIX selecionada.
3. WHILE a visão por Ator_de_Ameaca está ativa, THE STICKS SHALL listar os Atores_de_Ameaca presentes na Base_STIX selecionada e, para cada Ator_de_Ameaca, as Campanhas a ele associadas.
4. WHEN o Pesquisador seleciona uma Campanha, THE STICKS SHALL conduzir o Pesquisador à fase de Geração/Visualização do DAG dessa Campanha.
5. WHERE uma Campanha listada é um Caso_Curado, THE STICKS SHALL distinguir visualmente essa Campanha como já curada.

### Requirement 5: Geração do DAG como fase explícita

**User Story:** Como Pesquisador, quero que a geração do DAG seja uma fase própria do fluxo, para separar explicitamente a derivação do grafo procedimental da modelagem estrutural subsequente.

#### Acceptance Criteria

1. WHEN o Pesquisador avança a partir da Seleção de Campanha/Ator com uma Campanha selecionada, THE STICKS SHALL exibir a fase de Geração do DAG como um passo distinto, anterior à fase de tradução curada.
2. WHERE a Campanha selecionada é um Caso_Curado, THE STICKS SHALL carregar o DAG já produzido a partir de `sticks/data/dag/{slug}_dag.json` e exibi-lo nessa fase.
3. IF o arquivo `sticks/data/dag/{slug}_dag.json` de um Caso_Curado não pode ser lido ou está malformado, THEN THE STICKS SHALL exibir uma mensagem de erro descritiva e manter o avanço para a fase seguinte desabilitado.
4. WHERE a Campanha selecionada não é um Caso_Curado, THE STICKS SHALL exibir, nesta fase, um estado de "passa-direto" indicando que a geração do DAG ainda não está disponível para esta Campanha.
5. WHILE a Campanha selecionada não é um Caso_Curado, THE STICKS SHALL manter o avanço para a fase de tradução curada desabilitado.

### Requirement 6: Visualização do DAG

**User Story:** Como Pesquisador, quero visualizar o grafo dirigido acíclico da campanha nesta fase, para inspecionar a ordem procedimental antes de prosseguir.

#### Acceptance Criteria

1. WHEN um DAG de um Caso_Curado é carregado na fase de Geração do DAG, THE STICKS SHALL renderizar o DAG como um grafo dirigido acíclico utilizando a renderização Mermaid existente.
2. WHEN o STICKS renderiza o DAG via Mermaid, THE STICKS SHALL utilizar o modo `securityLevel: "strict"`, sem executar scripts embutidos na definição do diagrama.
3. WHEN o DAG de um Caso_Curado foi renderizado com sucesso, THE STICKS SHALL habilitar o avanço para a fase de tradução curada da mesma Campanha.
4. IF a renderização do DAG falha, THEN THE STICKS SHALL exibir uma mensagem de erro descritiva e manter o avanço para a fase de tradução curada desabilitado.

### Requirement 7: Preservação do fluxo guiado passo a passo

**User Story:** Como Pesquisador, quero continuar avançando pelas fases de tradução curada e emulação uma de cada vez, após visualizar o DAG, para manter a condução linear e previsível já existente.

#### Acceptance Criteria

1. WHEN o Pesquisador avança a partir da fase de Visualização do DAG de um Caso_Curado, THE STICKS SHALL exibir a fase de tradução curada da mesma Campanha, reutilizando o Wizard existente.
2. THE STICKS SHALL preservar o avanço guiado uma fase de cada vez, com controles "Avançar" e "Voltar", para as fases de tradução curada e emulação.
3. THE STICKS SHALL preservar o bloqueio sequencial existente, pelo qual uma fase só inicia se a fase anterior estiver concluída.
4. WHEN o Pesquisador retorna da fase de tradução curada para a fase anterior, THE STICKS SHALL exibir novamente a fase de Visualização do DAG da mesma Campanha.

### Requirement 8: Contenção e isolamento preservados

**User Story:** Como Pesquisador responsável, quero a garantia de que toda emulação permanece isolada no meu Docker local, para que o ataque nunca escape do ambiente controlado.

#### Acceptance Criteria

1. THE STICKS SHALL executar toda Emulacao_Contida exclusivamente no ambiente Docker local isolado do Pesquisador.
2. WHILE as redes do plano de ataque do laboratório estão configuradas, THE STICKS SHALL manter essas redes com `internal: true`, sem rota para o host ou para a internet.
3. WHEN o Pesquisador inicia uma Emulacao_Contida, THE STICKS SHALL exigir confirmação explícita do Pesquisador antes de qualquer execução real.
4. IF a verificação de contenção não confirma o isolamento das redes de ataque, THEN THE STICKS SHALL recusar a Emulacao_Contida e exibir o motivo da recusa.
5. THE STICKS SHALL preservar as garantias de contenção já implementadas, sem reduzir o nível de isolamento vigente ao introduzir as novas fases.

### Requirement 9: Não-objetivos desta iteração

**User Story:** Como mantenedor, quero que os não-objetivos desta iteração sejam explícitos, para evitar escopo indevido antes de validar o fluxo curado do ShadowRay.

#### Acceptance Criteria

1. THE STICKS SHALL tratar a Geracao_IA_DAG para Campanhas não curadas como não-objetivo desta iteração, exibindo o passo correspondente apenas como "passa-direto".
2. WHEN uma Campanha não curada alcança a fase de Geração do DAG nesta iteração, THE STICKS SHALL sinalizar indisponibilidade da geração automática sem invocar qualquer serviço de Inteligência Artificial.
3. THE STICKS SHALL preservar sem reescrita as telas de tradução curada e emulação já existentes e o modal de confirmação de emulação.

### Requirement 10: Preservação da suíte de testes

**User Story:** Como mantenedor, quero que a feature não quebre os testes existentes e adicione cobertura para os novos pontos, para manter a confiança na suíte.

#### Acceptance Criteria

1. THE suíte de testes do Backend SHALL continuar passando integralmente após a adição das novas capacidades de listagem/download de Bases_STIX, Dashboard de Inteligência e Geração do DAG.
2. THE suíte de testes do Frontend SHALL continuar passando integralmente após a adição da Landing, da Listagem de Bases STIX, do Dashboard de Inteligência e das fases de Geração e Visualização do DAG.
3. THE suíte de testes SHALL incluir cobertura para as contagens do Dashboard de Inteligência, para o carregamento do DAG de um Caso_Curado e para o estado de "passa-direto" de uma Campanha não curada.
