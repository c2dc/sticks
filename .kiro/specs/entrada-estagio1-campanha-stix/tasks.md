# Implementation Plan

## Overview

Plano para substituir a Entrada do Estágio 1 (hoje mostrando o `campaign_file` da
Caldera) pelo recorte do objeto `campaign` do pacote STIX da campanha
selecionada, lido da base STIX selecionada no fluxo. Trabalho vai do backend
(resolução + DTO + endpoint) ao frontend (propagação da versão + render + i18n),
com um checkpoint final verificando as suítes. A extração estrutural do Estágio 1
não é alterada.

## Task Dependency Graph

```mermaid
flowchart TD
  T1[1. Backend: obter_campanha_por_slug no StixService] --> T2[2. Backend: DTO + preencher Entrada no StageService]
  T2 --> T3[3. Backend: endpoint estagio/1 aceita versao]
  T3 --> T4[4. Backend: checkpoint pytest]
  T4 --> T5[5. Frontend: tipos + wrapper getEstagio1(versao)]
  T5 --> T6[6. Frontend: propagar versao App→Wizard→Stage1View]
  T6 --> T7[7. Frontend: render do bloco campanha_stix + i18n]
  T7 --> T8[8. Checkpoint final - todas as suites verdes]
```

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["3"] },
    { "wave": 4, "tasks": ["4"] },
    { "wave": 5, "tasks": ["5"] },
    { "wave": 6, "tasks": ["6"] },
    { "wave": 7, "tasks": ["7"] },
    { "wave": 8, "tasks": ["8"] }
  ],
  "dependencies": {
    "1": [],
    "2": ["1"],
    "3": ["2"],
    "4": ["3"],
    "5": ["4"],
    "6": ["5"],
    "7": ["6"],
    "8": ["7"]
  }
}
```

## Tasks

- [x] 1. Backend: resolver o objeto `campaign` por slug no `StixService`
  - [x] 1.1 Implementar `obter_campanha_por_slug(versao, slug)` em `app/services/stix/stix_service.py`
    - Ler o bundle da base (`ler_bundle`), varrer os objetos `campaign`, resolver o slug via a regra existente (`_resolver_slug_curado`/`SLUG_PARA_STIX`) e retornar os campos ricos do primeiro `campaign` cujo slug resolvido == slug: `stix_id`, `nome`, `external_id` (de `external_references`), `descricao`, `aliases`, `first_seen`, `last_seen`. Retornar `None` quando não houver correspondência. Não duplicar a regra de resolução.
    - _Requirements: 4.1, 2.1_
  - [x] 1.2 Property/unit tests da resolução
    - Encontra por `external_id` (ex. C0045→shadowray) e por nome canônico; extrai aliases/datas/descrição quando presentes; retorna `None` quando ausente; consistência com `_mapear_campanha` (mesmo `stix_id`). (Property 2)
    - _Requirements: 4.1, 2.1, 6.2_

- [x] 2. Backend: DTO `CampanhaStixEntrada` e preenchimento da Entrada
  - [x] 2.1 Adicionar o DTO e o campo `campanha_stix` em `Stage1Input` (`app/services/stage/stage_service.py`)
    - Definir `CampanhaStixEntrada` (campos pt-BR do design: `encontrada`, `versao_base`, `stix_id`, `nome`, `external_id`, `descricao`, `aliases`, `first_seen`, `last_seen`, `motivo_indisponivel`). Adicionar `campanha_stix: Optional[CampanhaStixEntrada]` em `Stage1Input`. Manter `dag_file`/`stix_reference` por compatibilidade (não mais exibidos pela UI).
    - _Requirements: 5.1, 2.3_
  - [x] 2.2 Preencher `campanha_stix` em `read_stage1`/`run_stage1` com os quatro cenários
    - Aceitar `versao: Optional[str]`. Preencher: encontrada (campos ricos); versão ausente (`motivo_indisponivel="versao_ausente"`); bundle indisponível (`"bundle_indisponivel"`, sem propagar erro); não encontrada (`"campanha_nao_encontrada"` + identificador curado de `SLUG_PARA_STIX`). A extração estrutural NÃO muda. (Property 1, 3)
    - _Requirements: 1.1, 3.1, 4.2, 4.3_

- [x] 3. Backend: endpoint `estagio/1` aceita `versao`
  - [x] 3.1 Adicionar o query param `versao` opcional em `GET /api/casos/{caso}/estagio/1` e no `POST .../estagio/1/executar` (`app/api/casos.py`)
    - Repassar `versao` ao `StageService`. Retrocompatível: sem `versao` aplica o fallback (`versao_ausente`), sem erro HTTP. Incluir `campanha_stix` na resposta.
    - _Requirements: 5.2, 5.4, 3.2_
  - [x] 3.2 Testes de contrato do endpoint
    - Com `versao` válida → `campanha_stix.encontrada=true` e campos; sem `versao` → `encontrada=false`/`versao_ausente`; `versao` sem a campanha → `campanha_nao_encontrada`. Sem erro HTTP em nenhum caso. (Property 1, 3)
    - _Requirements: 5.2, 5.4, 4.2_

- [x] 4. Checkpoint - backend verde
  - Rodar a suíte do backend (`.venv/bin/python -m pytest`) e confirmar verde; `import app.main` ok. Confirmar que a extração estrutural permanece inalterada.
  - _Requirements: 6.3_

- [x] 5. Frontend: tipos e wrapper
  - [x] 5.1 Tipar `CampanhaStixEntrada` e estender `Stage1Entrada` em `src/lib/estagios.ts`; `getEstagio1(caso, versao?)` acrescenta `?versao=`
    - Remover o consumo de `stix_reference` como entrada (o `dag_file` já não é exibido). Preservar nomes pt-BR do contrato.
    - _Requirements: 5.3, 5.2_
  - [x] 5.2 Teste do wrapper
    - `getEstagio1(caso)` e `getEstagio1(caso, "18.0")` montam a URL correta (com/sem `?versao=`).
    - _Requirements: 5.2, 6.2_

- [x] 6. Frontend: propagar a versão selecionada até o Estágio 1
  - [x] 6.1 `App.tsx` passa `versao={estado.versaoSelecionada}` ao `ExecutionWizard`; `ExecutionWizard` repassa ao `Stage1View`
    - Propagação opcional/retrocompatível; ausência de versão cai no fallback.
    - _Requirements: 3.1, 3.2_
  - [x] 6.2 Testes de propagação
    - `ExecutionWizard`/`Stage1View` chamam `getEstagio1` com a versão recebida; sem versão, chamam sem o parâmetro.
    - _Requirements: 3.1, 6.2_

- [x] 7. Frontend: render do bloco `campanha_stix` + i18n
  - [x] 7.1 Atualizar a seção Entrada do `Stage1View` para renderizar `campanha_stix`
    - Encontrada → nome, ID STIX, external_id, descrição legível (scroll/limite), aliases (chips), datas, versão da base; ausentes omitidos. Não encontrada → aviso claro + identificador curado. Nunca exibir caminhos Caldera. (Property 3)
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.3, 2.4, 4.2_
  - [x] 7.2 Chaves i18n (pt-BR/en) com paridade
    - Rótulos: campanha STIX, ID STIX, external_id, descrição, aliases, visto pela primeira/última vez, versão da base, e o aviso de fallback. Paridade pt-BR/en.
    - _Requirements: 6.1_
  - [x] 7.3 Testes de UI do `Stage1View`
    - Renderiza bloco rico quando encontrada; omite ausentes; mostra aviso + identificador quando não encontrada; não mostra `campaign_file`/Caldera. Atualizar testes existentes do Stage1View se necessário.
    - _Requirements: 1.1, 1.2, 2.3, 4.2, 6.2_

- [x] 8. Checkpoint final - todas as suítes verdes
  - Backend pytest, frontend Vitest + typecheck verdes ponta a ponta.
  - _Requirements: 6.3_

## Notes

- A Entrada é informativa: nenhuma falha de leitura do bundle/versão/campanha
  pode quebrar o Estágio 1 ou impedir a Modelagem (Property 1).
- A regra de resolução slug↔campaign é única (reutiliza `SLUG_PARA_STIX`), para
  não divergir do endpoint de campanhas (Property 2).
- A extração estrutural (técnicas/relações) permanece derivando do DAG curado;
  esta spec só muda a *Entrada* e seu contrato.
