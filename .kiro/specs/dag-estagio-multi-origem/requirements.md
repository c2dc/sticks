# Requirements Document

## Introduction

A ferramenta de pesquisa **STICKS** acompanha um artigo sobre a "lacuna de semântica procedural no ATT&CK-em-STIX": um pacote STIX de inteligência de ameaças (CTI) lista **quais** técnicas ATT&CK uma campanha utilizou, mas **não** registra a ordem procedural nem as dependências entre essas técnicas. Essa ordenação é um **Grafo Acíclico Dirigido (DAG)** que **não existe** no pacote STIX — ele precisa ser **produzido**. Hoje, os DAGs dos casos curados foram produzidos por um colaborador humano; no futuro, um agente de IA produzirá DAGs automaticamente a partir do STIX, e esse agente **pode** produzir DAGs **diferentes** e/ou **múltiplos** para a **mesma** campanha.

A aplicação web vive em `ui/` (recém-renomeada a partir de `pipeline_ui`): `ui/backend` (FastAPI + SQLAlchemy + Alembic + **PostgreSQL exclusivamente**, sem SQLite em produto/dev; o dev roda nativamente via `npm run dev` contra o PostgreSQL em contêiner em `localhost:5432`) e `ui/frontend` (React 19 + TypeScript + Vite + i18next pt-BR). Já existe a spec de fluxo `fluxo-stix-dashboard` (Landing → Bases STIX → Dashboard de Inteligência → Seleção de Campanha/Ator → fase de Geração/Visualização do DAG → wizard de 3 estágios).

**Hoje**, a "geração do DAG" é apenas uma **fase de visualização** na interface (não persistida) que fica **antes** dos 3 estágios persistidos. Os 3 estágios persistidos (tabela `execucoes_estagio` / modelo `StageRun`, com `estagio` 1/2/3) são: 1-Modelagem Estrutural, 2-Tradução Curada, 3-Emulação. As garantias de contenção/isolamento da emulação precisam ser preservadas.

Este documento especifica a evolução que:

1. **Promove a geração do DAG a um estágio real e persistido**, renumerando o pipeline para **quatro estágios** (Estágio 1: Geração do DAG; Estágio 2: Modelagem Estrutural; Estágio 3: Tradução Curada; Estágio 4: Emulação), com uma **migração Alembic** que preserva o estado já persistido dos casos curados.
2. **Unifica a terminologia** "fase" e "estágio" como sinônimos, padronizando em **"Estágio"** no produto.
3. Introduz uma **entidade DAG persistida de primeira classe e multi-origem** (1 campanha → N DAGs), com uma **origem** explícita e extensível (`curadoria_humana` | `gerado_ia`), seguindo o precedente já existente no código do atributo `origem_traducao` / `TranslationSource`.
4. Define um **ponto de extensão de geração por IA** (`GeradorDeDag`) com implementação de **curadoria** agora e implementação de **IA** encaixável depois **sem redesenho**, incluindo o botão "Gerar com IA" **visível porém desabilitado** ("em breve") e o **bloqueio** de avanço quando não há DAG.
5. Estabelece como **NÃO-OBJETIVO** desta iteração a implementação do gerador de IA em si.

Esta spec **constrói sobre** e **supersede** as partes relativas à fase do DAG da spec `fluxo-stix-dashboard`.

## Glossary

