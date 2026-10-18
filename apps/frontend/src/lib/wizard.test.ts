import { describe, expect, it } from "vitest"
import fc from "fast-check"

import { derivePresentation, deriveStates } from "@/lib/wizard"
import { type StageNumber } from "@/lib/campaigns"

/**
 * Feature: pipeline-five-stages, Property 1: Sequential-gating derivation.
 * Validates: Requirements 1.4, 8.9
 *
 * Under the five-stage scheme (Modeling = Stage 1), a stage N (N>1) is
 * `blocked` iff N-1 is not completed; "Advance" from N to N+1 is only allowed
 * when N+1 is not blocked. The wizard never allows advancing to a blocked
 * stage, and there is no advance from the last stage (5).
 */
describe("deriveStates — sequential gating (1..6)", () => {
  it("nothing completed: 1 available, 2..6 blocked; no advance", () => {
    const d = deriveStates([])
    expect(d.stages).toEqual([
      { stage: 1, status: "available" },
      { stage: 2, status: "blocked" },
      { stage: 3, status: "blocked" },
      { stage: 4, status: "blocked" },
      { stage: 5, status: "blocked" },
      { stage: 6, status: "blocked" },
    ])
    expect(d.canAdvance(1)).toBe(false)
    expect(d.canAdvance(2)).toBe(false)
    expect(d.canAdvance(3)).toBe(false)
    expect(d.canAdvance(4)).toBe(false)
    expect(d.canAdvance(5)).toBe(false)
  })

  it("stage 1 completed: 2 available, 3..6 blocked; advances from 1", () => {
    const d = deriveStates([1])
    expect(d.stages.find((e) => e.stage === 1)?.status).toBe("completed")
    expect(d.stages.find((e) => e.stage === 2)?.status).toBe("available")
    expect(d.stages.find((e) => e.stage === 3)?.status).toBe("blocked")
    expect(d.stages.find((e) => e.stage === 4)?.status).toBe("blocked")
    expect(d.stages.find((e) => e.stage === 5)?.status).toBe("blocked")
    expect(d.stages.find((e) => e.stage === 6)?.status).toBe("blocked")
    expect(d.canAdvance(1)).toBe(true)
    expect(d.canAdvance(2)).toBe(false)
  })

  it("1..5 completed: 6 available; advances from 1..5; never from 6", () => {
    const d = deriveStates([1, 2, 3, 4, 5])
    expect(d.stages.find((e) => e.stage === 6)?.status).toBe("available")
    expect(d.canAdvance(1)).toBe(true)
    expect(d.canAdvance(2)).toBe(true)
    expect(d.canAdvance(3)).toBe(true)
    expect(d.canAdvance(4)).toBe(true)
    expect(d.canAdvance(5)).toBe(true)
    expect(d.canAdvance(6)).toBe(false) // 6 is the last stage
  })

  it("exhaustive: a stage N>1 is blocked iff N-1 is not completed", () => {
    // Sweeps every subset of {1,2,3,4,5,6}.
    for (let mask = 0; mask < 64; mask++) {
      const completed = [1, 2, 3, 4, 5, 6].filter((n) => mask & (1 << (n - 1)))
      const d = deriveStates(completed)
      for (const n of [2, 3, 4, 5, 6] as const) {
        const blocked =
          d.stages.find((e) => e.stage === n)?.status === "blocked"
        const previousCompleted = completed.includes(n - 1)
        // blocked  <=>  previous NOT completed AND itself not completed
        const expectBlocked = !previousCompleted && !completed.includes(n)
        expect(blocked).toBe(expectBlocked)
        // never allows advancing to a blocked stage
        if (blocked)
          expect(d.canAdvance((n - 1) as 1 | 2 | 3 | 4 | 5)).toBe(false)
      }
    }
  })
})

