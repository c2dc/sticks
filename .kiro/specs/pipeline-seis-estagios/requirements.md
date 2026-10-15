# Requirements Document

## Introduction

A ferramenta de pesquisa **STICKS** avalia a **completude procedural** de inteligência de ameaças em STIX e a capacidade dessa inteligência de **reproduzir campanhas APT** em um ambiente de laboratório contido. A **Pipeline_UI** conduz uma campanha curada através de Estágios ordenados, com Entrada/Processamento/Saída explícitos por Estágio e bloqueio sequencial entre eles.

Hoje o pipeline tem **cinco Estágios** com os papéis definidos em `pipeline-cinco-estagios` (1 Modelagem Estrutural, 2 Geração do DAG, 3 Operacionalização, 4 Provisionamento do Ambiente, 5 Execução da Campanha), implementados em `ui/backend/app/services/stage/stage_service.py` com bloqueio sequencial (um Estágio só inicia quando o imediatamente anterior está `concluido`).

Esta spec **reestrutura o pipeline para seis Estágios**, redistribuindo os papéis para refletir o fluxo real de dados que o Pesquisador segue ao reproduzir uma campanha curada:

1. **Seleção da base STIX** — o Pesquisador escolhe a versão do pacote STIX; o sistema baixa o pacote da fonte oficial do MITRE se ainda não houver cópia em disco e apresenta um dashboard do bundle. Nenhuma versão vem baixada por padrão. Promove a seleção de versão a Estágio formal.
2. **Seleção da campanha** — lista as campanhas contidas no bundle; o Pesquisador escolhe uma campanha para emular; a saída é a **extração dos atributos** dessa campanha. Promove a seleção de campanha a Estágio formal, distinto da seleção de versão.
3. **Geração do DAG** — produz o Grafo Acíclico Dirigido que define quais técnicas ocorrem e em que ordem/dependência, a partir dos atributos da campanha.
4. **Operacionalização** — cria as Abilities (comandos que operacionalizam as técnicas) na ordem estipulada pelo DAG, e produz os parâmetros de ambiente exigidos por esses comandos.
5. **Criação do ambiente de simulação** — cria o ambiente (apenas contêineres neste primeiro momento) respeitando os parâmetros de ambiente recebidos.
6. **Execução da campanha, medição e relatório** — executa a campanha no ambiente simulado isolado, mede, gera o relatório e desprovisiona o ambiente ao final.

Esta é uma **solução de transição**. Para as **8 campanhas curadas**, os Estágios 3 (DAG), 4 (comandos/Abilities) e 5 (ambiente) consomem os **artefatos curados** por um colaborador humano, lidos **diretamente de disco**, sem invocar IA e sem acesso à rede no ambiente de desenvolvimento. As **8 campanhas curadas** existem **somente na versão 18** do pacote STIX, que é marcada com uma **flag de versão curada** indicando que os casos curados do colaborador estão nela. Esse uso de artefatos curados é **transitório**: hoje o fluxo completo roda ponta a ponta com os artefatos curados; no futuro, um **agente de IA** produzirá o DAG, selecionará/parametrizará os comandos (a partir da base do Atomic Red Team ou de outra base) e montará o ambiente sob medida, e a curadoria será **removida** quando o agente existir. O desenho deve acomodar essa substituição **sem redesenho**, por meio de um ponto de extensão por `origem` (`curadoria_humana` hoje, `gerado_ia` no futuro), seguindo o precedente já existente no código (`GeradorDeDag`; `TranslationSource` / `origem_traducao`). A implementação do agente de IA é **não-objetivo** desta spec.

O **isolamento/contenção** do ambiente é parte central e inegociável do sistema: os comandos de ataque **nunca** executam na máquina local do Pesquisador; são simulados **somente** dentro do ambiente provisionado e isolado. As proteções já implementadas no pacote `ui/backend/app/services/containment/` (parser de destinos, classificação interno/externo contra as três sub-redes internas do laboratório, verificação de isolamento dos contêineres e preview com confirmação) **devem ser preservadas** e podem ser melhoradas.

A aplicação vive em `ui/`: `ui/backend` (FastAPI + SQLAlchemy + Alembic + **PostgreSQL exclusivamente**) e `ui/frontend` (React 19 + TypeScript + Vite + i18next pt-BR). O MITRE Caldera executa as Abilities dentro do ambiente contido.

Esta spec **constrói sobre** e **supersede** a ordenação e os papéis de Estágio definidos em `pipeline-cinco-estagios`.

### Justificativa de ordem (contexto, já validada — não é requisito funcional)