- **STICKS**: Ferramenta de pesquisa que acompanha o artigo sobre a lacuna de semântica procedural no ATT&CK-em-STIX. Compreende `ui/backend` (FastAPI + SQLAlchemy + Alembic + PostgreSQL) e `ui/frontend` (React/TypeScript).
- **STIX**: Formato padronizado de inteligência de ameaças cibernéticas (CTI). Lista **quais** técnicas ATT&CK uma campanha usou, sem registrar a ordem procedural entre elas.
- **ATT&CK**: Base de conhecimento de táticas e técnicas adversárias do MITRE, referenciada pelas campanhas STIX por identificadores de técnica (ex.: `T1102`).
- **Campanha**: Unidade de inteligência (campanha ou ator) descrita por um pacote STIX. No código corresponde a um **Caso curado** identificado por um `slug`. Uma campanha pode ter **N DAGs**.
- **Caso curado**: Caso cujo material (par de arquivos em `data/api/{slug}_dag-*.json` e o DAG em `data/dag/{slug}_dag.json`) foi produzido por **curadoria humana** e já existe em disco. Modelado pela entidade `Caso` (tabela `casos`) e enumerado em `CURATED_CASES`.
- **DAG (Grafo Acíclico Dirigido)**: Representação ordenada das técnicas de uma campanha e de suas dependências procedurais. **Não existe** no pacote STIX; precisa ser produzido. Seu conteúdo gráfico são os `structural_nodes` e as arestas (`de`/`para`) derivadas das relações `parent_nodes`/`child_nodes`, equivalentes ao conteúdo de `data/dag/{slug}_dag.json`.
- **Estágio** (= **fase**): Etapa persistida do pipeline de uma campanha. Após esta spec, "fase" e "estágio" são **sinônimos** e o termo padronizado no produto é **"Estágio"**. O pipeline passa a ter quatro estágios (1 a 4), cada um com uma execução persistida (`StageRun` na tabela `execucoes_estagio`).
- **Origem do DAG**: Atributo explícito e extensível de um DAG que indica **como** ele foi produzido. Valores atuais: `curadoria_humana` (produzido por humano) e `gerado_ia` (produzido por agente de IA). Modelado seguindo o padrão de `TranslationSource` / `origem_traducao`.
- **GeradorDeDag**: Costura (interface/serviço) de extensão no backend que produz um DAG para uma campanha. Possui uma implementação de **curadoria** agora (carrega o DAG curado de `data/dag/{slug}_dag.json`) e admite uma implementação de **IA** encaixável depois, sem redesenho.
- **StageRun / `execucoes_estagio`**: Modelo SQLAlchemy e tabela que persistem a execução de um Estágio de um Caso (coluna `estagio`, estado, progresso, falha).
- **TranslationSource / `origem_traducao`**: Enum e atributo já existentes no código que modelam a origem (extensível) da tradução do estágio de tradução. Precedente para como a **origem do DAG** é modelada.
- **Contenção**: Conjunto de garantias de isolamento da emulação — redes Docker `internal: true`, confirmação explícita antes de qualquer execução real, auditoria por comando — que devem ser preservadas.
- **Estado_de_Sessão / `estado_sessao`**: Estado que dá continuidade ao Pesquisador entre máquinas; deriva os estágios concluídos por caso a partir de `StageRun`.
- **Pesquisador**: Pessoa usuária da ferramenta STICKS.
- **Migração Alembic**: Script versionado de evolução de esquema/dados no `ui/backend/alembic/versions`, aplicado contra o PostgreSQL do produto.

## Requirements

### Requisito 1: Pipeline de quatro estágios (promoção da geração do DAG)

**História de usuário:** Como Pesquisador, quero que a geração do DAG seja um estágio real e persistido do pipeline, para que o pipeline tenha quatro estágios rastreáveis e eu acompanhe o progresso da geração do DAG como acompanho os demais estágios.

#### Critérios de Aceitação

1. THE Pipeline_STICKS SHALL definir exatamente quatro Estágios ordenados: Estágio 1 "Geração do DAG", Estágio 2 "Modelagem Estrutural", Estágio 3 "Tradução Curada" e Estágio 4 "Emulação".
2. THE Backend SHALL persistir a execução do Estágio 1 "Geração do DAG" como um `StageRun` na tabela `execucoes_estagio`, com o mesmo contrato de estado e progresso dos demais Estágios.
3. WHEN o Backend deriva o estado de um Estágio de um Caso, THE Backend SHALL ler o `StageRun` correspondente ao número de Estágio no intervalo de 1 a 4.
4. WHILE o Estágio 1 "Geração do DAG" de um Caso não estiver no estado `concluido`, THE Pipeline_STICKS SHALL manter o Estágio 2 "Modelagem Estrutural" desse Caso no estado bloqueado.
5. WHILE o Estágio imediatamente anterior de um Caso não estiver no estado `concluido`, THE Pipeline_STICKS SHALL manter o Estágio seguinte desse Caso no estado bloqueado, para cada par de Estágios adjacentes de 1 a 4.
6. WHEN o Backend calcula o progresso agregado de replicação, THE Backend SHALL contar como totalmente concluído apenas o Caso cujos quatro Estágios estejam no estado `concluido`.
7. WHEN um canal de tempo real reporta o estado de um Estágio, THE Backend SHALL identificar o Estágio pelo número no intervalo de 1 a 4.
8. THE Estado_de_Sessão SHALL derivar os Estágios concluídos por Caso a partir dos `StageRun` cujo `estado` seja `concluido`, considerando os números de Estágio no intervalo de 1 a 4.

### Requisito 2: Migração que renumera os estágios preservando o estado persistido

**História de usuário:** Como Pesquisador, quero que a renumeração dos estágios aconteça por meio de uma migração que preserve o que já foi produzido, para que o progresso dos casos curados já persistido não seja perdido.

#### Critérios de Aceitação

