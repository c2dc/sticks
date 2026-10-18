import { describe, expect, it } from "vitest"

import {
  FIRST_STAGE,
  LAST_STAGE,
  initialNavState,
  resetByReselection,
  wizardNavReducer,
  type WizardNavState,
} from "@/lib/wizard"
import { type StageNumber } from "@/lib/campaigns"

/**
 * Example tests for the Next/Back navigation with reselection of the six-stage
 * wizard (pipeline-six-stages, Req. 4.2–4.6). The `wizardNavReducer` reducer and
 * the `resetByReselection` helper are pure, so they are exercised directly,
 * without mounting the DOM (the reselection property test lives in task 13.3).
 */

/** Builds a navigation state from partial fields. */
function navState(partial: Partial<WizardNavState>): WizardNavState {
  return { ...initialNavState, ...partial }
}

describe("wizard — range constants (1..6)", () => {
  it("first is 1 and last is 6", () => {
    expect(FIRST_STAGE).toBe(1)
    expect(LAST_STAGE).toBe(6)
  })
})

describe("wizardNavReducer — NEXT (Req. 4.2, sequential gating)", () => {
  it("does not advance when the current stage is not completed (no-op)", () => {
    const state = navState({ currentStage: 1, completed: [] })
    const next = wizardNavReducer(state, { type: "NEXT" })
    // Backend-derived gate: Stage 1 not completed ⇒ does not advance.
    expect(next).toEqual(state)
  })

  it("advances when the current stage is completed; preserves the selection (Req. 4.4)", () => {
    const state = navState({ currentStage: 1, completed: [1] })
    const next = wizardNavReducer(state, { type: "NEXT" })
    expect(next.currentStage).toBe(2)
    // While there was no "Back", the confirmed selection stays unchanged.
    expect(next.completed).toEqual([1])
    expect(next.reselecting).toBe(false)
  })

  it("never advances past the last stage (6)", () => {
    const state = navState({
      currentStage: 6,
      completed: [1, 2, 3, 4, 5, 6],
    })
    const next = wizardNavReducer(state, { type: "NEXT" })
    expect(next).toEqual(state) // no-op: 6 is the last
  })
})

describe("wizardNavReducer — BACK (Req. 4.3)", () => {
  it("returns to the previous stage and enters reselection mode", () => {
    const state = navState({ currentStage: 3, completed: [1, 2, 3] })
    const back = wizardNavReducer(state, { type: "BACK" })
    expect(back.currentStage).toBe(2)
    expect(back.reselecting).toBe(true)
    // "Back" by itself resets nothing — the reset only happens on reconfirm.
    expect(back.completed).toEqual([1, 2, 3])
  })

  it("at the first stage it is a floor: does not recede, but enables reselection", () => {
    const state = navState({ currentStage: 1, completed: [1] })
    const back = wizardNavReducer(state, { type: "BACK" })
    expect(back.currentStage).toBe(1)
    expect(back.reselecting).toBe(true)
  })
})

describe("wizardNavReducer — reselection Back + change + Next (Req. 4.5, 4.6)", () => {
  it("on reconfirm after Back, adopts the new selection and resets the later ones", () => {
    // Researcher had 1..4 completed, was on Stage 4.
    let state: WizardNavState = navState({
      currentStage: 4,
      completed: [1, 2, 3, 4],
    })
    // Goes back to Stage 2 (two "Back"s).
    state = wizardNavReducer(state, { type: "BACK" }) // -> 3
    state = wizardNavReducer(state, { type: "BACK" }) // -> 2
    expect(state.currentStage).toBe(2)
    expect(state.reselecting).toBe(true)

    // Changes the Stage 2 selection and triggers "Next": adopts the new
    // selection and the backend resets the later stages (> 2) to nao_iniciado.
    const reconfirmed = wizardNavReducer(state, { type: "NEXT" })
    expect(reconfirmed.currentStage).toBe(3)
    expect(reconfirmed.completed).toEqual([1, 2]) // 3 and 4 reset
    expect(reconfirmed.reselecting).toBe(false)
  })

  it("after the reset, advancing again respects the sequential gate", () => {
    // After reselecting on Stage 2 (completed=[1,2], current=3), there is no
    // way to advance from Stage 3 because it is not completed yet.
    const state = navState({ currentStage: 3, completed: [1, 2] })
    const noAdvance = wizardNavReducer(state, { type: "NEXT" })
    expect(noAdvance).toEqual(state) // no-op: Stage 3 not completed
  })
})

describe("wizardNavReducer — SYNC_BACKEND (source of truth)", () => {
  it("replaces completed with the backend re-read, normalized", () => {
    const state = navState({ currentStage: 3, completed: [1] })
    const synced = wizardNavReducer(state, {
      type: "SYNC_BACKEND",
      // Out of order, with a duplicate and invalid numbers (7 and 0 discarded).
      completed: [3, 1, 2, 2, 7, 0],
    })
    expect(synced.completed).toEqual([1, 2, 3])
    // Navigation intact.
    expect(synced.currentStage).toBe(3)
    expect(synced.reselecting).toBe(false)
  })
})

describe("resetByReselection — mirrors the backend reset (Req. 4.6)", () => {
  it("removes every stage after the changed one; keeps earlier ones", () => {
    expect(resetByReselection([1, 2, 3, 4, 5, 6], 2)).toEqual([1, 2])
    expect(resetByReselection([1, 2, 3], 3)).toEqual([1, 2, 3]) // nothing later
    expect(resetByReselection([1, 2, 3, 4], 1)).toEqual([1])
  })

  it("is pure: does not mutate the input and normalizes (unique, ordered, 1..6)", () => {
    const input = [4, 2, 1, 2, 9, 0]
    const copy = [...input]
    const output = resetByReselection(input, 3)
    expect(output).toEqual([1, 2]) // 4 (later) removed; 9/0 discarded
    expect(input).toEqual(copy) // no mutation
  })
})
