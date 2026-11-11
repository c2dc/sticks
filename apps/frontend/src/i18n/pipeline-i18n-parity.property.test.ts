import { describe, it, expect } from "vitest"
import fc from "fast-check"

import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"

/**
 * Property-based test (fast-check, >= 100 runs) for pt-BR/en key parity across
 * the FULL `pipeline` i18n namespace (pipeline-six-stages, task 14.7).
 *
 * Task 14.5 unified the six-stage i18n keys (stages.1..6) and added the new key
 * groups `environmentParameters.*`, `report.*` and `metrics.*`. This test is
 * the property-level complement to the example-based `pipeline-parity.test.ts`
 * and `stages.terminology.test.ts`: instead of checking a handful of hand-picked
 * keys, it enumerates EVERY leaf path in both locales and asserts, over many
 * generated paths, that the two locales declare exactly the same key tree and
 * that no value is empty. It also asserts the "Estágio"/"Stage" terminology for
 * every stage 1..6.
 *
 * This test does NOT add or modify any i18n keys; it only reads the locale JSON.
 *
 * Feature: pipeline-six-stages, Property: pt-BR/en key parity
 * Validates: Requirements 1.1, 4.1
 */

type JsonValue = string | number | boolean | null | JsonObject | JsonValue[]
interface JsonObject {
  [key: string]: JsonValue
}

/**
 * Collect every leaf path (dot-joined) of a nested JSON object. A leaf is any
 * non-object value (strings, numbers, booleans). This gives a stable, order-
 * independent view of the key tree that we can compare between locales.
 */
function collectLeafPaths(value: JsonValue, prefix = ""): string[] {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return [prefix]
  }
  const paths: string[] = []
  for (const key of Object.keys(value)) {
    const next = prefix ? `${prefix}.${key}` : key
    paths.push(...collectLeafPaths((value as JsonObject)[key], next))
  }
  return paths
}

/** Resolve a dot-joined path to its leaf value, or `undefined` if absent. */
function getByPath(obj: JsonValue, path: string): JsonValue | undefined {
  let current: JsonValue | undefined = obj
  for (const segment of path.split(".")) {
    if (
      current === null ||
      typeof current !== "object" ||
      Array.isArray(current)
    ) {
      return undefined
    }
    current = (current as JsonObject)[segment]
  }
  return current
}

const ptPaths = collectLeafPaths(ptPipeline as JsonValue).sort()
const enPaths = collectLeafPaths(enPipeline as JsonValue).sort()
const allPaths = Array.from(new Set([...ptPaths, ...enPaths]))

const STAGES = [1, 2, 3, 4, 5, 6] as const

describe("pipeline i18n parity (property, pipeline-six-stages)", () => {
  // Property: for any leaf key present in EITHER locale, it is present in BOTH,
  // and neither value is empty. Driven over the whole namespace key set.
  it("Property: pt-BR/en key parity across the whole pipeline namespace", () => {
    // Sanity guard: there is a non-trivial key tree to exercise.
    expect(allPaths.length).toBeGreaterThan(50)

    fc.assert(
      fc.property(fc.constantFrom(...allPaths), (path) => {
        const ptValue = getByPath(ptPipeline as JsonValue, path)
        const enValue = getByPath(enPipeline as JsonValue, path)

        // Key parity: the path exists in both locales.
        expect(ptValue, `${path} missing in pt-BR`).toBeDefined()
        expect(enValue, `${path} missing in en`).toBeDefined()

        // Both leaves are strings (the pipeline namespace is string-valued).
        expect(typeof ptValue).toBe("string")
        expect(typeof enValue).toBe("string")

        // No empty translations.
        expect((ptValue as string).length).toBeGreaterThan(0)
        expect((enValue as string).length).toBeGreaterThan(0)
      }),
      { numRuns: 200 },
    )
  })

  // Property: the six stages (and the new key groups) have identical leaf-path
  // sets across locales. This is the set-equality counterpart of the per-path
  // check above and fails loudly if either locale gains/loses a key.
  it("Property: key sets for stages/environmentParameters/report/metrics are identical", () => {
    const groups = [
      "stages",
      "environmentParameters",
      "report",
      "metrics",
    ] as const

    fc.assert(
      fc.property(fc.constantFrom(...groups), (group) => {
        const ptGroup = ptPaths.filter((p) => p.startsWith(`${group}.`))
        const enGroup = enPaths.filter((p) => p.startsWith(`${group}.`))
        expect(ptGroup.length).toBeGreaterThan(0)
        expect(ptGroup).toEqual(enGroup)
      }),
      { numRuns: 100 },
    )
  })

  // Property: for every stage 1..6, pt-BR uses "Estágio" and en uses "Stage" in
  // both the `name` and `short` keys, and the keys exist in both locales.
  it("Property: 'Estágio'/'Stage' terminology for the six stages", () => {
    fc.assert(
      fc.property(fc.constantFrom(...STAGES), (n) => {
        const ptName = getByPath(ptPipeline as JsonValue, `stages.${n}.name`)
        const ptShort = getByPath(ptPipeline as JsonValue, `stages.${n}.short`)
        const enName = getByPath(enPipeline as JsonValue, `stages.${n}.name`)
        const enShort = getByPath(enPipeline as JsonValue, `stages.${n}.short`)

        expect(typeof ptName).toBe("string")
        expect(typeof ptShort).toBe("string")
        expect(typeof enName).toBe("string")
        expect(typeof enShort).toBe("string")

        // pt-BR terminology: "Estágio".
        expect(ptName as string).toContain("Estágio")
        expect(ptShort as string).toContain("Estágio")

        // en terminology: "Stage".
        expect(enName as string).toContain("Stage")
        expect(enShort as string).toContain("Stage")
      }),
      { numRuns: 100 },
    )
  })
})

