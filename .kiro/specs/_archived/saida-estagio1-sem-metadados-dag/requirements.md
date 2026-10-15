# Requirements Document

Saída do Estágio 1 sem metadados do DAG

## Introduction

No pipeline de cinco Estágios, o **Estágio 1 é a Modelagem Estrutural** do
pacote STIX (técnicas, relacionamentos, indicadores, infraestrutura e malware) e
o **Estágio 2 é a Geração do DAG**. Hoje a **Saída do Estágio 1** carrega dois
campos — `dag_metadata` e `capability_flow` — que são propriedades do **DAG**, e
não da modelagem estrutural do pacote STIX. Eles são um resíduo do esquema antigo
(de quando o DAG era o Estágio 1): não derivam do pacote STIX, mas sim dos
arquivos de curadoria humana `sticks/data/dag/{slug}_dag.json`.

Além de estarem conceitualmente no estágio errado, esses campos causam um bug de
exibição: o `MetadataBlock` da tela do Estágio 1 renderiza o dicionário aninhado
como texto (`String(value)`), produzindo "[object Object]" para o Pesquisador.

Investigação do código confirma que ambos são **dados mortos na aplicação**:

- `capability_flow` é lido do arquivo DAG e resumido (apenas
  `initial_capabilities` e `final_capabilities`) na Saída do Estágio 1, mas **não
  é consumido** por nenhuma fase: o frontend não o referencia, o Estágio 2
  (`CuradoriaGeradorDeDag._montar_conteudo`) só usa
  `node_id`/`technique_id`/`technique_name`/`tactic`/`parent_nodes`/`child_nodes`,
  e a Saída do Estágio 2 (`DagView`/`DagDetalhe`) só tem nós/arestas/mermaid.
- `dag_metadata` é o bloco `metadata` cru do arquivo DAG, repassado como texto à
  tela. **Exceção crítica:** o subcampo `metadata.validation.is_dag` É usado
  internamente — pela ferramenta `sticks/tools/convert_dag_to_caldera.py` (que
  valida a ausência de ciclos antes de converter) e é a garantia de aciclicidade
  que o `DagService.montar_mermaid` assume. Esse uso interno NÃO depende da Saída
  do Estágio 1 e NÃO pode ser quebrado.

Esta spec é a contraparte, para a **Saída** do Estágio 1, da spec irmã
`entrada-estagio1-campanha-stix` (que corrigiu a **Entrada**). Ela aplica a
**Opção A aprovada**: **remover** `dag_metadata` e `capability_flow` da Saída do
Estágio 1, não exibindo esses campos em NENHUM estágio (não migrar para o
Estágio 2 — são dados mortos na UI). A Saída de metadados do Estágio 1 passa a
conter apenas o que é propriamente da campanha/modelagem: `campaign_name`,
`campaign_file`, `generated_at` e `total_nodes`.

Escopo: backend (parar de emitir os dois campos no bloco de metadados da Saída do
Estágio 1 em `stage_service.py`) e frontend (o `MetadataBlock` em
`Stage1View.tsx` deixa de receber/renderizar esses campos, eliminando o
"[object Object]"). Fora de escopo: alterar a leitura dos arquivos
`sticks/data/dag/{slug}_dag.json`, mudar a extração estrutural do Estágio 1,
alterar o uso interno de `metadata.validation.is_dag`, ou alterar qualquer outro
Estágio.

## Glossary

- **Estágio 1 (Modelagem Estrutural)**: estágio que extrai do pacote STIX as
  técnicas, relacionamentos, indicadores, infraestrutura e malware da campanha.
- **Estágio 2 (Geração do DAG)**: estágio que produz o grafo acíclico dirigido;
  é dele que `dag_metadata` e `capability_flow` são conceitualmente propriedade.
- **Saída do Estágio 1**: o objeto `Stage1Output` montado por `StageService`,
  cujo bloco `metadata` é construído por `_extract_metadata(dag)` em
  `stage_service.py`.
- **`dag_metadata`**: cópia crua do bloco `metadata` do arquivo
  `sticks/data/dag/{slug}_dag.json`, hoje incluída na Saída do Estágio 1.
- **`capability_flow`**: resumo (`initial_capabilities`/`final_capabilities`) do
  fluxo de capacidades do arquivo DAG, hoje incluído na Saída do Estágio 1.
- **`metadata.validation.is_dag`**: subcampo do arquivo DAG que afirma a
  aciclicidade do grafo; usado por `convert_dag_to_caldera.py` e assumido por
  `DagService.montar_mermaid`. Uso interno, independente da Saída do Estágio 1.
- **`MetadataBlock`**: subcomponente de `Stage1View.tsx` que renderiza o bloco de
  metadados da Saída do Estágio 1 par chave/valor via `String(value)`.
- **Pesquisador**: ator humano que opera a Pipeline_UI e visualiza os Estágios.
- **Resíduo do esquema antigo**: campo herdado de quando o DAG era o Estágio 1,
  hoje fora de lugar na Modelagem Estrutural.

## Requirements

### Requisito 1 — Saída do Estágio 1 não emite metadados do DAG

**User story:** Como Pesquisador, quero que a Saída do Estágio 1 apresente apenas
metadados da campanha/modelagem, para que a Modelagem Estrutural não misture
propriedades do DAG que pertencem ao Estágio 2.

#### Critérios de aceitação
1. QUANDO a Saída do Estágio 1 é montada para uma campanha curada ENTÃO o bloco
   de metadados NÃO DEVE conter o campo `dag_metadata`.