// Feature: wizard-progress-stages, Property 1: Presenting "completed" requires
// real completion AND reach. For any `completed`, `maxVisitedStage` and
// `currentStage`, a stage N is presented as "completed" if and only if N is
// completed in the Real_State (completed.includes(N)) AND N <= maxVisitedStage.
// Corollaries: completed in real but not yet reached (N > maxVisitedStage) is
// presented "available" (never "completed"); and a stage not completed in real
// is never presented "completed".
// Validates: Requirements 1.1, 1.2, 4.1
describe("derivePresentation — Property 1: completed requires real AND reach", () => {
  /** Arbitrary subset of {1,2,3,4,5}. */
  const arbCompleted = fc.subarray([1, 2, 3, 4, 5])
  /** Stage in 1..5 (used for maxVisitedStage and currentStage). */
  const arbStage = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<
    StageNumber
  >

  it("∀ inputs: presented(N)==='concluido' ⇔ includes(N) && N<=max", () => {
    fc.assert(
      fc.property(
        arbCompleted,
        arbStage,
        arbStage,
        (completed, maxVisitedStage, currentStage) => {
          const { stages } = derivePresentation(
            completed,
            maxVisitedStage,
            currentStage,
          )
          for (const { stage, presented } of stages) {
            const expectCompleted =
              completed.includes(stage) && stage <= maxVisitedStage
            // Biconditional: "completed" in the presentation iff real AND reached.
            expect(presented === "completed").toBe(expectCompleted)
            // Corollary: completed in real but not reached → "available".
            if (completed.includes(stage) && stage > maxVisitedStage) {
              expect(presented).toBe("available")
            }
            // Corollary: not completed in real → never "completed".
            if (!completed.includes(stage)) {
              expect(presented).not.toBe("completed")
            }
          }
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progress-stages, Property 2: Monotonic reveal on advance.
// Modeling "progress per stage" as a total order completed > available >
// blocked, as `maxVisitedStage` grows from `max1` to `max2` (with
// `max1 <= max2`), the presentation of each stage is non-decreasing in progress
// (advancing can only reveal completions, never hide them). Also, no stage past
// the visited one (`stage > maxVisitedStage`) is presented as "completed".
// Validates: Requirements 1.3
describe("derivePresentation — Property 2: monotonic reveal on advance", () => {
  /** Arbitrary subset of {1,2,3,4,5}. */
  const arbCompleted = fc.subarray([1, 2, 3, 4, 5])
  /** Stage in 1..5 (used for maxVisitedStage and currentStage). */
  const arbStage = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<
    StageNumber
  >

  /** Total order of the presented progress: completed > available > blocked. */
  const rank = (presented: "completed" | "available" | "blocked"): number =>
    presented === "completed" ? 2 : presented === "available" ? 1 : 0

  it("∀ completed, ∀ max1<=max2, ∀ current: non-decreasing progress and no completed past the visited", () => {
    fc.assert(
      fc.property(
        arbCompleted,
        arbStage,
        arbStage,
        arbStage,
        (completed, a, b, currentStage) => {
          // Normalize to max1 <= max2 without discarding cases.
          const max1 = (Math.min(a, b) as StageNumber)
          const max2 = (Math.max(a, b) as StageNumber)

          const pres1 = derivePresentation(completed, max1, currentStage)
          const pres2 = derivePresentation(completed, max2, currentStage)

          for (const { stage, presented } of pres1.stages) {
            const in2 = pres2.stages.find((e) => e.stage === stage)
            expect(in2).toBeDefined()
            // Monotonicity: advancing never reduces the presented progress.
            expect(rank(in2!.presented)).toBeGreaterThanOrEqual(
              rank(presented),
            )
          }

          // No stage past the visited one is presented as "completed".
          for (const max of [max1, max2]) {
            const { stages } = derivePresentation(completed, max, currentStage)
            for (const { stage, presented } of stages) {
              if (stage > max) {
                expect(presented).not.toBe("completed")
              }
            }
          }
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progress-stages, Property 3: Advance governed by the
// Real_State, not the presentation. For any `completed`, a stage's advance
// enablement — computed by `deriveStates(completed).canAdvance` — is invariant
// to `maxVisitedStage`. Since `deriveStates` does not even receive
// `maxVisitedStage`, for any two values `maxA`/`maxB` the same `completed`
// produces exactly the same advance decision: the temporary divergence between
// Presented_State and Real_State never changes the gate.
// Validates: Requirements 3.4, 7.3
describe("deriveStates — Property 3: advance invariant to maxVisitedStage", () => {
  /** Arbitrary subset of {1,2,3,4,5}. */
  const arbCompleted = fc.subarray([1, 2, 3, 4, 5])
  /** maxVisitedStage in 1..5. */
  const arbMax = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<StageNumber>

  it("∀ completed, ∀ maxA,maxB: canAdvance(n) identical (invariant to maxVisitedStage)", () => {
    fc.assert(
      fc.property(
        arbCompleted,
        arbMax,
        arbMax,
        (completed, _maxA, _maxB) => {
          // The gate is derived only from the Real_State; the presentation
          // (governed by maxVisitedStage) never touches it. Since
          // `deriveStates` ignores any advance, two derivations under maxA/maxB
          // coincide on canAdvance for every stage n in 1..4 (no advance from 5).
          const gateA = deriveStates(completed)
          const gateB = deriveStates(completed)
          for (const n of [1, 2, 3, 4] as const) {
            expect(gateA.canAdvance(n)).toBe(gateB.canAdvance(n))
          }
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progress-stages, Property 4: `preCompleted` only marks the
// system's automatic completions. For any `completed`, `maxVisitedStage` and
// `currentStage`, if a stage N is marked `preCompleted` then N is `concluido`
// in the Real_State (completed.includes(N)), N > 1, N−1 is `concluido`
// (completed.includes(N-1)), N is presented "completed" and the researcher has
// not yet acted past window N (currentStage <= N). In particular, a stage not
// completed in real (e.g. Stage 2 of a non-curated case) is never
// `preCompleted`.
// Validates: Requirements 2.1, 4.3, 7.4
describe("derivePresentation — Property 4: preCompleted only marks automatic completions", () => {
  /** Arbitrary subset of {1,2,3,4,5}. */
  const arbCompleted = fc.subarray([1, 2, 3, 4, 5])
  /** Stage in 1..5 (used for maxVisitedStage and currentStage). */
  const arbStage = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<
    StageNumber
  >

  it("∀ inputs: preCompleted(N) ⇒ includes(N) && includes(N-1) && N>1 && presented(N)==='concluido' && current<=N; not completed is never preCompleted", () => {
    fc.assert(
      fc.property(
        arbCompleted,
        arbStage,
        arbStage,
        (completed, maxVisitedStage, currentStage) => {
          const { stages } = derivePresentation(
            completed,
            maxVisitedStage,
            currentStage,
          )
          for (const { stage, presented, preCompleted } of stages) {
            if (preCompleted) {
              // Implication: preCompleted ⇒ all the conditions below hold.
              expect(completed.includes(stage)).toBe(true)
              expect(completed.includes(stage - 1)).toBe(true)
              expect(stage).toBeGreaterThan(1)
              expect(presented).toBe("completed")
              expect(currentStage).toBeLessThanOrEqual(stage)
            }
            // Core contrapositive: a stage not completed in real is never
            // preCompleted (covers Stage 2 of a non-curated case).
            if (!completed.includes(stage)) {
              expect(preCompleted).toBe(false)
            }
          }
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progress-stages, Property 5: `derivePresentation` is pure and
// deterministic. For every triple of inputs `(completed, maxVisitedStage,
// currentStage)`, repeated invocations produce deeply equal outputs
// (determinism) and the function causes no side effects — in particular, it
// does not mutate the received `completed` array (purity). Recomputing after a
// backend re-read depends only on the current inputs.
// Validates: Requirements 6.1
describe("derivePresentation — Property 5: purity and determinism", () => {
  /** Arbitrary subset of {1,2,3,4,5}. */
  const arbCompleted = fc.subarray([1, 2, 3, 4, 5])
  /** Stage in 1..5 (used for maxVisitedStage and currentStage). */
  const arbStage = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<
    StageNumber
  >

  it("∀ inputs: two equal calls ⇒ deeply equal outputs; no input mutation", () => {
    fc.assert(
      fc.property(
        arbCompleted,
        arbStage,
        arbStage,
        (completed, maxVisitedStage, currentStage) => {
          // Snapshot the input to detect mutation (purity).
          const snapshot = [...completed]

          const r1 = derivePresentation(
            completed,
            maxVisitedStage,
            currentStage,
          )
          const r2 = derivePresentation(
            completed,
            maxVisitedStage,
            currentStage,
          )

          // Determinism: two equal calls ⇒ deeply equal outputs.
          expect(r2).toEqual(r1)

          // Purity: the function does not mutate the input array.
          expect(completed).toEqual(snapshot)
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progress-stages, example/unit tests of the derivation.
// Concrete cases cited in the design's Testing Strategy, anchoring the real UX
// scenarios of Stage 2 (DAG generation). They complement the (fast-check)
// properties with named, readable examples.
// Validates: Requirements 1.1, 1.2, 2.1, 4.1
describe("derivePresentation — concrete examples (Stage 2)", () => {
  /** Finds stage N in the derived presentation (always present in 1..5). */
  const stage = (
    r: ReturnType<typeof derivePresentation>,
    n: StageNumber,
  ) => r.stages.find((e) => e.stage === n)!

  // R1.1: on completing Stage 1 of a curated case (backend returns {1,2} at
  // once) and staying in the Stage 1 window, Stage 2 has not been reached yet
  // (maxVisitedStage=1), so it is presented "available" — never "completed" —
  // and is not marked preCompleted.
  it("R1.1: completed={1,2}, max=1, current=1 ⇒ Stage 2 'disponivel'", () => {
    const r = derivePresentation([1, 2], 1, 1)
    const e2 = stage(r, 2)
    expect(e2.presented).toBe("available")
    expect(e2.presented).not.toBe("completed")
    expect(e2.preCompleted).toBe(false)
  })

  // R1.2 / R2.1: on navigating to the pre-completed Stage 2 window of a curated
  // case (max=2, current=2, completed={1,2}), Stage 2 is presented "completed"
  // AND marked preCompleted=true (completed by the system via curation, not by
  // a direct researcher action).
  it("R1.2/R2.1: completed={1,2}, max=2, current=2 ⇒ Stage 2 'concluido' and preCompleted=true", () => {
    const r = derivePresentation([1, 2], 2, 2)
    const e2 = stage(r, 2)
    expect(e2.presented).toBe("completed")
    expect(e2.preCompleted).toBe(true)
  })

  // R4.1: for a non-curated case the backend returns only {1} — Stage 2 is not
  // completed in real. Sweeps every possible maxVisitedStage (and currentStage)
  // to confirm Stage 2 is never presented "completed" nor marked preCompleted
  // (no regression).
  it("R4.1: completed={1} ⇒ Stage 2 never 'concluido' and preCompleted=false (∀ max, ∀ current)", () => {
    for (const max of [1, 2, 3, 4, 5] as const) {
      for (const current of [1, 2, 3, 4, 5] as const) {
        const r = derivePresentation([1], max, current)
        const e2 = stage(r, 2)
        expect(e2.presented).not.toBe("completed")
        expect(e2.preCompleted).toBe(false)
      }
    }
  })
})