- **O DAG precede a operacionalização** porque o DAG define a ordem e as dependências entre as técnicas da campanha. O Atomic Red Team fornece o comando **por técnica**, mas não o **encadeamento** da campanha; esse encadeamento vem do DAG.
- **A operacionalização precede a criação do ambiente** porque é o comando que dita os requisitos do ambiente. Nos testes atômicos, os campos `dependencies` / `get_prereq_command`, `input_arguments` (alvos) e `supported_platforms` são exatamente os "parâmetros de ambiente" que o Estágio 5 consome.

## Glossary

- **STICKS**: Ferramenta de pesquisa que avalia a completude procedural de inteligência STIX e sua capacidade de reproduzir campanhas APT em laboratório contido. Compreende `ui/backend` (FastAPI + SQLAlchemy + Alembic + PostgreSQL) e `ui/frontend` (React/TypeScript).
- **Pipeline_UI**: A aplicação web que conduz uma campanha curada através dos Estágios ordenados, com Entrada/Processamento/Saída explícitos e bloqueio sequencial.
- **STIX**: Formato padronizado de inteligência de ameaças (CTI). Lista **quais** técnicas ATT&CK uma campanha usou, sem registrar a ordem procedural entre elas.
- **ATT&CK**: Base de conhecimento de táticas e técnicas adversárias do MITRE, referenciada pelas campanhas STIX por identificadores de técnica (ex.: `T1102`).
- **Fonte oficial do STIX ATT&CK**: Repositório oficial do MITRE no GitHub `mitre-attack/attack-stix-data`, que publica os bundles STIX 2.1 do `enterprise-attack` por versão. É a fonte de download dos pacotes STIX no Estágio 1.
- **Pacote STIX / Bundle STIX**: Arquivo de inteligência de uma versão específica (ex.: versão 18, 19). Contém uma ou mais campanhas. Materializado em disco quando baixado da fonte oficial. Nenhuma versão vem baixada por padrão.
- **Versão da base STIX**: Identificador da versão do pacote STIX que o Pesquisador escolhe no Estágio 1 (ex.: 18, 19).
- **Flag de versão curada**: Marcação/sinalização associada a uma versão do pacote STIX indicando que os casos curados do colaborador estão nela. Nesta primeira entrega, a **versão 18** é marcada com essa flag; o apontamento de onde estão os artefatos curados fica associado à versão 18.
- **Dashboard do bundle**: Visão geral do pacote STIX selecionado (contagens por tipo, campanhas disponíveis e demais metadados do bundle) exibida no Estágio 1.
- **Campanha**: Unidade de inteligência descrita por um pacote STIX, identificada por um `slug`. Pode ser emulada. As campanhas curadas são enumeradas em `CURATED_CASES` e mapeadas em `SLUG_PARA_STIX`.
- **Campanha curada**: Uma das 8 campanhas (ex.: `ShadowRay`) cujo material (DAG, comandos/Abilities e ambiente) já foi produzido por **curadoria humana** e existe em disco. Está presente **somente na versão 18**. Lida sem IA e sem rede.
- **Curadoria humana / artefatos curados**: Mecanismo de **transição** no qual o DAG, as Abilities e o ambiente de uma campanha são produzidos por um colaborador humano e lidos de disco. Será **removido** quando o agente de IA existir.
- **Atributos da campanha**: Os dados extraídos de uma campanha selecionada (ex.: identidade, aliases, técnicas associadas, metadados) — a Saída do Estágio 2.
- **DAG (Grafo Acíclico Dirigido)**: Representação ordenada das técnicas de uma campanha e de suas dependências procedurais. **Não existe** no pacote STIX; precisa ser **produzido**. É a Saída do Estágio 3.
- **Abilities**: Os comandos concretos que operacionalizam a execução das técnicas, ordenados conforme o DAG, carregáveis no Caldera. Compõem parte da Saída do Estágio 4.
- **Parâmetros de ambiente**: Pré-requisitos para a criação do ambiente de simulação, derivados dos comandos (ex.: hosts-alvo, serviços/vulnerabilidades a provisionar, plataforma). Compõem a segunda parte da Saída do Estágio 4.
- **Ambiente de simulação**: O ambiente contido onde a campanha é executada, com as Abilities carregadas no Caldera. Neste primeiro momento usa apenas **contêineres**. É a Saída do Estágio 5.
- **Contêiner / Container**: Unidade de ambiente baseada em Docker usada para a simulação neste primeiro momento. (Máquinas virtuais são possibilidade futura, fora do escopo.)
- **Isolamento de contêiner**: Condição em que um contêiner-alvo está anexado **exclusivamente** a redes marcadas `internal: true`, sem bridge externo (ex.: `local-network`, `bridge`, `host`) e sem DNS externo configurado. Verificada por inspeção read-only do Docker (`IsolationResult`, com veredito por contêiner e um resultado geral pass/fail).
- **Sub-redes internas do laboratório**: As três redes `internal: true` definidas no `docker-compose`, fonte única em `ui/backend/app/services/containment/subnets.py`: `192.168.10.0/24` (`caldera-kali-network`), `192.168.20.0/24` (`kali-nginx-network`) e `192.168.30.0/24` (`nginx-db-network`).
- **Destino interno / externo**: Classificação de cada endereço de destino (IP/host/URL) extraído de um comando: **interno** quando é um IP contido em uma das sub-redes internas; **externo** caso contrário (inclusive hostnames, por padrão seguro, sem resolução de DNS).
- **ContainmentValidator / Contenção**: Conjunto de proteções já implementadas em `ui/backend/app/services/containment/` que garantem o isolamento: `command_parser` (extrai os destinos dos comandos), `subnets` (fonte única das sub-redes internas), `subnet_validator` (classifica interno/externo e produz `AbilityContainmentReport`), `isolation` (verifica isolamento dos contêineres por inspeção read-only e produz `IsolationResult`) e `preview` (lista comandos com contêiner-alvo e sinaliza destinos externos).
- **Preview de execução**: Lista completa e concreta dos comandos com o contêiner-alvo de cada um, exibida em um modal de confirmação antes de qualquer execução, sinalizando quaisquer destinos externos.
- **Desprovisionamento / Teardown**: Limpeza/remoção do ambiente provisionado após a execução da campanha, para não esgotar os recursos de hardware.
- **Métricas de suficiência procedural**: Conjunto proposto de medições que indicam o quanto a inteligência STIX é procedural­mente suficiente para reproduzir a campanha (ver Requisito 15). É uma proposta inicial a refinar.
- **Caldera**: O MITRE Caldera, plataforma que executa as Abilities dentro do ambiente contido no Estágio 6.
- **Relatório**: O documento de resultados da execução da campanha, com as medições consolidadas, produzido no Estágio 6.
- **Estágio**: Etapa persistida e ordenada do pipeline de uma campanha, com Entrada, Processamento e Saída explícitos. Cada Estágio é apresentado como uma **tela**. O pipeline passa a ter **seis** Estágios (1 a 6).
- **Próximo / Voltar**: Controles de navegação entre as telas dos Estágios. "Próximo" avança para o Estágio seguinte; "Voltar" retorna ao Estágio anterior para permitir alterar a seleção.
- **Entrada / Saída**: Os artefatos consumidos (Entrada) e produzidos (Saída) por um Estágio. A Saída de um Estágio é a Entrada do Estágio seguinte.
- **Origem**: Atributo explícito e extensível que indica **como** um artefato de Estágio (DAG, Abilities, ambiente) foi produzido: `curadoria_humana` (hoje) ou `gerado_ia` (futuro). Segue o precedente de `GeradorDeDag` e de `TranslationSource` / `origem_traducao` no código.
- **GeradorDeDag**: Costura (interface/serviço) de extensão no backend que produz o DAG de uma campanha. Possui implementação de **curadoria** agora (lê o DAG de disco) e admite implementação de **IA** encaixável depois, sem redesenho.
- **Atomic Red Team**: Base de testes atômicos (comando por técnica ATT&CK) que o futuro agente de IA usará como fonte de comandos no Estágio 4. Fonte dos campos `dependencies` / `get_prereq_command`, `input_arguments` e `supported_platforms`.
- **Agente de IA**: Componente futuro que produzirá o DAG (Estágio 3), selecionará/parametrizará ou criará comandos (Estágio 4) e montará o ambiente (Estágio 5), e que poderá conduzir ciclos de repetição no Estágio 6. Sua implementação é **não-objetivo** desta spec.
- **Pesquisador**: Pessoa usuária da ferramenta STICKS.
- **Bloqueio sequencial**: Regra que mantém um Estágio bloqueado enquanto o Estágio imediatamente anterior não está `concluido`.

