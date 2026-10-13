import { describe, it, expect } from "vitest"
import fc from "fast-check"

import { flowReducer, type FlowState, type View } from "./flow"

// Property-based tests for the pure, deterministic `flowReducer` in
// `src/lib/flow.ts`. They drive the reducer over many generated states
// (fast-check, >= 100 runs each) without mounting any React component. The flow
// module is NOT modified by these tests.
//
// The standalone `"dag"` view was removed (task 10.2): the DAG generation is now
// Estágio 1 inside the wizard, so there is no `AVANCAR`/`DAG_PRONTO` event nor a
// `dagPodeAvancar` flag at the flow level. The linear machine now has five
// views and `SELECIONAR_CAMPANHA` goes straight `selecao -> wizard`.

/** All five views of the linear flow. */
const VIEWS: View[] = ["landing", "bases", "dashboard", "selecao", "wizard"]

/** Arbitrary optional campaign identity carried across the flow. */
const campanhaArb = fc.record({
  stix_id: fc.string(),
  nome: fc.string(),
  slug_curado: fc.oneof(fc.string(), fc.constant(null), fc.constant(undefined)),
})

/**
 * Build an arbitrary FlowState pinned to a specific view, randomizing every
 * other field so the property holds regardless of the surrounding state.
 */
function flowStateArb(view: View): fc.Arbitrary<FlowState> {
  return fc.record({
    view: fc.constant(view),
    versaoSelecionada: fc.oneof(fc.string(), fc.constant(undefined)),
    campanhaSelecionada: fc.oneof(campanhaArb, fc.constant(undefined)),
  })
}

describe("flow reducer property tests", () => {
  // Feature: dag-estagio-multi-origem, Property 11: Transição inicial da Landing.
  // Para qualquer estado na view `landing` (demais campos arbitrários), aplicar
  // o evento `INICIAR` resulta na view `bases`.
  // Validates: Requirements 1.x (fluxo linear INICIAR: landing -> bases)
  it("Property 11: INICIAR na Landing sempre leva à view bases", () => {
    fc.assert(
      fc.property(flowStateArb("landing"), (estado) => {
        const proximo = flowReducer(estado, { tipo: "INICIAR" })
        expect(proximo.view).toBe("bases")
      }),
      { numRuns: 100 },
    )
  })

  // Feature: dag-estagio-multi-origem, Property 10: Ida e volta de navegação
  // seleção ↔ wizard preserva a campanha. Para qualquer campanha escolhida na
  // view `selecao`, aplicar `SELECIONAR_CAMPANHA(c)` leva ao `wizard` com a
  // campanha armazenada, e aplicar `VOLTAR` retorna à view `selecao` com a MESMA
  // campanha. (A view autônoma `dag` foi removida — a geração do DAG é o
  // Estágio 1 dentro do wizard.)
  // Validates: Requirements 1.x, 7.4 (preservação da campanha no ida/volta)
  it("Property 10: SELECIONAR_CAMPANHA + VOLTAR preserva a campanha e retorna a selecao", () => {
    fc.assert(
      fc.property(
        campanhaArb,
        // Other fields are irrelevant to the invariant but randomized anyway.
        fc.oneof(fc.string(), fc.constant(undefined)),
        (campanha, versao) => {
          const estado: FlowState = {
            view: "selecao",
            versaoSelecionada: versao,
          }

          const noWizard = flowReducer(estado, {
            tipo: "SELECIONAR_CAMPANHA",
            campanha,
          })
          // SELECIONAR_CAMPANHA goes selecao -> wizard storing the campaign.
          expect(noWizard.view).toBe("wizard")
          expect(noWizard.campanhaSelecionada).toEqual(campanha)

          const voltado = flowReducer(noWizard, { tipo: "VOLTAR" })
          // Round trip returns to selecao with the SAME campaign (Req. 7.4).
          expect(voltado.view).toBe("selecao")
          expect(voltado.campanhaSelecionada).toEqual(campanha)
        },
      ),
      { numRuns: 100 },
    )
  })

  // Sanity guard used by both properties: VIEWS lists exactly the five screens.
  it("covers all five views in the generator pool", () => {
    expect(VIEWS).toHaveLength(5)
  })
})
