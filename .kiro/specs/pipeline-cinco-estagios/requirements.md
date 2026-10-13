# Requirements Document

## Introduction

A ferramenta de pesquisa **STICKS** reproduz campanhas de adversário a partir de inteligência STIX, de forma contida. Esta spec **corrige e expande** o pipeline de Estágios definido em `dag-estagio-multi-origem`.

Duas mudanças motivam este documento:

1. **A ordem dos dois primeiros Estágios estava invertida.** A spec anterior colocou "Geração do DAG" como Estágio 1 e "Modelagem Estrutural" como Estágio 2. Isso contraria o fluxo de dados real: o DAG é **derivado** das técnicas e correlações extraídas do STIX. Portanto a Modelagem Estrutural deve vir **antes** da Geração do DAG. Quando o gerador por IA assumir, ele precisará da modelagem como **entrada** para produzir o grafo.
2. **A "Emulação" era uma responsabilidade única e deve ser dividida em duas.** Preparar o ambiente onde a campanha pode rodar (contêineres, VMs, redes simuladas) é um problema distinto de executar a campanha e avaliar seu sucesso. No futuro, o agente de IA montará o ambiente sob medida a partir das técnicas rastreadas — tarefa complexa que merece seu próprio Estágio.

O pipeline passa de **quatro** para **cinco Estágios ordenados**:

1. **Modelagem Estrutural** — extrai técnicas ATT&CK e correlações do STIX.
2. **Geração do DAG** — ordena essas técnicas num Grafo Acíclico Dirigido.
3. **Operacionalização** — traduz técnicas abstratas em Abilities executáveis (comandos, ordenação, Adversary).
4. **Provisionamento do Ambiente** — prepara/verifica o ambiente isolado onde a campanha pode ser executada.
5. **Execução da Campanha** — roda a emulação real e avalia sucesso/falha.

Esta é uma **solução de transição**: enquanto não existe o agente de IA, o sistema roda sobre o material **curado** (DAGs, Abilities e arquivos de ambiente que um colaborador humano já produziu). Quando o agente existir, a curadoria será removida e os Estágios 2, 3 e 4 passarão a ser produzidos pela IA. Por isso o fluxo precisa já estar na ordem correta e com as costuras de extensão prontas.

A aplicação vive em `ui/`: `ui/backend` (FastAPI + SQLAlchemy + Alembic + **PostgreSQL exclusivamente**) e `ui/frontend` (React 19 + TypeScript + Vite + i18next pt-BR).

Esta spec **constrói sobre** e **supersede** as partes de ordenação de Estágios e de "Emulação" da spec `dag-estagio-multi-origem`.

## Glossary

- **STICKS**: Ferramenta de pesquisa. Compreende `ui/backend` (FastAPI + SQLAlchemy + Alembic + PostgreSQL) e `ui/frontend` (React/TypeScript).
- **STIX**: Formato de inteligência de ameaças. Lista **quais** técnicas ATT&CK uma campanha usou, sem a ordem procedural.
- **ATT&CK**: Base de conhecimento de táticas e técnicas adversárias do MITRE.
- **Campanha / Caso curado**: Unidade de inteligência descrita por um pacote STIX, identificada por um `slug`, com material curado em disco (`data/api/{slug}_dag-*.json`, `data/dag/{slug}_dag.json`) e, para o ambiente, um arquivo de configuração de laboratório. Modelada pela entidade `Caso` e enumerada em `CURATED_CASES`.
- **Estágio**: Etapa persistida do pipeline de um Caso. O pipeline passa a ter **cinco** Estágios (1 a 5), cada um com uma execução persistida (`StageRun` na tabela `execucoes_estagio`).
- **Modelagem Estrutural (Estágio 1)**: Extração das técnicas ATT&CK e das correlações (relações `parent_nodes`/`child_nodes`) a partir do STIX.
- **Geração do DAG (Estágio 2)**: Produção do Grafo Acíclico Dirigido a partir da Modelagem Estrutural. Possui `origem` explícita e extensível (`curadoria_humana` | `gerado_ia`).
- **Operacionalização (Estágio 3)**: Transformação das técnicas abstratas em Abilities executáveis — comandos concretos, ordenação/dependências e agrupamento em Adversary. (Antes chamado "Tradução Curada".)
- **Provisionamento do Ambiente (Estágio 4)**: Preparação/verificação do ambiente isolado (redes Docker `internal`, contêineres) onde a campanha pode ser executada. Hoje o ambiente é provisionado pelo docker-compose do laboratório; no futuro, pelo agente de IA.
- **Execução da Campanha (Estágio 5)**: Execução real da emulação (Operação na Caldera) contra o ambiente provisionado, com avaliação de sucesso/falha por Ability e agregado.
- **GeradorDeDag**: Costura de extensão que produz um DAG para um Caso **a partir da Modelagem Estrutural**. Implementação de **curadoria** agora; implementação de **IA** encaixável depois, sem redesenho.
- **Origem (do DAG)**: Atributo explícito e extensível que indica como o DAG foi produzido (`curadoria_humana` | `gerado_ia`), seguindo o precedente de `TranslationSource` / `origem_traducao`.
- **StageRun / `execucoes_estagio`**: Modelo e tabela que persistem a execução de um Estágio de um Caso (coluna `estagio`, estado, progresso, falha).
- **Contenção**: Garantias de isolamento — redes Docker `internal: true`, confirmação explícita antes de qualquer execução real, auditoria por comando — que devem ser preservadas.
- **Estado_de_Sessão**: Estado que dá continuidade entre máquinas; deriva os Estágios concluídos por Caso a partir de `StageRun`.
- **Pesquisador**: Pessoa usuária da ferramenta STICKS.
- **Migração Alembic**: Script versionado de evolução de esquema/dados em `ui/backend/alembic/versions`, aplicado contra o PostgreSQL.