## Requirements

### Requisito 1: Pipeline de seis Estágios na ordem definida

**História de usuário:** Como Pesquisador, quero que o pipeline tenha seis Estágios ordenados que reflitam o fluxo real de reprodução de uma campanha, para que cada etapa tenha Entrada e Saída claras e eu acompanhe o progresso de ponta a ponta.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL definir exatamente seis Estágios ordenados: Estágio 1 "Seleção da base STIX", Estágio 2 "Seleção da campanha", Estágio 3 "Geração do DAG", Estágio 4 "Operacionalização", Estágio 5 "Criação do ambiente de simulação" e Estágio 6 "Execução da campanha, medição e relatório".
2. THE Pipeline_UI SHALL identificar cada Estágio por um número no intervalo de 1 a 6.
3. THE Estágio 1 "Seleção da base STIX" SHALL ser o ponto de partida do pipeline e NÃO depender de nenhum Estágio anterior.
4. THE Pipeline_UI SHALL registrar, para cada Estágio, a sua Entrada e a sua Saída de modo explícito.
5. WHEN o Backend calcula o progresso agregado de replicação de uma campanha, THE Backend SHALL contar a campanha como totalmente concluída apenas quando os seis Estágios dessa campanha estiverem no estado `concluido`.

### Requisito 2: Encadeamento Entrada → Saída entre Estágios

