# Implementation Plan

- [x] 1. Adicionar a derivação pura `derivarApresentacao` em `lib/wizard.ts`
  - Definir os tipos de apresentação `EstadoApresentado`
    (`"concluido" | "disponivel" | "bloqueado"`), `EstagioApresentado`
    (`{ estagio, apresentado, preConcluido }`) e `ApresentacaoDerivada`
    (`{ estagios: EstagioApresentado[] }`).
  - Implementar `derivarApresentacao(concluidos, maxEstagioVisitado, estagioAtual)`
    **reutilizando** `derivarEstados(concluidos)` para o `status` base (não
    reimplementar o gate). Regra por estágio N: se `status==="concluido"` e
    `N <= maxEstagioVisitado` → `"concluido"`; se `status==="concluido"` e
    `N > maxEstagioVisitado` → `"disponivel"`; caso contrário repassar `status`.
  - Calcular `preConcluido(N)` ≙ `apresentado(N)==="concluido"` ∧ `N>1` ∧
    `concluidos.includes(N)` ∧ `concluidos.includes(N-1)` ∧ `estagioAtual <= N`.
  - Função pura, sem efeitos; defensiva a `concluidos` fora de 1..5 (ignorados
    pela base) e a `concluidos` vazio.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 3.1, 3.4, 4.1, 4.3, 6.1, 7.3, 7.4_

- [x] 2. Testes de propriedade da derivação (fast-check, ≥100 iterações)
  - [x] 2.1 Property 1 — "concluído" exige conclusão real E alcance
    - fast-check: ∀ `concluidos` (subconjunto de {1..5}), `maxEstagioVisitado`,
      `estagioAtual`: `apresentado(N)==="concluido"` ⇔
      `concluidos.includes(N) && N<=maxEstagioVisitado`; corolários (concluído no
      real mas não alcançado → `"disponivel"`; não concluído no real → nunca
      `"concluido"`).
    - Tag: `// Feature: wizard-progresso-estagios, Property 1: ...`
    - _Requirements: 1.1, 1.2, 4.1_
  - [x] 2.2 Property 2 — revelação monotônica ao avançar
    - fast-check: ∀ `concluidos`, ∀ `max1<=max2`: a apresentação em `max2` é
      não decrescente em progresso por estágio vs `max1`; nenhum estágio além do
      visitado é `"concluido"`.
    - Tag: `// Feature: wizard-progresso-estagios, Property 2: ...`
    - _Requirements: 1.3_
  - [x] 2.3 Property 3 — avanço governado pelo Estado_Real
    - fast-check: ∀ `concluidos`, ∀ `maxA,maxB`: `derivarEstados(concluidos)
      .podeAvancar(n)` é idêntico (invariante a `maxEstagioVisitado`).
    - Tag: `// Feature: wizard-progresso-estagios, Property 3: ...`
    - _Requirements: 3.4, 7.3_
  - [x] 2.4 Property 4 — `preConcluido` só marca conclusões automáticas
    - fast-check: ∀ inputs: `preConcluido(N)` ⇒ `concluidos` tem N e N−1, `N>1`,
      `apresentado(N)==="concluido"`, `estagioAtual<=N`; estágio não concluído no
      real nunca é `preConcluido`.
    - Tag: `// Feature: wizard-progresso-estagios, Property 4: ...`
    - _Requirements: 2.1, 4.3, 7.4_
  - [x] 2.5 Property 5 — pureza e determinismo
    - fast-check: ∀ inputs: duas chamadas iguais ⇒ saídas iguais; sem efeitos.
    - Tag: `// Feature: wizard-progresso-estagios, Property 5: ...`
    - _Requirements: 6.1_

- [x] 3. Testes de exemplo/unitários da derivação
  - R1.1: `concluidos={1,2}`, `max=1`, `atual=1` ⇒ Estágio 2 `"disponivel"`.
  - R1.2/R2.1: `concluidos={1,2}`, `max=2`, `atual=2` ⇒ Estágio 2 `"concluido"`
    e `preConcluido=true`.
  - R4.1: `concluidos={1}` ⇒ Estágio 2 nunca `"concluido"`, `preConcluido=false`.
  - _Requirements: 1.1, 1.2, 2.1, 4.1_