## Requirements

### Requisito 1: Pipeline de cinco Estágios na ordem correta

**História de usuário:** Como Pesquisador, quero que o pipeline tenha cinco Estágios na ordem que reflete o fluxo de dados real (modelar antes de gerar o grafo; preparar o ambiente antes de executar), para que o processo seja correto hoje com a curadoria e permaneça correto quando o agente de IA assumir.

#### Critérios de Aceitação

1. THE Pipeline_STICKS SHALL definir exatamente cinco Estágios ordenados: Estágio 1 "Modelagem Estrutural", Estágio 2 "Geração do DAG", Estágio 3 "Operacionalização", Estágio 4 "Provisionamento do Ambiente" e Estágio 5 "Execução da Campanha".
2. THE Backend SHALL persistir a execução de cada um dos cinco Estágios como um `StageRun` na tabela `execucoes_estagio`, com o mesmo contrato de estado e progresso.
3. WHEN o Backend deriva o estado de um Estágio de um Caso, THE Backend SHALL ler o `StageRun` correspondente ao número de Estágio no intervalo de 1 a 5.
4. WHILE o Estágio imediatamente anterior de um Caso não estiver no estado `concluido`, THE Pipeline_STICKS SHALL manter o Estágio seguinte desse Caso no estado bloqueado, para cada par de Estágios adjacentes de 1 a 5.
5. THE Estágio 1 "Modelagem Estrutural" SHALL ser o ponto de partida do pipeline e NÃO depender de nenhum Estágio anterior.
6. WHEN o Backend calcula o progresso agregado de replicação, THE Backend SHALL contar como totalmente concluído apenas o Caso cujos cinco Estágios estejam no estado `concluido`.
7. WHEN um canal de tempo real reporta o estado de um Estágio, THE Backend SHALL identificar o Estágio pelo número no intervalo de 1 a 5.
8. THE Estado_de_Sessão SHALL derivar os Estágios concluídos por Caso a partir dos `StageRun` cujo `estado` seja `concluido`, considerando os números de Estágio no intervalo de 1 a 5.

### Requisito 2: Modelagem Estrutural como Estágio 1

**História de usuário:** Como Pesquisador, quero extrair primeiro as técnicas e correlações do STIX, para que o DAG seja construído a partir dessa modelagem.

#### Critérios de Aceitação