**História de usuário:** Como Pesquisador, quero que a Saída de cada Estágio seja a Entrada do Estágio seguinte, para que a dependência de dados entre Estágios seja explícita e rastreável.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL definir a Saída de cada Estágio no intervalo de 1 a 5 como a Entrada do Estágio imediatamente seguinte.
2. THE Entrada do Estágio 2 "Seleção da campanha" SHALL ser o bundle STIX selecionado no Estágio 1.
3. THE Entrada do Estágio 3 "Geração do DAG" SHALL ser os atributos da campanha selecionada no Estágio 2.
4. THE Entrada do Estágio 4 "Operacionalização" SHALL ser o DAG produzido no Estágio 3.
5. THE Entrada do Estágio 5 "Criação do ambiente de simulação" SHALL ser as Abilities ordenadas e os parâmetros de ambiente produzidos no Estágio 4.
6. THE Entrada do Estágio 6 "Execução da campanha, medição e relatório" SHALL ser o ambiente de simulação produzido no Estágio 5.

### Requisito 3: Bloqueio sequencial entre Estágios

**História de usuário:** Como Pesquisador, quero que um Estágio só inicie quando o imediatamente anterior estiver concluído, para que eu nunca execute uma etapa sem a Entrada que ela exige.

#### Critérios de Aceitação

1. WHILE o Estágio imediatamente anterior de uma campanha não estiver no estado `concluido`, THE Pipeline_UI SHALL manter o Estágio seguinte dessa campanha no estado bloqueado, para cada par de Estágios adjacentes de 1 a 6.
2. WHEN o Estágio anterior de uma campanha passa ao estado `concluido`, THE Pipeline_UI SHALL liberar o início do Estágio imediatamente seguinte dessa campanha.
3. IF o Pesquisador tenta iniciar um Estágio cujo Estágio imediatamente anterior não está `concluido`, THEN THE Backend SHALL recusar o início e manter o Estágio no estado bloqueado.

### Requisito 4: Navegação entre as telas dos Estágios (Próximo / Voltar)

**História de usuário:** Como Pesquisador, quero avançar e retornar entre as telas dos Estágios com botões "Próximo" e "Voltar", para que eu possa revisar e alterar seleções anteriores de forma controlada.

#### Critérios de Aceitação

1. THE Pipeline_UI SHALL apresentar cada Estágio como uma tela distinta.
2. THE Pipeline_UI SHALL fornecer um controle "Próximo" para avançar da tela do Estágio atual para a tela do Estágio imediatamente seguinte.
3. THE Pipeline_UI SHALL fornecer um controle "Voltar" para retornar da tela do Estágio atual para a tela do Estágio imediatamente anterior.
4. WHILE o Pesquisador não acionar "Voltar", THE Pipeline_UI SHALL manter inalterada a seleção já confirmada pelo controle "Próximo" no Estágio atual.
5. WHEN o Pesquisador aciona "Voltar", altera a seleção de um Estágio e aciona "Próximo" novamente, THE Pipeline_UI SHALL adotar a nova seleção como válida para os Estágios seguintes.
6. WHEN uma seleção de um Estágio anterior é alterada após "Voltar", THE Backend SHALL recalcular os Estágios seguintes a partir da nova seleção.

### Requisito 5: Estágio 1 — Seleção da base STIX

**História de usuário:** Como Pesquisador, quero escolher a versão do pacote STIX e ver um dashboard do bundle, para que eu parta de uma base de inteligência conhecida antes de escolher a campanha.

#### Critérios de Aceitação

