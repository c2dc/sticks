# Requirements Document

Entrada do Estágio 1 = recorte da campanha no pacote STIX

## Introduction

No pipeline de cinco Estágios, o **Estágio 1 é a Modelagem Estrutural** e sua
*entrada conceitual* é a **inteligência STIX** — mais especificamente, o recorte
do pacote STIX que descreve a **campanha** que o Pesquisador selecionou. Hoje a
tela do Estágio 1 mostra, como "Referência STIX", o valor de
`DagData.campaign_file`, que na prática aponta para um arquivo de **adversário da
Caldera** (ex.: `data/caldera_adversaries/0.shadowray.yml`). Isso é um resíduo do
esquema antigo: está com o rótulo errado, com o valor errado (artefato de
estágios posteriores, não a entrada da Modelagem) e confunde o usuário.

Esta spec corrige a Entrada do Estágio 1 para exibir o **objeto `campaign` do
pacote STIX** da campanha selecionada — o recorte do pacote correspondente àquela
campanha — lido da **base STIX que o Pesquisador selecionou no fluxo**. O pacote
STIX é inteiro (uma base por versão); a campanha é um objeto `campaign` dentro
dele, identificado de forma tolerante (por `external_id`, ex. `C0045`, ou pelo
nome canônico), reutilizando o mapeamento `SLUG_PARA_STIX` já existente no
backend.

Escopo: frontend (propagar a versão da base até o Estágio 1 e renderizar os
campos do objeto `campaign`) e backend (expor os campos do objeto `campaign` na
Entrada do Estágio 1, lendo o bundle da base selecionada). Fora de escopo:
alterar a lógica de extração estrutural (técnicas/relações) do Estágio 1, que
continua derivando do DAG curado; alterar os demais Estágios.

## Glossary

- **Base STIX**: o pacote STIX inteiro de uma versão ATT&CK Enterprise (ex.:
  "18.0"), baixado em disco.
- **Objeto `campaign`**: o objeto STIX de `type == "campaign"` dentro do bundle,
  com `id` (ex.: `campaign--…`), `name`, `description`, `external_references`
  (incluindo `external_id` como `C0045`), `aliases`, `first_seen`/`last_seen`.
- **Caso curado (slug)**: a identidade usada hoje pelo Estágio 1 (ex.:
  `shadowray`), mapeada ao objeto `campaign` via `SLUG_PARA_STIX`.
- **Versão selecionada**: a base STIX que o Pesquisador escolheu no fluxo
  (`versaoSelecionada` no estado do App).

## Requirements

### Requisito 1 — Entrada do Estágio 1 é o recorte da campanha no pacote STIX

**User story:** Como Pesquisador, quero ver na Entrada do Estágio 1 o recorte do
pacote STIX que descreve a campanha que selecionei, para entender exatamente qual
é a inteligência de origem da Modelagem Estrutural.

#### Critérios de aceitação
1. QUANDO o Estágio 1 é exibido para uma campanha curada ENTÃO a Entrada DEVE
   apresentar os dados do **objeto `campaign`** da base STIX selecionada, e NÃO o
   `campaign_file` da Caldera.
2. O sistema NÃO DEVE exibir, na Entrada do Estágio 1, nenhum caminho de arquivo
   de adversário da Caldera (ex.: `data/caldera_adversaries/*.yml`).
3. A Entrada DEVE deixar explícito que a origem é a inteligência STIX da campanha
   (rótulos corretos; sem o rótulo "Arquivo DAG" nem "Referência STIX" apontando
   para artefatos não-STIX).

### Requisito 2 — Campos exibidos do objeto `campaign`

**User story:** Como Pesquisador, quero ver os campos relevantes da campanha STIX
para reconhecê-la sem ambiguidade.

#### Critérios de aceitação
1. QUANDO o objeto `campaign` é encontrado ENTÃO a Entrada DEVE exibir, quando
   presentes: o **nome** da campanha, o **ID STIX** (`id`, ex.:
   `campaign--…`), o **external_id** (ex.: `C0045`), a **descrição**, os
   **aliases** e as datas **first_seen**/**last_seen**.
2. A Entrada DEVE exibir a **versão da base STIX** de onde o objeto foi lido
   (ex.: "18.0").
3. Campos ausentes no objeto `campaign` NÃO DEVEM ser renderizados como vazios
   confusos; o sistema DEVE omiti-los ou indicar ausência de forma clara.
4. A descrição, possivelmente longa, DEVE ser exibida de forma legível (sem
   quebrar o layout).

### Requisito 3 — Base STIX de origem é a selecionada no fluxo

**User story:** Como Pesquisador, quero que o recorte da campanha venha da mesma
base STIX que escolhi no fluxo, para haver consistência entre as telas.

#### Critérios de aceitação
1. O sistema DEVE ler o objeto `campaign` da **base STIX selecionada**
   (`versaoSelecionada`) propagada do fluxo até o Estágio 1.
2. SE a versão selecionada não estiver disponível no estado (ex.: acesso direto
   ao Estágio 1 sem passar pela seleção) ENTÃO o sistema DEVE degradar de forma
   graciosa conforme o Requisito 4 (sem quebrar o Estágio 1).

### Requisito 4 — Resolução tolerante e fallback claro

**User story:** Como Pesquisador, quero que a tela continue funcionando mesmo que
a campanha não exista exatamente na base escolhida, com um aviso claro.

#### Critérios de aceitação
1. O sistema DEVE resolver o objeto `campaign` a partir do slug curado de forma
   tolerante, reutilizando `SLUG_PARA_STIX` (por `external_id`, depois por nome
   canônico).
2. SE nenhum objeto `campaign` correspondente for encontrado na base selecionada
   (ou a base não puder ser lida) ENTÃO a Entrada DEVE exibir um **aviso claro**
   (ex.: "Campanha não encontrada nesta base STIX") E cair para o **identificador
   curado** (nome + external_id/slug) SEM quebrar o Estágio 1 nem impedir a
   Modelagem.
3. A extração estrutural do Estágio 1 (técnicas/relações, derivada do DAG curado)
   NÃO DEVE ser afetada pela presença ou ausência do objeto `campaign` — a
   Entrada é informativa; a Modelagem segue funcionando.

### Requisito 5 — Contrato de API e tipos coerentes

**User story:** Como desenvolvedor, quero um contrato tipado e consistente entre
backend e frontend para a Entrada do Estágio 1.

#### Critérios de aceitação
1. O backend DEVE expor os campos do objeto `campaign` na resposta da Entrada do
   Estágio 1 (um bloco de identidade da campanha STIX), preservando os nomes de
   campo em pt-BR do padrão do projeto.
2. O endpoint de leitura do Estágio 1 DEVE aceitar (de forma opcional e
   retrocompatível) a versão da base STIX a ser usada.
3. O frontend DEVE tipar e consumir esse bloco, e remover do contrato de UI o uso
   do `campaign_file`/"Arquivo DAG" como entrada.
4. A ausência da versão (parâmetro opcional não informado) NÃO DEVE causar erro
   HTTP; aplica-se o fallback do Requisito 4.

### Requisito 6 — Qualidade e i18n

**User story:** Como Pesquisador, quero a tela correta nos dois idiomas e coberta
por testes.

#### Critérios de aceitação
1. Todos os novos rótulos DEVEM existir em pt-BR e en, com paridade de chaves.
2. O comportamento (campos ricos, omissão de ausentes, aviso de fallback) DEVE
   ser coberto por testes automatizados (backend e frontend).
3. As suítes existentes (backend pytest, frontend Vitest, typecheck) DEVEM
   permanecer verdes.