1. THE Estágio 1 "Modelagem Estrutural" SHALL extrair, a partir do material do Caso, as técnicas ATT&CK e as correlações (relações `parent_nodes`/`child_nodes`) entre elas, além de indicadores, infraestrutura, malware e metadados quando presentes.
2. WHEN o Pesquisador aciona a execução da Modelagem Estrutural de um Caso, THE Backend SHALL executar a extração, persistir os elementos extraídos associados ao Caso e marcar o `StageRun` do Estágio 1 como `concluido`.
3. WHEN a extração não produz nenhuma técnica, THE Backend SHALL tratar o resultado como sucesso com indicação de "nenhum elemento estrutural extraído" e marcar o Estágio 1 como `concluido`.
4. WHEN a leitura/análise do material do Caso falha, THE Backend SHALL marcar o Estágio 1 como `erro` com causa e etapa da falha, sem persistir extração parcial.
5. THE abertura da tela do Estágio 1 SHALL ser somente-leitura e NÃO executar nem persistir a modelagem.

### Requisito 3: Geração do DAG como Estágio 2, derivada da Modelagem

**História de usuário:** Como Pesquisador, quero que o DAG seja o Estágio 2, construído a partir da Modelagem Estrutural, para que a dependência de dados fique explícita e o agente de IA futuro receba a modelagem como entrada.

#### Critérios de Aceitação

1. THE costura `GeradorDeDag` SHALL receber a Modelagem Estrutural do Estágio 1 do Caso como entrada para produzir o(s) DAG(s), além do identificador do Caso.
2. WHILE o Estágio 1 "Modelagem Estrutural" de um Caso não estiver `concluido`, THE Pipeline_STICKS SHALL manter o Estágio 2 "Geração do DAG" desse Caso no estado bloqueado.
3. WHEN um Caso possui um DAG curado disponível, THE Backend SHALL carregar o DAG curado e marcar o Estágio 2 desse Caso como `concluido` automaticamente, sem exigir ação do Pesquisador, assim que o Estágio 1 estiver `concluido`.
4. WHEN um Caso NÃO possui DAG (nem curado, nem gerado), THE Frontend SHALL exibir um único botão "Gerar DAG" visível porém **desabilitado**, e THE Pipeline_STICKS SHALL manter o avanço para além do Estágio 2 bloqueado.
5. WHEN o Pesquisador aciona o botão "Gerar DAG" enquanto desabilitado, THE Sistema SHALL ser no-op (nenhuma geração é iniciada; nenhum cliente de IA é invocado).
6. THE implementação de curadoria de `GeradorDeDag` SHALL ler o DAG curado do material do Caso sem acesso à rede e sem invocar qualquer serviço de IA, ignorando a Modelagem recebida como entrada (o grafo curado já existe); a entrada de Modelagem existe para a futura implementação de IA.
7. THE entidade DAG SHALL permanecer persistida, de primeira classe e multi-origem, com `origem` explícita e extensível (`curadoria_humana` | `gerado_ia`).

### Requisito 4: Operacionalização como Estágio 3

**História de usuário:** Como Pesquisador, quero transformar as técnicas abstratas em Abilities executáveis no Estágio 3, para que a campanha tenha comandos concretos prontos para execução.

#### Critérios de Aceitação

1. THE Estágio 3 "Operacionalização" SHALL ter como entrada as técnicas abstratas (consolidadas no DAG do Estágio 2) e como saída as Abilities executáveis, com comandos concretos, ordenação/dependências e agrupamento em Adversary.
2. WHILE o Estágio 2 "Geração do DAG" de um Caso não estiver `concluido`, THE Pipeline_STICKS SHALL manter o Estágio 3 desse Caso no estado bloqueado.
3. THE Estágio 3 SHALL expor a origem da operacionalização de forma explícita e extensível (hoje `curadoria_humana`), seguindo o precedente de `origem_traducao` / `TranslationSource`.
4. WHEN um Caso curado possui a operacionalização pronta, THE Backend SHALL permitir concluir o Estágio 3 carregando o material curado (a conclusão equivale a "avançar", pois os artefatos já existem).
5. THE terminologia do produto SHALL referir-se a este Estágio como "Operacionalização" (não mais "Tradução Curada").

### Requisito 5: Provisionamento do Ambiente como Estágio 4

**História de usuário:** Como Pesquisador, quero um Estágio dedicado à preparação do ambiente isolado onde a campanha pode rodar, para que a construção do laboratório seja uma responsabilidade separada da execução.

#### Critérios de Aceitação