1. THE Estágio 1 "Seleção da base STIX" SHALL ter como Entrada a versão do pacote STIX escolhida pelo Pesquisador.
2. WHEN o Pesquisador seleciona uma versão do pacote STIX e o pacote dessa versão não está disponível em disco, THE Backend SHALL baixar o pacote STIX dessa versão a partir da fonte oficial do MITRE `mitre-attack/attack-stix-data`.
3. WHEN o Pesquisador seleciona uma versão do pacote STIX e o pacote dessa versão já está disponível em disco, THE Backend SHALL reutilizar o pacote em disco sem baixá-lo novamente.
4. THE Backend SHALL iniciar sem nenhum pacote STIX baixado por padrão.
5. THE Backend SHALL marcar a versão 18 do pacote STIX com uma flag de versão curada que indica que os casos curados do colaborador estão nessa versão.
6. THE Backend SHALL associar o apontamento dos artefatos curados à versão 18 por meio da flag de versão curada.
7. WHEN o pacote STIX da versão selecionada está disponível em disco, THE Pipeline_UI SHALL exibir um dashboard com as informações do bundle selecionado.
8. THE Estágio 1 "Seleção da base STIX" SHALL ter como Saída o bundle STIX disponível em disco e o dashboard do bundle.
9. IF o download do pacote STIX da versão selecionada falha, THEN THE Backend SHALL manter o Estágio 1 não concluído e reportar a causa da falha ao Pesquisador.
10. WHEN o bundle STIX da versão selecionada está disponível e o dashboard foi exibido, THE Backend SHALL permitir concluir o Estágio 1.

### Requisito 6: Estágio 2 — Seleção da campanha

**História de usuário:** Como Pesquisador, quero listar as campanhas do bundle selecionado e escolher uma para emular, para que o pipeline extraia os atributos daquela campanha.

#### Critérios de Aceitação

1. THE Estágio 2 "Seleção da campanha" SHALL ter como Entrada o bundle STIX selecionado no Estágio 1.
2. WHEN o Pesquisador abre o Estágio 2, THE Backend SHALL listar as campanhas contidas no bundle selecionado.
3. WHEN o Pesquisador seleciona uma campanha da lista, THE Backend SHALL extrair os atributos dessa campanha.
4. THE Estágio 2 "Seleção da campanha" SHALL ter como Saída os atributos extraídos da campanha selecionada.
5. WHERE o bundle selecionado é a versão 18 marcada com a flag de versão curada, THE Pipeline_UI SHALL permitir a seleção de uma das 8 campanhas curadas.
6. WHEN o Pesquisador seleciona uma campanha e os atributos são extraídos com sucesso, THE Backend SHALL permitir concluir o Estágio 2.
7. IF a extração dos atributos da campanha selecionada falha, THEN THE Backend SHALL manter o Estágio 2 não concluído e reportar a causa da falha ao Pesquisador.

### Requisito 7: Estágio 3 — Geração do DAG

**História de usuário:** Como Pesquisador, quero gerar o DAG a partir dos atributos da campanha, para que fique definido quais técnicas ocorrem e em que ordem e dependência.

#### Critérios de Aceitação

1. THE Estágio 3 "Geração do DAG" SHALL ter como Entrada os atributos da campanha selecionada no Estágio 2.
2. THE Estágio 3 "Geração do DAG" SHALL produzir o DAG que define quais técnicas da campanha ocorrem e em que ordem e dependência.
3. THE Estágio 3 "Geração do DAG" SHALL ter como Saída o DAG pronto.
4. WHEN a campanha selecionada é uma campanha curada, THE Backend SHALL produzir o DAG lendo o DAG curado diretamente de disco.
5. THE Estágio 3 "Geração do DAG" SHALL registrar a origem do DAG de forma explícita e extensível, com o valor `curadoria_humana` para o DAG lido de disco.
6. WHEN o DAG da campanha é produzido com sucesso, THE Backend SHALL permitir concluir o Estágio 3.

### Requisito 8: Estágio 4 — Operacionalização e parâmetros de ambiente

**História de usuário:** Como Pesquisador, quero que a operacionalização crie as Abilities na ordem do DAG e produza os parâmetros de ambiente que os comandos exigem, para que o Estágio seguinte receba tanto o que executar quanto o que o ambiente precisa oferecer.

#### Critérios de Aceitação