1. THE Migração_Alembic SHALL renumerar cada linha existente de `execucoes_estagio` deslocando o valor de `estagio` em uma unidade, mapeando 1 para 2, 2 para 3 e 3 para 4.
2. WHEN a Migração_Alembic renumera as linhas de `execucoes_estagio`, THE Migração_Alembic SHALL preservar o `estado`, o `progresso`, a `mensagem_erro`, a `etapa_falha` e o `caso_id` de cada linha renumerada.
3. THE Migração_Alembic SHALL executar o deslocamento de valores de `estagio` em ordem decrescente de valor, de modo que nenhuma linha renumerada colida com o valor de outra linha durante a atualização.
4. THE Migração_Alembic SHALL fornecer uma operação de reversão (`downgrade`) que mapeia os valores de `estagio` de 4 para 3, 3 para 2 e 2 para 1, preservando os demais atributos de cada linha.
5. WHEN a Migração_Alembic é aplicada contra a Base_de_Dados PostgreSQL, THE Migração_Alembic SHALL concluir sem recorrer a SQLite.
6. IF a Migração_Alembic encontra um valor de `estagio` fora do intervalo de 1 a 3 antes do `upgrade`, THEN THE Migração_Alembic SHALL interromper a aplicação sinalizando um erro descritivo sem alterar dados.

### Requisito 3: Unificação de terminologia "fase" e "estágio"

**História de usuário:** Como Pesquisador, quero que "fase" e "estágio" signifiquem a mesma coisa, para que eu não confunda a geração do DAG como algo separado dos estágios do pipeline.

#### Critérios de Aceitação

1. THE Interface_STICKS SHALL apresentar o termo "Estágio" como designação única das etapas do pipeline exibidas ao Pesquisador.
2. THE Interface_STICKS SHALL exibir a geração do DAG como o Estágio 1 do pipeline, e não como uma fase separada dos Estágios.
3. WHERE um texto da Interface_STICKS referencia uma etapa do pipeline, THE Interface_STICKS SHALL usar o termo "Estágio" em pt-BR por meio das chaves de i18next correspondentes.
4. THE Base_de_Codigo SHALL referenciar as etapas do pipeline com uma terminologia única consistente com o termo "Estágio" apresentado na Interface_STICKS.

### Requisito 4: Entidade DAG persistida, de primeira classe e multi-origem

**História de usuário:** Como Pesquisador, quero que cada DAG seja uma entidade persistida associada a uma campanha, para que uma campanha possa ter vários DAGs de origens distintas e eu possa listá-los e escolher entre eles.

#### Critérios de Aceitação

1. THE Backend SHALL persistir cada DAG como uma entidade de primeira classe associada à Campanha (Caso) à qual pertence.
2. THE Entidade_DAG SHALL permitir que uma mesma Campanha possua múltiplos DAGs, caracterizando a relação de uma Campanha para N DAGs.
3. THE Entidade_DAG SHALL registrar uma `origem` explícita e extensível, com os valores `curadoria_humana` e `gerado_ia`, modelada seguindo o padrão de `TranslationSource` / `origem_traducao`.
4. THE Entidade_DAG SHALL registrar um identificador/versão do DAG, um instante de criação e o conteúdo gráfico composto pelos `structural_nodes` e pelas arestas, equivalente ao conteúdo de `data/dag/{slug}_dag.json`.
5. WHEN o Backend carrega os DAGs curados existentes, THE Backend SHALL persistir um DAG de `origem` igual a `curadoria_humana` por Caso curado, a partir do arquivo `data/dag/{slug}_dag.json` correspondente.
6. THE Enum_Origem_DAG SHALL ser definido de modo que o valor `gerado_ia` seja aceitável sem alteração do esquema persistido da Entidade_DAG.
7. THE Estágio 1 "Geração do DAG" de uma execução SHALL referenciar qual DAG foi escolhido para aquela execução.
8. WHEN o Backend lista os DAGs de uma Campanha, THE Backend SHALL incluir, para cada DAG, a sua `origem`, o seu identificador/versão e o seu instante de criação.

### Requisito 5: Ponto de extensão de geração do DAG (`GeradorDeDag`)

**História de usuário:** Como mantenedor do STICKS, quero uma costura de extensão para a geração do DAG com uma implementação curada agora, para que uma implementação por IA seja encaixada depois sem redesenho.

#### Critérios de Aceitação

1. THE Backend SHALL definir a costura `GeradorDeDag` como interface/serviço com uma operação de produção de DAG para uma Campanha.
2. THE Backend SHALL fornecer uma implementação de curadoria de `GeradorDeDag` que produz o DAG de `origem` `curadoria_humana` a partir de `data/dag/{slug}_dag.json`.
3. THE Backend SHALL definir a costura `GeradorDeDag` de modo que uma implementação de geração por IA seja adicionável sem alterar a Entidade_DAG nem a assinatura da costura.
4. WHEN a implementação de curadoria de `GeradorDeDag` produz um DAG, THE implementação de curadoria SHALL registrar a `origem` do DAG como `curadoria_humana`.
5. THE implementação de curadoria de `GeradorDeDag` SHALL operar sem acesso à rede e sem invocar qualquer serviço de IA.

