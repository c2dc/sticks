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
  it("START on the Landing always leads to the wizard view", () => {
    fc.assert(
      fc.property(flowStateArb("landing"), (state) => {
        const next = flowReducer(state, { type: "START" })
        expect(next.view).toBe("wizard")
      }),
      { numRuns: 100 },
    )
  })

  // Property: BACK from the wizard returns to the Landing, and START↔BACK is a
  // clean round trip (landing → wizard → landing).
  it("START + BACK does the round trip landing → wizard → landing", () => {
    fc.assert(
      fc.property(flowStateArb("landing"), (state) => {
        const inWizard = flowReducer(state, { type: "START" })
        expect(inWizard.view).toBe("wizard")

        const back = flowReducer(inWizard, { type: "BACK" })
        expect(back.view).toBe("landing")
      }),
      { numRuns: 100 },
    )
  })

  // Property: BACK on the Landing is a no-op (the floor).
  it("BACK on the Landing is a no-op", () => {
    fc.assert(
      fc.property(flowStateArb("landing"), (state) => {
        const next = flowReducer(state, { type: "BACK" })
        expect(next.view).toBe("landing")
      }),
      { numRuns: 100 },
    )
  })

  // Sanity guard: VIEWS lists exactly the two screens.
  it("covers both views in the generator pool", () => {
    expect(VIEWS).toHaveLength(2)
  })
})