1. THE Estágio 4 "Operacionalização" SHALL ter como Entrada o DAG produzido no Estágio 3.
2. THE Estágio 4 "Operacionalização" SHALL criar as Abilities que operacionalizam a execução das técnicas na ordem estipulada pelo DAG.
3. THE Estágio 4 "Operacionalização" SHALL produzir os parâmetros de ambiente necessários para a criação do ambiente de simulação, derivados dos comandos das Abilities.
4. THE Estágio 4 "Operacionalização" SHALL produzir as Abilities ordenadas e os parâmetros de ambiente como dois artefatos tipados separados, destinados a consumidores distintos (as Abilities ordenadas para o Caldera no Estágio 6; os parâmetros de ambiente para o Estágio 5).
5. WHEN a campanha selecionada é uma campanha curada, THE Backend SHALL criar as Abilities a partir dos comandos curados lidos diretamente de disco para essa campanha.
6. THE Estágio 4 "Operacionalização" SHALL registrar a origem das Abilities de forma explícita e extensível, com o valor `curadoria_humana` para os comandos lidos de disco.
7. THE Estágio 4 "Operacionalização" SHALL ter como Saída as Abilities ordenadas e os parâmetros de ambiente.
8. WHEN as Abilities ordenadas e os parâmetros de ambiente são produzidos com sucesso, THE Backend SHALL permitir concluir o Estágio 4.

### Requisito 9: Estágio 5 — Criação do ambiente de simulação

**História de usuário:** Como Pesquisador, quero criar o ambiente de simulação respeitando os parâmetros recebidos da operacionalização, para que a campanha execute em um ambiente adequado e contido, com as Abilities carregadas no Caldera.

#### Critérios de Aceitação

1. THE Estágio 5 "Criação do ambiente de simulação" SHALL ter como Entrada as Abilities ordenadas e os parâmetros de ambiente produzidos no Estágio 4.
2. THE Estágio 5 "Criação do ambiente de simulação" SHALL criar o ambiente de simulação respeitando os parâmetros de ambiente recebidos do Estágio 4.
3. THE Estágio 5 "Criação do ambiente de simulação" SHALL criar o ambiente utilizando exclusivamente contêineres neste primeiro momento.
4. WHEN a campanha selecionada é uma campanha curada, THE Backend SHALL criar o ambiente a partir do ambiente curado lido diretamente de disco para essa campanha.
5. THE Estágio 5 "Criação do ambiente de simulação" SHALL carregar as Abilities ordenadas no Caldera na ordem estipulada pelo DAG.
6. BEFORE concluir o Estágio 5, THE Backend SHALL verificar o isolamento de todos os contêineres-alvo do ambiente conforme o Requisito 14.
7. IF algum contêiner-alvo do ambiente não estiver isolado, THEN THE Backend SHALL manter o Estágio 5 não concluído e reportar o contêiner não isolado ao Pesquisador.
8. THE Estágio 5 "Criação do ambiente de simulação" SHALL ter como Saída o ambiente de simulação pronto e isolado, com as Abilities carregadas no Caldera.
9. WHEN o ambiente de simulação está pronto, isolado e as Abilities estão carregadas no Caldera, THE Backend SHALL permitir concluir o Estágio 5.

### Requisito 10: Estágio 6 — Execução da campanha, medição e relatório

**História de usuário:** Como Pesquisador, quero executar a campanha no ambiente simulado, medir e gerar o relatório, para que eu obtenha os resultados da reprodução da campanha.

#### Critérios de Aceitação

1. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL ter como Entrada o ambiente de simulação produzido no Estágio 5.
2. BEFORE executar a campanha, THE Backend SHALL verificar as pré-condições de contenção conforme os Requisitos 13 e 14.
3. WHEN o Pesquisador aciona a execução e as pré-condições de contenção são satisfeitas, THE Backend SHALL executar a campanha uma única vez no ambiente simulado.
4. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL medir o resultado da execução da campanha conforme as métricas do Requisito 15.
5. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL analisar o sucesso ou a falha da campanha a partir das medições.
6. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL gerar o relatório consolidando as medições e o veredito de sucesso ou falha.
7. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL ter como Saída os resultados da execução e o relatório.
8. WHEN a execução da campanha conclui e as medições e o relatório são produzidos, THE Backend SHALL desprovisionar o ambiente da campanha conforme o Requisito 12.
9. WHEN a execução da campanha conclui e o ambiente é desprovisionado, THE Backend SHALL permitir concluir o Estágio 6 e persistir os resultados e o relatório.

### Requisito 11: Uso de artefatos curados como mecanismo de transição

**História de usuário:** Como Pesquisador, quero que as 8 campanhas curadas usem os artefatos que o colega já produziu, para que eu rode o fluxo completo hoje sem IA e sem acesso à rede nesses caminhos, enquanto o agente de IA ainda não existe.

