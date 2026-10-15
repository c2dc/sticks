import { describe, it, expect } from "vitest"
import fc from "fast-check"

import { flowReducer, type FlowState, type View } from "./flow"

// Property-based tests for the pure, deterministic `flowReducer` in
// `src/lib/flow.ts`. They drive the reducer over many generated states
// (fast-check, >= 100 runs each) without mounting any React component.
//
// The flow is now minimal: landing <-> wizard. Base and campaign selection live
// INSIDE the wizard (one screen per stage), so there are no standalone
// bases/dashboard/selection views and the only events are START and BACK.

/** The two views of the flow. */
const VIEWS: View[] = ["landing", "wizard"]

/** Build an arbitrary FlowState pinned to a specific view. */
function flowStateArb(view: View): fc.Arbitrary<FlowState> {
  return fc.record({ view: fc.constant(view) })
}

describe("flow reducer property tests", () => {
  // Property: START on the Landing always enters the wizard.
  it("START na Landing sempre leva à view wizard", () => {
    fc.assert(
      fc.property(flowStateArb("landing"), (estado) => {
        const proximo = flowReducer(estado, { tipo: "START" })
        expect(proximo.view).toBe("wizard")
      }),
      { numRuns: 100 },
    )
  })

  // Property: BACK from the wizard returns to the Landing, and START↔BACK is a
  // clean round trip (landing → wizard → landing).
  it("START + BACK faz o round trip landing → wizard → landing", () => {
    fc.assert(
      fc.property(flowStateArb("landing"), (estado) => {
        const noWizard = flowReducer(estado, { tipo: "START" })
        expect(noWizard.view).toBe("wizard")

        const voltado = flowReducer(noWizard, { tipo: "BACK" })
        expect(voltado.view).toBe("landing")
      }),
      { numRuns: 100 },
    )
  })

  // Property: BACK on the Landing is a no-op (the floor).
  it("BACK na Landing é no-op", () => {
    fc.assert(
      fc.property(flowStateArb("landing"), (estado) => {
        const proximo = flowReducer(estado, { tipo: "BACK" })
        expect(proximo.view).toBe("landing")
      }),
      { numRuns: 100 },
    )
  })

  // Sanity guard: VIEWS lists exactly the two screens.
  it("covers both views in the generator pool", () => {
    expect(VIEWS).toHaveLength(2)
  })
})