2. QUANDO a Saída do Estágio 1 é montada para uma campanha curada ENTÃO o bloco
   de metadados NÃO DEVE conter o campo `capability_flow`.
3. QUANDO a Saída do Estágio 1 é montada ENTÃO o bloco de metadados DEVE conter
   exatamente os campos `campaign_name`, `campaign_file`, `generated_at` e
   `total_nodes`.
4. O `_extract_metadata(dag)` em `stage_service.py` NÃO DEVE adicionar
   `dag_metadata` nem `capability_flow` ao bloco de metadados, mesmo quando o
   arquivo DAG contiver `metadata` ou `capability_flow`.

### Requisito 2 — Leitura do arquivo DAG preservada

**User story:** Como desenvolvedor, quero que a leitura dos arquivos
`sticks/data/dag/{slug}_dag.json` continue inalterada, para que apenas a
propagação dos campos para a Saída mude, sem efeitos colaterais na leitura.

#### Critérios de aceitação
1. O sistema DEVE continuar lendo o arquivo `sticks/data/dag/{slug}_dag.json`
   como está hoje, incluindo os blocos `metadata` e `capability_flow`, sem
   alterar `CaseService`/`DagData`.
2. A remoção de `dag_metadata` e `capability_flow` DEVE ocorrer apenas na
   montagem da Saída do Estágio 1, e NÃO na etapa de leitura/parse do arquivo.

### Requisito 3 — Uso interno de `metadata.validation.is_dag` preservado

**User story:** Como desenvolvedor, quero que a garantia de aciclicidade
continue intacta, para que a conversão para Caldera e a montagem do Mermaid não
quebrem com esta mudança.

#### Critérios de aceitação
1. A ferramenta `sticks/tools/convert_dag_to_caldera.py` DEVE continuar lendo
   `metadata.validation.is_dag` do arquivo DAG para validar a ausência de ciclos
   antes da conversão.
2. A garantia de aciclicidade assumida por `DagService.montar_mermaid` (derivada
   de `metadata.validation.is_dag`) DEVE permanecer válida após esta mudança.
3. A remoção de `dag_metadata` da Saída do Estágio 1 NÃO DEVE afetar nenhum
   acesso interno a `metadata.validation.is_dag`.

### Requisito 4 — Extração estrutural inalterada

**User story:** Como Pesquisador, quero que as técnicas, relacionamentos,
indicadores, infraestrutura e malware da Saída do Estágio 1 continuem iguais,
para que a correção não altere o produto da Modelagem Estrutural.

#### Critérios de aceitação
1. A Saída do Estágio 1 DEVE continuar apresentando as mesmas técnicas,
   relacionamentos, indicadores, infraestrutura e malware extraídos hoje.
2. A remoção de `dag_metadata` e `capability_flow` NÃO DEVE alterar os campos
   `techniques`, `relationships`, `indicators`, `infrastructure`, `malware`,
   `is_empty` nem `note` do `Stage1Output`.
3. Nenhum Estágio além do Estágio 1 DEVE ser alterado por esta mudança.

### Requisito 5 — Tela do Estágio 1 sem "[object Object]"

**User story:** Como Pesquisador, quero ver o bloco de metadados do Estágio 1 sem
valores "[object Object]", para que a tela mostre apenas informação legível.

#### Critérios de aceitação
1. QUANDO a tela do Estágio 1 renderiza o bloco de metadados ENTÃO o
   `MetadataBlock` NÃO DEVE exibir nenhum valor "[object Object]".
2. QUANDO a tela do Estágio 1 renderiza o bloco de metadados ENTÃO o
   `MetadataBlock` DEVE exibir apenas os campos `campaign_name`, `campaign_file`,
   `generated_at` e `total_nodes`.
3. O `MetadataBlock` em `Stage1View.tsx` DEVE deixar de receber/renderizar
   `dag_metadata` e `capability_flow`.
4. SE o bloco de metadados estiver vazio ENTÃO o `MetadataBlock` NÃO DEVE
   renderizar seção alguma (comportamento atual de omissão quando não há
   entradas).

### Requisito 6 — Contrato coerente entre backend e frontend

**User story:** Como desenvolvedor, quero um contrato tipado e consistente entre
backend e frontend para o bloco de metadados do Estágio 1, para evitar campos
órfãos no contrato de UI.

#### Critérios de aceitação
1. O backend DEVE expor na Saída do Estágio 1 um bloco de metadados restrito aos
   campos `campaign_name`, `campaign_file`, `generated_at` e `total_nodes`.
2. O frontend NÃO DEVE referenciar `dag_metadata` nem `capability_flow` no
   contrato/tipos consumidos pela tela do Estágio 1.
3. A ausência dos campos removidos NÃO DEVE causar erro de renderização nem erro
   HTTP na leitura do Estágio 1.

### Requisito 7 — Qualidade e testes

**User story:** Como Pesquisador, quero a correção coberta por testes, para que a
regressão (reintrodução dos campos ou retorno do "[object Object]") seja
detectada automaticamente.

#### Critérios de aceitação
1. O comportamento (ausência de `dag_metadata`/`capability_flow` na Saída e
   presença apenas dos quatro campos de campanha) DEVE ser coberto por testes
   automatizados de backend.
2. O comportamento da tela (ausência de "[object Object]" e dos campos removidos
   no `MetadataBlock`) DEVE ser coberto por testes automatizados de frontend.
3. As suítes existentes (backend pytest, frontend Vitest, typecheck) DEVEM
   permanecer verdes.