#### Critérios de Aceitação

1. WHEN a campanha selecionada é uma campanha curada, THE Backend SHALL obter o DAG do Estágio 3, as Abilities do Estágio 4 e o ambiente do Estágio 5 a partir dos artefatos curados lidos diretamente de disco.
2. THE Backend SHALL tratar o conjunto curado como um mecanismo de transição válido somente até o agente de IA existir.
3. WHILE os Estágios 3, 4 e 5 de uma campanha curada são conduzidos, THE Backend SHALL NÃO invocar nenhum agente de IA.
4. WHILE os Estágios 3, 4 e 5 de uma campanha curada são conduzidos no ambiente de desenvolvimento, THE Backend SHALL NÃO realizar acesso à rede nesses caminhos.
5. THE conjunto curado de DAG, Abilities e ambiente SHALL existir somente para a versão 18 e para as 8 campanhas curadas.
6. IF o artefato curado correspondente a um dos Estágios 3, 4 ou 5 de uma campanha curada estiver ausente, THEN THE Backend SHALL manter esse Estágio não concluído e reportar a ausência do artefato ao Pesquisador.
7. WHERE o agente de IA passar a produzir os artefatos dos Estágios 3, 4 e 5, THE conjunto curado SHALL ser removido como fonte desses artefatos.

### Requisito 12: Desprovisionamento do ambiente após a execução

**História de usuário:** Como Pesquisador, quero que o ambiente provisionado seja limpo após a execução da campanha, para que os recursos de hardware não se esgotem entre execuções.

#### Critérios de Aceitação

1. WHEN a execução de uma campanha conclui no Estágio 6, THE Backend SHALL desprovisionar o ambiente que foi provisionado para essa campanha.
2. THE desprovisionamento SHALL remover os contêineres criados para o ambiente da campanha.
3. IF o desprovisionamento do ambiente falha, THEN THE Backend SHALL reportar a falha ao Pesquisador e identificar os recursos que permaneceram provisionados.

### Requisito 13: Contenção da execução — nenhum destino externo

**História de usuário:** Como Pesquisador, quero que os comandos de ataque nunca executem na minha máquina local e nunca alcancem destinos externos, para que a reprodução da campanha seja simulada somente dentro do ambiente isolado.

#### Critérios de Aceitação

1. THE Backend SHALL simular os comandos de ataque exclusivamente dentro do ambiente de simulação provisionado e isolado.
2. THE Backend SHALL NÃO executar comandos de ataque na máquina local do Pesquisador.
3. THE Backend SHALL extrair os endereços de destino (IPs, hosts e URLs) de cada comando concreto por meio do `command_parser` da contenção.
4. THE Backend SHALL classificar cada destino como interno ou externo contra as três sub-redes internas do laboratório (`192.168.10.0/24`, `192.168.20.0/24`, `192.168.30.0/24`) por meio do `subnet_validator`, produzindo um `AbilityContainmentReport` tipado.
5. IF ao menos um comando de uma Ability tem um destino externo, THEN THE Backend SHALL recusar a execução, não iniciar a Operação e identificar a Ability e o comando com o destino externo.
6. BEFORE qualquer execução, THE Pipeline_UI SHALL exibir, por meio do `preview`, a lista dos comandos concretos com o contêiner-alvo de cada um e sinalizar os destinos externos em um modal de confirmação.
7. THE proteções de contenção já implementadas em `ui/backend/app/services/containment/` SHALL ser preservadas.

### Requisito 14: Verificação de isolamento dos contêineres-alvo

**História de usuário:** Como Pesquisador, quero que o sistema verifique que cada contêiner-alvo está isolado antes de executar, para que nenhuma simulação ocorra em um ambiente com vazamento para a rede externa.

#### Critérios de Aceitação

1. THE Backend SHALL verificar o isolamento dos contêineres-alvo por meio do `isolation`, usando somente inspeção read-only do Docker, sem iniciar, parar, criar ou modificar contêineres ou redes.
2. THE Backend SHALL considerar um contêiner-alvo isolado somente quando ele estiver anexado exclusivamente a redes marcadas `internal: true`, sem bridge externo e sem DNS externo configurado.
3. THE Backend SHALL produzir um `IsolationResult` com um veredito por contêiner e um resultado geral de aprovação ou reprovação.
4. IF algum contêiner-alvo não estiver isolado, THEN THE Backend SHALL reprovar o resultado geral de isolamento e identificar o contêiner não isolado com a razão.
5. IF o resultado geral de isolamento é reprovado, THEN THE Backend SHALL recusar a execução da campanha no Estágio 6.

