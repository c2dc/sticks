# Design Document

Entrada do Estágio 1 = recorte da campanha no pacote STIX

## Overview

A Entrada do Estágio 1 (Modelagem Estrutural) passa a exibir o **objeto
`campaign`** do pacote STIX da campanha selecionada, lido da **base STIX
selecionada no fluxo**. O backend ganha a capacidade de recortar esse objeto do
bundle (reutilizando a infraestrutura já existente em `StixService`), e o
endpoint de leitura do Estágio 1 passa a aceitar, opcionalmente, a versão da base.
O frontend propaga a `versaoSelecionada` até o Estágio 1 e renderiza um bloco de
identidade da campanha STIX, com fallback claro quando o objeto não é encontrado.

A extração estrutural (técnicas/relações) do Estágio 1 permanece **inalterada**:
continua derivando do DAG curado em disco. Esta mudança afeta apenas a *Entrada*
(informativa) e seu contrato.

### Decisões (confirmadas com o usuário)

1. **Base de origem**: a base STIX **selecionada no fluxo** (`versaoSelecionada`),
   propagada front→back até o Estágio 1. (Rejeitado: backend escolher a base
   automaticamente — poderia divergir da base escolhida pelo Pesquisador.)
2. **Campos exibidos**: nome, ID STIX (`campaign--…`), external_id (ex. `C0045`),
   descrição, aliases e datas (first_seen/last_seen) quando presentes, mais a
   versão da base. Campos ausentes são omitidos.
3. **Fallback**: se o objeto `campaign` não for encontrado na base selecionada
   (ou a base não puder ser lida, ou a versão não vier), exibir aviso claro e
   cair para o identificador curado (nome + external_id/slug), sem quebrar o
   Estágio 1 nem a Modelagem.

## Architecture

```mermaid
flowchart LR
  App[App.tsx\nversaoSelecionada + slug] --> Wizard[ExecutionWizard]
  Wizard --> S1[Stage1View]
  S1 -->|getEstagio1(caso, versao?)| API[GET /casos/{caso}/estagio/1?versao=]
  API --> SS[StageService.read_stage1]
  SS -->|resolve slug→campaign| STIX[StixService\nler_bundle + _mapear_campanha + SLUG_PARA_STIX]
  STIX --> SS
  SS --> API --> S1
```

- **Propagação da versão**: `App.tsx` já tem `versaoSelecionada` e passa o `slug`
  ao `ExecutionWizard`; passará também a `versao`. O `Stage1View` chama
  `getEstagio1(caso, versao?)`.
- **Backend**: o endpoint de leitura do Estágio 1 aceita `versao` opcional
  (query param). `StageService` monta a Entrada e, para o bloco de campanha STIX,
  delega ao `StixService` para localizar o objeto `campaign` na base informada.
- **Resolução**: reutiliza `SLUG_PARA_STIX` (external_id → nome canônico) e
  `_mapear_campanha`/`ler_bundle` já existentes.

## Components and Interfaces

### Backend

- **Novo DTO `CampanhaStixEntrada`** (bloco de identidade da campanha STIX),
  campos pt-BR, todos opcionais exceto a marcação de disponibilidade:
  - `encontrada: bool` — se o objeto `campaign` foi localizado na base.
  - `versao_base: Optional[str]` — versão da base STIX usada.
  - `stix_id`, `nome`, `external_id`, `descricao: Optional[str]`
  - `aliases: list[str]`
  - `first_seen`, `last_seen: Optional[str]`
  - `motivo_indisponivel: Optional[str]` — razão pt-BR do fallback (ex.: base não
    informada / campanha não encontrada / bundle inválido).
- **`Stage1Input`** ganha um campo `campanha_stix: Optional[CampanhaStixEntrada]`.
  Os campos legados `dag_file`/`stix_reference` permanecem no DTO por
  compatibilidade de contrato, mas deixam de ser usados pela UI (o `stix_reference`
  deixa de ser exibido; a UI passa a ler `campanha_stix`).
- **`StixService`**: novo método `obter_campanha_por_slug(versao, slug) ->
  Optional[CampanhaStixDetalhadaInterna]` que lê o bundle, encontra o objeto
  `campaign` cujo slug resolvido == slug (via a lógica de `_resolver_slug_curado`)
  e extrai os campos ricos (`external_id` a partir das `external_references`,
  `aliases`, `first_seen`, `last_seen`). Reaproveita `ler_bundle` e a resolução
  existente; não duplica regra.
- **`StageService.read_stage1` / `run_stage1`**: aceitam `versao: Optional[str]`
  e preenchem `entrada.campanha_stix`:
  - se `versao` ausente → `encontrada=False`, `motivo_indisponivel="versao_ausente"`;
  - se bundle ilegível/ inválido → `encontrada=False`,
    `motivo_indisponivel="bundle_indisponivel"` (sem propagar erro — a Entrada é
    informativa);
  - se objeto não achado → `encontrada=False`,
    `motivo_indisponivel="campanha_nao_encontrada"`, ainda carregando o
    identificador curado (nome + external_id do `SLUG_PARA_STIX`);
  - se achado → `encontrada=True` com os campos ricos.