1. THE Estágio 4 "Provisionamento do Ambiente" SHALL ser responsável por preparar/verificar o ambiente isolado (redes Docker `internal`, contêineres) onde a campanha pode ser executada.
2. WHILE o Estágio 3 "Operacionalização" de um Caso não estiver `concluido`, THE Pipeline_STICKS SHALL manter o Estágio 4 desse Caso no estado bloqueado.
3. WHEN um Caso curado possui o arquivo de configuração do ambiente disponível, THE Backend SHALL verificar que o ambiente está isolado/pronto e marcar o Estágio 4 desse Caso como `concluido` automaticamente, sem exigir ação do Pesquisador, assim que o Estágio 3 estiver `concluido`.
4. THE Estágio 4 SHALL exibir a topologia do ambiente contido (hosts, redes internas, diagrama) para o Pesquisador.
5. THE verificação de isolamento do ambiente (redes `internal`, ausência de rota ao host/internet) SHALL pertencer ao Estágio 4.
6. WHEN o ambiente de um Caso não puder ser verificado como isolado, THE Pipeline_STICKS SHALL manter o Estágio 4 não concluído e bloquear o avanço para o Estágio 5.
7. THE costura de provisionamento SHALL ser desenhada de modo que uma futura implementação por IA (criar contêineres/VMs/redes sob medida) encaixe sem redesenho; esta iteração NÃO implementa provisionamento dinâmico por IA.

### Requisito 6: Execução da Campanha como Estágio 5

**História de usuário:** Como Pesquisador, quero executar a campanha no ambiente provisionado e avaliar seu sucesso no Estágio 5, para que a emulação real fique separada da preparação do ambiente.

#### Critérios de Aceitação

1. THE Estágio 5 "Execução da Campanha" SHALL ter como entrada o Adversary + Abilities do Estágio 3 e o ambiente provisionado do Estágio 4, e como saída o resultado por Ability e o agregado (sucesso/falha).
2. WHILE o Estágio 4 "Provisionamento do Ambiente" de um Caso não estiver `concluido`, THE Pipeline_STICKS SHALL manter o Estágio 5 desse Caso no estado bloqueado.
3. THE Estágio 5 SHALL preservar as garantias de contenção: confirmação explícita antes de qualquer execução real e auditoria por comando.
4. WHEN um comando aponta para um destino externo às redes internas, THE Backend SHALL recusar a execução e NÃO iniciar a Operação.
5. WHEN a Caldera não responder dentro do limite configurado, THE Backend SHALL reportar indisponibilidade e NÃO iniciar a Operação.
6. WHEN o Pesquisador NÃO confirmar explicitamente a execução, THE Backend SHALL não iniciar nada.
7. WHEN a execução conclui com sucesso, THE Backend SHALL marcar o Estágio 5 como `concluido` e persistir o resultado por Ability e o agregado.

### Requisito 7: Migração que renumera para cinco Estágios preservando o estado persistido

**História de usuário:** Como Pesquisador, quero que a renumeração para cinco Estágios aconteça por meio de uma migração que preserve o que já foi produzido, para que o progresso já persistido dos casos curados não seja perdido nem corrompido.

#### Critérios de Aceitação

1. THE migração Alembic SHALL encadear a partir da revisão de cabeça atual (`c348c9e36422`).
2. WHEN a migração é aplicada (upgrade), THE migração SHALL remapear os `StageRun` existentes do esquema antigo (1 Geração do DAG, 2 Modelagem, 3 Tradução, 4 Emulação) para o novo esquema (1 Modelagem, 2 Geração do DAG, 3 Operacionalização, 4 Provisionamento do Ambiente, 5 Execução), preservando `estado`, `progresso`, `mensagem_erro`, `etapa_falha` e `caso_id` de cada linha.
3. THE remapeamento do upgrade SHALL traduzir corretamente a inversão dos dois primeiros Estágios e a divisão da antiga Emulação (Estágio 4 antigo) nos novos Estágios 4 (Provisionamento) e 5 (Execução), conforme o design definir.
4. THE migração SHALL fornecer um downgrade simétrico que restaure exatamente o esquema anterior sobre os dados preservados (up∘down = identidade sobre os dados remapeáveis).
5. IF existir alguma linha de `execucoes_estagio` com `estagio` fora do intervalo esperado pelo esquema antigo, THEN THE upgrade SHALL abortar com erro descritivo e sem alterar dados.
6. WHEN a migração cria/ajusta qualquer coluna ou constraint (por exemplo o `CHECK` de intervalo de `estagio`), THE migração SHALL acomodar o intervalo de 1 a 5.