### Requisito 15: Métricas de suficiência procedural e consolidação no relatório

**História de usuário:** Como Pesquisador, quero medir a suficiência procedural da inteligência STIX para reproduzir a campanha, para que o relatório mostre o quanto a campanha foi efetivamente reproduzível.

#### Critérios de Aceitação

1. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL medir a taxa de técnicas executadas com sucesso em relação ao total de técnicas do DAG.
2. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL registrar quais nós ou técnicas falharam e em qual etapa do DAG a falha ocorreu.
3. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL medir a cobertura procedural como a proporção do DAG efetivamente executável.
4. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL medir o tempo de execução por técnica e o tempo total de execução.
5. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL registrar os artefatos ou a telemetria observados por técnica (processo, conexão de rede, autenticação e acesso a arquivo).
6. THE Estágio 6 "Execução da campanha, medição e relatório" SHALL produzir um veredito geral de sucesso ou falha da campanha.
7. THE relatório SHALL consolidar as medições dos itens 1 a 6 deste requisito.
8. THE conjunto de métricas definido neste requisito SHALL ser tratado como uma proposta inicial sujeita a refinamento.

### Requisito 16: Ponto de extensão para substituição futura por IA

**História de usuário:** Como mantenedor do STICKS, quero que os Estágios 3, 4 e 5 tenham um ponto de extensão por origem, para que o agente de IA seja encaixado depois sem redesenho — sem implementar o agente agora.

#### Critérios de Aceitação

1. THE Estágio 3 "Geração do DAG" SHALL expor a produção do DAG por meio de um ponto de extensão com origem explícita, admitindo uma implementação futura de origem `gerado_ia` sem alterar o artefato DAG nem a assinatura do ponto de extensão.
2. THE Estágio 4 "Operacionalização" SHALL expor a criação das Abilities e dos parâmetros de ambiente por meio de um ponto de extensão com origem explícita, admitindo uma implementação futura de origem `gerado_ia` sem redesenho.
3. THE Estágio 5 "Criação do ambiente de simulação" SHALL expor a criação do ambiente por meio de um ponto de extensão com origem explícita, admitindo uma implementação futura de origem `gerado_ia` sem redesenho.
4. THE origem dos artefatos dos Estágios 3, 4 e 5 SHALL aceitar o valor `gerado_ia` sem exigir alteração do esquema persistido quando o agente de IA for implementado.
5. THE implementação do agente de IA que produziria artefatos de origem `gerado_ia` SHALL ser mantida fora do escopo desta spec.

### Requisito 17: Restrição de contêineres neste primeiro momento

**História de usuário:** Como Pesquisador, quero que o ambiente use apenas contêineres agora mas que o desenho não impeça máquinas virtuais no futuro, para que a limitação atual não vire um obstáculo de redesenho depois.

#### Critérios de Aceitação

1. THE Estágio 5 "Criação do ambiente de simulação" SHALL criar o ambiente usando exclusivamente contêineres neste primeiro momento.
2. THE desenho do Estágio 5 "Criação do ambiente de simulação" SHALL admitir a adição futura de máquinas virtuais como plataforma de ambiente sem redesenho.

### Requisito 18: Não-objetivos desta spec

**História de usuário:** Como mantenedor do STICKS, quero delimitar explicitamente o que esta spec não faz, para que o escopo termine no pipeline de seis Estágios funcionando com os artefatos curados e com os pontos de extensão prontos.

#### Critérios de Aceitação

1. THE escopo desta spec SHALL NÃO incluir a implementação do agente de IA dos Estágios 3, 4 e 5.
2. THE escopo desta spec SHALL NÃO incluir a implementação do seletor ou parametrizador de comandos do Atomic Red Team.
3. THE escopo desta spec SHALL NÃO incluir os ciclos de repetição ou retry automáticos do Estágio 6 (voltar à operacionalização ou à criação do ambiente e executar de novo de outra forma), tratados como visão futura dependente do agente de IA.
4. THE escopo desta spec SHALL NÃO incluir a otimização do provisionamento e do desprovisionamento do ambiente a cada execução, tratada como ponto de atenção futuro.
5. THE escopo desta spec SHALL NÃO incluir a migração ou renomeação das classes `Stage*` do código, cuja inconsistência de nomes é tratada como dívida técnica separada.
6. THE escopo desta spec SHALL completar o pipeline de ponta a ponta utilizando apenas as 8 campanhas curadas existentes na versão 18.