- **Endpoint** `GET /api/casos/{caso}/estagio/1`: adiciona query param opcional
  `versao`. Retrocompatível (sem `versao` → fallback). O POST
  `/estagio/1/executar` também aceita `versao` opcional para enriquecer a Entrada
  do resultado (sem afetar a extração).

### Frontend

- **`lib/estagios.ts`**: `Stage1Entrada` ganha `campanha_stix?: CampanhaStixEntrada`
  (tipo espelhando o DTO). `getEstagio1(caso, versao?)` acrescenta `?versao=` quando
  informado. O campo `dag_file` deixa de ser exibido (já removido em mudança
  anterior) e `stix_reference` deixa de ser consumido pela UI.
- **`App.tsx`**: passa `versao={estado.versaoSelecionada}` ao `ExecutionWizard`.
- **`ExecutionWizard`**: repassa `versao` ao `Stage1View`.
- **`Stage1View`**: a seção **Entrada** passa a renderizar o bloco
  `campanha_stix`:
  - quando `encontrada` → nome, ID STIX, external_id, descrição (legível,
    com `whitespace-pre-wrap`/limite de altura com scroll), aliases (chips),
    datas, e a versão da base;
  - quando `!encontrada` → aviso pt-BR/en claro + o identificador curado
    disponível (nome + external_id/slug), sem quebrar a tela.

## Data Models

`CampanhaStixEntrada` (pt-BR), exemplo quando encontrada:

```json
{
  "encontrada": true,
  "versao_base": "18.0",
  "stix_id": "campaign--…",
  "nome": "ShadowRay",
  "external_id": "C0045",
  "descricao": "…",
  "aliases": ["…"],
  "first_seen": "…",
  "last_seen": "…",
  "motivo_indisponivel": null
}
```

Quando não encontrada (fallback):

```json
{
  "encontrada": false,
  "versao_base": "18.0",
  "stix_id": null,
  "nome": "ShadowRay",
  "external_id": "C0045",
  "descricao": null,
  "aliases": [],
  "first_seen": null,
  "last_seen": null,
  "motivo_indisponivel": "campanha_nao_encontrada"
}
```

## Error Handling

- A Entrada é **informativa**: nenhuma falha de leitura do bundle, versão ausente
  ou campanha não encontrada pode quebrar o Estágio 1 ou impedir a Modelagem. Em
  todos esses casos, `campanha_stix.encontrada=false` com `motivo_indisponivel`
  apropriado.
- O endpoint não deve retornar erro HTTP por `versao` ausente/ inválida; apenas
  reflete o fallback no corpo.
- A extração estrutural segue com suas próprias falhas já modeladas
  (`estado=erro`, `etapa_falha`), independentes do bloco de campanha STIX.

## Testing Strategy

- **Backend (pytest)**:
  - `obter_campanha_por_slug`: encontra por external_id e por nome canônico;
    extrai aliases/datas/descrição; retorna `None` quando ausente.
  - `read_stage1`/`run_stage1`: preenche `campanha_stix` nos quatro cenários
    (encontrada / versão ausente / bundle indisponível / não encontrada) e a
    extração estrutural permanece inalterada em todos.
  - Endpoint `GET …/estagio/1?versao=`: contrato com e sem `versao`
    (retrocompatível), e os motivos de fallback.
- **Frontend (Vitest)**:
  - `Stage1View` renderiza o bloco rico quando `encontrada`; omite campos
    ausentes; mostra o aviso + identificador curado quando `!encontrada`; não
    exibe `campaign_file`/Caldera.
  - `getEstagio1(caso, versao?)` monta a URL com/sem `?versao=`.
  - `ExecutionWizard`/`App` propagam a versão.
  - i18n: paridade pt-BR/en das novas chaves.
- As suítes existentes permanecem verdes (backend pytest, Vitest, typecheck).

## Correctness Properties

### Property 1: A Entrada nunca quebra a Modelagem
**Validates: Requirements 4.3, 1.1**

Para qualquer combinação de (versão ausente/presente, bundle legível/ilegível,
campanha encontrada/não), `read_stage1`/`run_stage1` sempre retornam uma Entrada
válida com `campanha_stix` preenchido (encontrada=true/false) e a extração
estrutural segue seu curso normal (nunca falha por causa do bloco de campanha).

### Property 2: Resolução consistente com o endpoint de campanhas
**Validates: Requirements 4.1, 2.1**

Para todo slug curado presente em `SLUG_PARA_STIX`, o objeto `campaign` resolvido
por `obter_campanha_por_slug` em uma base que o contém é o **mesmo** (mesmo
`stix_id`) que o resolvido por `listar_campanhas`/`_mapear_campanha` para aquele
slug — a regra de resolução é única e não diverge entre as telas.

### Property 3: Nenhum artefato Caldera na Entrada
**Validates: Requirements 1.2, 1.3**

Para qualquer caso, a Entrada do Estágio 1 exposta pela API e renderizada pela UI
não contém caminhos de adversário da Caldera (`data/caldera_adversaries/*.yml`)
nem o antigo rótulo "Arquivo DAG"/"Referência STIX" apontando para não-STIX.