### Requisito 8: Renumeração consistente de serviços, API, tempo real e frontend

**História de usuário:** Como Pesquisador, quero que todas as camadas reflitam os cinco Estágios na ordem correta, para que a interface, as rotas e os canais de tempo real sejam coerentes.

#### Critérios de Aceitação

1. THE StageService SHALL generalizar o intervalo de Estágios para 1 a 5 e derivar bloqueio sequencial, progresso agregado e derivação de estado a partir das constantes de Estágio, sem literais espalhados.
2. THE operação de Modelagem Estrutural (antiga `run_stage1`/`read_stage1`) SHALL persistir/ler o `StageRun` de `estagio=1`.
3. THE operação de Geração do DAG (escolha/carga do DAG) SHALL persistir/ler o `StageRun` de `estagio=2`.
4. THE operação de Operacionalização SHALL persistir/ler o `StageRun` de `estagio=3`.
5. THE operação de Provisionamento do Ambiente SHALL persistir/ler o `StageRun` de `estagio=4`.
6. THE operação de Execução da Campanha SHALL persistir/ler o `StageRun` de `estagio=5`.
7. THE API REST SHALL expor as rotas de Estágio renumeradas para 1 a 5, com contratos pt-BR e semântica (somente-leitura onde aplicável; bloqueio sequencial) preservadas.
8. THE canal WebSocket de Estágios SHALL reportar o estado/progresso de qualquer Estágio no intervalo de 1 a 5.
9. THE Frontend SHALL apresentar os cinco Estágios no wizard, com o indicador de passo "Passo X de 5", a trilha de badges dos cinco Estágios e o corpo correto para cada Estágio.
10. THE i18n SHALL expor as chaves dos cinco Estágios com os nomes de design em pt-BR ("Modelagem Estrutural", "Geração do DAG", "Operacionalização", "Provisionamento do Ambiente", "Execução da Campanha") e os equivalentes em inglês, mantendo paridade de chaves entre os idiomas.

### Requisito 9: Casos curados pré-concluídos nos Estágios 2 e 4

**História de usuário:** Como Pesquisador, quero que os casos curados já apareçam com os Estágios 2 (DAG) e 4 (Ambiente) concluídos, para que eu possa rodar as campanhas existentes sem produzir manualmente o que o colega já produziu.

#### Critérios de Aceitação

1. WHEN um Caso curado tem DAG disponível e seu Estágio 1 está `concluido`, THE Backend SHALL marcar o Estágio 2 desse Caso como `concluido` carregando o DAG curado.
2. WHEN um Caso curado tem arquivo de configuração de ambiente disponível e seu Estágio 3 está `concluido`, THE Backend SHALL marcar o Estágio 4 desse Caso como `concluido` após verificar o isolamento do ambiente.
3. THE conclusão automática dos Estágios 2 e 4 SHALL respeitar o bloqueio sequencial (o Estágio só conclui quando seu Estágio anterior estiver `concluido`).
4. THE conclusão automática NÃO SHALL ocorrer para um Caso sem o material curado correspondente (DAG ausente para o Estágio 2; arquivo de ambiente ausente para o Estágio 4).

### Requisito 10: Compatibilidade com a transição para o agente de IA (não-objetivo de implementação)

**História de usuário:** Como mantenedor, quero que as costuras de extensão estejam prontas para o agente de IA, para que a futura substituição da curadoria não exija redesenho — mas sem implementar o agente agora.

#### Critérios de Aceitação

1. THE costura `GeradorDeDag` (Estágio 2) SHALL admitir uma implementação de IA encaixável que receba a Modelagem Estrutural e produza DAG(s) com `origem = gerado_ia`, sem alterar a assinatura nem a entidade DAG.
2. THE costura de Operacionalização (Estágio 3) e a de Provisionamento do Ambiente (Estágio 4) SHALL ser desenhadas para admitir implementações futuras por IA sem redesenho.
3. THE implementação do agente de IA, a remoção da curadoria e o provisionamento/operacionalização dinâmicos por IA SHALL ser **não-objetivos** desta iteração.
4. THE esquema persistido SHALL aceitar `origem = gerado_ia` sem exigir nova migração quando o agente for implementado.
