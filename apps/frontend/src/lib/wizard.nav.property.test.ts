import { describe, expect, it } from "vitest"
import fc from "fast-check"

import {
  PIPELINE_STAGES,
  type StageNumber,
} from "@/lib/campaigns"
import {
  FIRST_STAGE,
  deriveStates,
  resetByReselection,
  wizardNavReducer,
  type WizardNavState,
} from "@/lib/wizard"

/**
 * Property test for the reselection-on-Back of the six-stage wizard
 * (pipeline-six-stages, Req. 4.4, 4.5, 4.6). The `wizardNavReducer` reducer and
 * the `resetByReselection` helper are pure and deterministic, so they are
 * exercised directly over many generated inputs (fast-check, >= 100 iterations),
 * without mounting the DOM. The modules under test are NOT modified by this test.
 *
 * Property 12 (design.md): "For any campaign with completed stages, if the
 * selection of an earlier stage is changed after 'Back', then the `StageRun` of
 * all later stages are reset to `nao_iniciado`, restoring the sequential gate
 * from the changed stage."
 */

const STAGES: readonly StageNumber[] = PIPELINE_STAGES

/** Arbitrary of a single valid stage (1..6). */
const stageArb: fc.Arbitrary<StageNumber> = fc.constantFrom(...STAGES)

/**
 * Arbitrary of a "sequentially valid" set of completed stages: a prefix 1..k
 * (0 <= k <= 6). The sequential gate (Req. 3) only admits states where, if N > 1
 * is completed, N-1 also is — so we generate exactly those prefixes, which are
 * the states actually reachable by the pipeline.
 */
const completedPrefixArb: fc.Arbitrary<StageNumber[]> = fc
  .integer({ min: 0, max: STAGES.length })
  .map((k) => STAGES.slice(0, k) as StageNumber[])

describe("wizard — Property 12: Reselection on Back recomputes the following stages", () => {
  // Feature: pipeline-six-stages, Property 12: Reselection on Back recomputes
  // the following stages.
  // Validates: Requirements 4.4, 4.5, 4.6
  it("Back + change + Next adopts the new selection and resets the later ones to nao_iniciado", () => {
    fc.assert(
      fc.property(completedPrefixArb, stageArb, (completed, target) => {
        // Starts with the pipeline advanced up to `completed` and the screen
        // open at the last completed stage (or the first, if nothing completed).
        const initialCurrent = (completed.length > 0
          ? completed[completed.length - 1]
          : FIRST_STAGE) as StageNumber

        let state: WizardNavState = {
          currentStage: initialCurrent,
          completed: [...completed],
          reselecting: false,
        }

        // Goes back to an earlier stage `target` (<= current). Each "Back" recedes
        // one screen and turns on reselection mode; never below the first stage.
        // Ensures at least one "Back" (the scenario is "after 'Back'"): even at
        // the floor (Stage 1) "Back" enables reselection without receding.
        const floor = Math.min(target, initialCurrent) as StageNumber
        state = wizardNavReducer(state, { type: "BACK" })
        while (state.currentStage > floor) {
          state = wizardNavReducer(state, { type: "BACK" })
        }
        // "Back" by itself resets no completions — it only enables reselection.
        expect(state.reselecting).toBe(true)
        expect(state.completed).toEqual(completed)

        const changedStage = state.currentStage

        // Changes the current stage selection and triggers "Next". The advance
        // only happens when the changed stage is completed (Backend gate).
        const reconfirmed = wizardNavReducer(state, { type: "NEXT" })

        const couldAdvance = deriveStates(state.completed).canAdvance(
          changedStage,
        )

        if (!couldAdvance) {
          // Without the changed stage completed, "Next" is a no-op: nothing changes.
          expect(reconfirmed).toEqual(state)
          return
        }

        // Req. 4.5/4.6: adopts the new selection and mirrors the Backend reset
        // over the later stages (> changedStage), which go back to
        // `nao_iniciado` (leave `completed`). The changed stage and the earlier
        // ones (<= changedStage) are preserved.
        const expected = completed.filter((n) => n <= changedStage)
        expect(reconfirmed.completed).toEqual(expected)
        // All later ones were indeed removed (reset).
        for (const n of reconfirmed.completed) {
          expect(n).toBeLessThanOrEqual(changedStage)
        }
        // The screen advances and reselection mode is ended.
        expect(reconfirmed.currentStage).toBe(changedStage + 1)
        expect(reconfirmed.reselecting).toBe(false)
      }),
      { numRuns: 100 },
    )
  })

  // Feature: pipeline-six-stages, Property 12: Reselection on Back recomputes
  // the following stages.
  // Validates: Requirement 4.4
  it("while there is no reselection, 'Next' preserves the confirmed selection (only advances the screen)", () => {
    fc.assert(
      fc.property(completedPrefixArb, (completed) => {
        // Advances on the current stage without ever triggering "Back" (reselecting=false).
        const current = (completed.length > 0
          ? completed[completed.length - 1]
          : FIRST_STAGE) as StageNumber

        const state: WizardNavState = {
          currentStage: current,
          completed: [...completed],
          reselecting: false,
        }

        const next = wizardNavReducer(state, { type: "NEXT" })

        // Req. 4.4: the confirmed selection stays unchanged in any case — both in
        // the no-op (gate blocks) and in the screen advance.
        expect(next.completed).toEqual(completed)
        expect(next.reselecting).toBe(false)

        if (deriveStates(completed).canAdvance(current)) {
          expect(next.currentStage).toBe(current + 1)
        } else {
          expect(next.currentStage).toBe(current)
        }
      }),
      { numRuns: 100 },
    )
  })

  // Feature: pipeline-six-stages, Property 12: Reselection on Back recomputes
  // the following stages.
  // Validates: Requirement 4.6
  it("resetByReselection keeps the stages <= K and discards all > K", () => {
    fc.assert(
      fc.property(
        // arbitrary `completed` (includes noise outside 1..6 and duplicates) to
        // exercise the helper's defensive normalization.
        fc.array(fc.integer({ min: -3, max: 9 }), { maxLength: 20 }),
        stageArb,
        (completed, k) => {
          const result = resetByReselection(completed, k)

          // Every surviving stage is valid (1..6) and <= K.
          for (const n of result) {
            expect(STAGES).toContain(n)
            expect(n).toBeLessThanOrEqual(k)
          }

          // No valid stage <= K from the input was lost.
          const expectedList = [...new Set(completed)]
            .filter((n) => STAGES.includes(n as StageNumber) && n <= k)
            .sort((a, b) => a - b)
          expect(result).toEqual(expectedList)

          // It is unique and ordered (normalization invariant).
          expect(result).toEqual([...result].sort((a, b) => a - b))
          expect(new Set(result).size).toBe(result.length)
        },
      ),
      { numRuns: 100 },
    )
  })
})