### Requisito 6: Bloqueio de avanço e botão "Gerar com IA" visível e desabilitado

**História de usuário:** Como Pesquisador, quero ver uma opção de gerar o DAG por IA sinalizada como indisponível quando a campanha não tem DAG, para que eu entenda que a funcionalidade existirá e que não posso avançar sem um DAG.

#### Critérios de Aceitação

1. WHILE a Campanha selecionada não possui nenhum DAG persistido, THE Interface_STICKS SHALL exibir o botão "Gerar com IA" visível e desabilitado com indicação "em breve".
2. WHILE a Campanha selecionada não possui nenhum DAG persistido, THE Pipeline_STICKS SHALL manter o avanço para além do Estágio 1 "Geração do DAG" bloqueado.
3. WHEN o Pesquisador seleciona um DAG existente da Campanha para a execução, THE Backend SHALL registrar a referência do DAG escolhido no Estágio 1 e liberar o avanço para o Estágio 2.
4. WHILE a Campanha selecionada possui ao menos um DAG persistido, THE Interface_STICKS SHALL permitir ao Pesquisador escolher entre os DAGs disponíveis da Campanha.
5. IF o Pesquisador aciona o botão "Gerar com IA" enquanto ele está desabilitado, THEN THE Interface_STICKS SHALL manter o Estágio 1 inalterado e não iniciar nenhuma geração.

### Requisito 7: Preservação da contenção e das garantias existentes

**História de usuário:** Como Pesquisador, quero que o isolamento da emulação e as telas existentes continuem funcionando após a renumeração, para que a segurança e o comportamento já validado sejam preservados.

#### Critérios de Aceitação

1. THE Pipeline_STICKS SHALL preservar as garantias de Contenção do Estágio 4 "Emulação", incluindo as redes Docker `internal: true` e a confirmação explícita antes de qualquer execução real.
2. WHEN o Pesquisador alcança o Estágio 4 "Emulação", THE Interface_STICKS SHALL apresentar o modal de confirmação existente antes de qualquer execução real.
3. THE Backend SHALL operar exclusivamente contra a Base_de_Dados PostgreSQL, sem recorrer a SQLite no produto.
4. THE Ambiente_Dev SHALL continuar executando nativamente com recarga a quente (`npm run dev`) contra o PostgreSQL em contêiner.
5. THE Interface_STICKS SHALL preservar as telas existentes de tradução curada, de emulação e o modal de confirmação após a renumeração dos Estágios.

### Requisito 8: Cobertura de testes para a evolução

**História de usuário:** Como mantenedor do STICKS, quero que as suítes de teste existentes permaneçam verdes e que a evolução tenha cobertura, para que a renumeração, a migração e a entidade DAG multi-origem sejam verificáveis.

#### Critérios de Aceitação

1. THE Suite_de_Testes SHALL manter os testes existentes aprovados após a renumeração dos Estágios.
2. THE Suite_de_Testes SHALL cobrir a numeração dos quatro Estágios, incluindo o bloqueio sequencial e o progresso agregado.
3. THE Suite_de_Testes SHALL cobrir a Migração_Alembic de renumeração, verificando a preservação de `estado`, `progresso`, `mensagem_erro`, `etapa_falha` e `caso_id` das linhas renumeradas.
4. THE Suite_de_Testes SHALL cobrir a Entidade_DAG multi-origem, incluindo a relação de uma Campanha para N DAGs e a `origem` extensível com os valores `curadoria_humana` e `gerado_ia`.

### Requisito 9: Não-objetivo — geração por IA não implementada nesta iteração

**História de usuário:** Como mantenedor do STICKS, quero delimitar explicitamente que o gerador de IA não é construído agora, para que o escopo desta iteração termine no pipeline completo com os casos curados e no modelo de dados pronto para a IA.

#### Critérios de Aceitação

1. THE Iteração_Atual SHALL completar o pipeline de ponta a ponta utilizando apenas os Casos curados existentes.
2. THE Iteração_Atual SHALL deixar o modelo de dados e o fluxo prontos para a geração por IA e para múltiplos DAGs por Campanha, sem implementar o gerador de IA.
3. THE Iteração_Atual SHALL deixar a produção de um DAG de `origem` `gerado_ia` como responsabilidade fora do escopo, a ser encaixada posteriormente na costura `GeradorDeDag` sem redesenho.
4. WHEN um DAG de `origem` `gerado_ia` for inserido futuramente, THE Entidade_DAG SHALL aceitá-lo sem qualquer alteração de esquema.