- [x] 4. Adicionar o adorno "pré-concluído" em `StageStateBadge.tsx`
  - Adicionar prop opcional `preConcluido?: boolean` (default `false`).
  - Quando `estado==="concluido"` e `preConcluido`: usar o rótulo i18n
    `state.preConcluido`, estilo distinguível do "concluído por você" (ex.:
    variação team-blue + ícone distinto do `CheckCircle2`), `aria-label` no novo
    rótulo e `data-state="pre_concluido"`.
  - `preConcluido` ignorado quando `estado !== "concluido"` (sem regressão nos
    estados existentes).
  - _Requirements: 2.1, 5.1, 5.3, 7.4_

- [x] 5. Rastrear o avanço do pesquisador no `ExecutionWizard.tsx`
  - Adicionar `const [maxEstagioVisitado, setMaxEstagioVisitado] =
    useState<NumeroEstagio>(1)`.
  - `handleNext`: após avançar, `setMaxEstagioVisitado(m => max(m, novoAtual))`.
  - `handleBack`: **não** reduz `maxEstagioVisitado` (monotônico).
  - `useEffect([casoId])`: resetar `maxEstagioVisitado=1` junto com
    `setEstagioAtual(1)`.
  - `handleRestart`: resetar `maxEstagioVisitado=1` junto com `setEstagioAtual(1)`.
  - _Requirements: 1.3, 6.2, 6.3_

- [x] 6. Consumir a apresentação na trilha de badges e na janela do estágio
  - Substituir o mapeamento inline atual (que colapsa tudo em `concluido` /
    `nao_iniciado`) por `derivarApresentacao(concluidos, maxEstagioVisitado,
    estagioAtual)`.
  - Mapear por estágio: `"concluido"` → `StageStateBadge estado="concluido"`
    com `preConcluido` repassado; `"disponivel"` → `estado="nao_iniciado"
    bloqueado={false}`; `"bloqueado"` → `estado="nao_iniciado" bloqueado={true}`.
  - **Não alterar** `podeAvancar` nem o caso especial do Estágio 3 — habilitação
    segue governada por `derivarEstados`.
  - Quando `estagioAtual` for um estágio `preConcluido`, renderizar a nota
    explicativa com `t("wizard.preConcluidoNota")` junto ao corpo do estágio.
  - _Requirements: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 7.3, 7.4_

- [x] 7. Adicionar as chaves i18n com paridade pt-BR/en
  - Em `pt-BR/pipeline.json` e `en/pipeline.json`, adicionar
    `state.preConcluido` e `wizard.preConcluidoNota` com os textos da tabela do
    design (pt-BR e en).
  - Garantir que nenhum texto de progresso novo fique hardcoded fora do
    namespace `pipeline`.
  - _Requirements: 5.1, 5.2, 5.3_

- [x] 8. Testes de componente do wizard e da badge
  - [x] 8.1 Badge pré-concluída distinta (R2.1): `data-state="pre_concluido"`,
    rótulo `state.preConcluido`, estilo distinto do concluído por ação.
    - _Requirements: 2.1, 7.4_
  - [x] 8.2 Nota explicativa (R2.2): abrir Estágio 2 pré-concluído mostra
    `wizard.preConcluidoNota`; caso não-curado (`concluidos={1}`) não mostra.
    - _Requirements: 2.2, 4.3_
  - [x] 8.3 Gate governado pelo real (R3.4): com `maxEstagioVisitado=1` e
    `concluidos={1,2}` (Estágio 2 apresentado `"disponivel"`), o botão "Avançar"
    do Estágio 1 permanece habilitado.
    - _Requirements: 3.4, 7.3_
  - [x] 8.4 Recomputação na releitura (R6.1): mock de `getCaso` muda
    `concluidos` ⇒ a trilha recomputa; sem POST ao marcar `preConcluido` (R2.3).
    - _Requirements: 2.3, 3.1, 6.1_
  - [x] 8.5 Reset na troca de caso e no reinício (R6.2, R6.3): mudar `casoId` e
    acionar reiniciar resetam a trilha ao Estágio 1 e recomputam.
    - _Requirements: 6.2, 6.3_

- [x] 9. Teste de paridade i18n e verificação final
  - Teste assertando que `state.preConcluido` e `wizard.preConcluidoNota` existem
    em ambos os locales (`pt-BR` e `en`).
  - Rodar `vitest --run` (incluindo o `wizard.test.ts` existente, que deve
    permanecer verde — gate sequencial preservado) e `tsc` sem erros.
  - _Requirements: 3.2, 3.3, 4.2, 5.2_