/**
 * Semantic, number-free agentic phase keys (agentic-phase-model-refactor, task
 * 6.2). Task 6.1 renamed the phase i18n entries to the English semantic phase
 * names and added the container-emulability strings; this block pins I18n_Parity
 * for those specific keys rather than relying only on the whole-namespace
 * parity check above. It asserts each semantic phase key resolves in BOTH
 * locales as a non-empty string, across every i18n subtree the refactor touches:
 *   - `guided.speech.<phase>`     — Guided_Run_View robot lines (Req. 7.2)
 *   - `aiRun.stageName.<phase>`   — AiRunView agent-step names (Req. 7.2)
 *   - `aiRun.comparison.stage.<phase>` — curation-comparison phase labels
 *   - `campaign.containerEmulability.*` — container-emulability indicators (Req. 6.6)
 *
 * This test does NOT add or modify any i18n keys; it only reads the locale JSON.
 *
 * Feature: agentic-phase-model-refactor, Property: I18n_Parity for semantic phase keys
 * Validates: Requirements 7.2, 6.6
 */
describe("semantic phase i18n parity (agentic-phase-model-refactor)", () => {
  // The five semantic agentic phases, number-free (Req. 1.1). `input` is a
  // non-phase and intentionally excluded from these phase-keyed subtrees.
  const AGENTIC_PHASES = [
    "dag",
    "operationalization",
    "environment",
    "execution",
    "evaluation",
  ] as const

  // The full `guided.speech.<phase>` and `aiRun.stageName.<phase>` keys plus the
  // two `aiRun.comparison.stage.<phase>` labels the UI resolves dynamically from
  // the backend `AgentStage` value.
  const PHASE_KEYS = [
    ...AGENTIC_PHASES.map((p) => `guided.speech.${p}`),
    ...AGENTIC_PHASES.map((p) => `aiRun.stageName.${p}`),
    "aiRun.comparison.stage.dag",
    "aiRun.comparison.stage.operationalization",
  ]

  // The container-emulability indicator keys (Req. 6.6).
  const EMULABILITY_KEYS = [
    "campaign.containerEmulability.emulable",
    "campaign.containerEmulability.notEmulable",
    "campaign.containerEmulability.notEmulableWithTechniques",
  ]

  const ALL_SEMANTIC_KEYS = [...PHASE_KEYS, ...EMULABILITY_KEYS]

  // Property: every semantic phase / emulability key exists in BOTH locales as a
  // non-empty string. Driven over the explicit key set so a missing or renamed
  // phase key fails with its exact path.
  it("Property: semantic phase keys present with en and pt-BR values", () => {
    fc.assert(
      fc.property(fc.constantFrom(...ALL_SEMANTIC_KEYS), (key) => {
        const ptValue = getByPath(ptPipeline as JsonValue, key)
        const enValue = getByPath(enPipeline as JsonValue, key)

        // Key parity: present in both locales.
        expect(ptValue, `${key} missing in pt-BR`).toBeDefined()
        expect(enValue, `${key} missing in en`).toBeDefined()

        // String-valued, non-empty in both locales.
        expect(typeof ptValue, `${key} not a string in pt-BR`).toBe("string")
        expect(typeof enValue, `${key} not a string in en`).toBe("string")
        expect((ptValue as string).length).toBeGreaterThan(0)
        expect((enValue as string).length).toBeGreaterThan(0)
      }),
      { numRuns: 100 },
    )
  })

  // Property: no legacy numbered phase token survives in any semantic phase
  // value. Guards Req. 1.5 / 7.5 (absence of legacy numbering) for exactly the
  // keys this refactor renamed.
  it("Property: semantic phase values carry no legacy stage numbering", () => {
    const LEGACY_TOKENS = [
      "stage_3",
      "stage_4",
      "stage_5",
      "Stage 3",
      "Stage 4",
      "Stage 5",
      "Estágio 3",
      "Estágio 4",
      "Estágio 5",
    ]

    fc.assert(
      fc.property(fc.constantFrom(...PHASE_KEYS), (key) => {
        const ptValue = getByPath(ptPipeline as JsonValue, key) as string
        const enValue = getByPath(enPipeline as JsonValue, key) as string

        for (const token of LEGACY_TOKENS) {
          expect(ptValue, `${key} (pt-BR) leaks legacy token ${token}`).not.toContain(token)
          expect(enValue, `${key} (en) leaks legacy token ${token}`).not.toContain(token)
        }
      }),
      { numRuns: 100 },
    )
  })

  // Property: the bilingual phase lines are genuinely translated, not copied en
  // into pt-BR (a common parity-but-not-translated regression). Checked on the
  // user-facing `guided.speech.*` lines which must differ between locales.
  it("Property: guided.speech.<phase> values are genuinely translated", () => {
    fc.assert(
      fc.property(fc.constantFrom(...AGENTIC_PHASES), (phase) => {
        const key = `guided.speech.${phase}`
        const ptValue = getByPath(ptPipeline as JsonValue, key) as string
        const enValue = getByPath(enPipeline as JsonValue, key) as string
        expect(ptValue, `${key} not translated`).not.toBe(enValue)
      }),
      { numRuns: 100 },
    )
  })
})
