import { describe, it, expect } from "vitest"
import fc from "fast-check"

import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"

import type { AgentRunStatus, EvaluatorDecision } from "@/lib/ai"

/**
 * Property-based test (fast-check, >= 100 runs) for pt-BR/en key parity across
 * the `aiRun.*` i18n keys of the `pipeline` namespace (ai-curation-agents, task
 * 17.3).
 *
 * Task 17.2 introduced every user-facing string of the AI-run view (`AiRunView`)
 * under the `aiRun.*` keys, pt-BR being the source of truth (Req. 13.5) and en
 * holding full parity (Req. 13.6). This test enumerates EVERY leaf path under
 * `aiRun` in both locales and asserts, over many generated paths, that the two
 * locales declare exactly the same key tree with no empty value. It also asserts
 * that every dynamic enum key the view resolves at runtime — the five
 * `AgentRunStatus` members under `aiRun.status.*` and the two `EvaluatorDecision`
 * members under `aiRun.decisao.*` — exists in BOTH locales, since those keys are
 * built from the backend enum values rather than written literally in the view.
 *
 * This test does NOT add or modify any i18n keys; it only reads the locale JSON.
 *
 * Feature: ai-curation-agents, Property: paridade de chaves pt-BR/en (aiRun)
 * Validates: Requirements 13.5, 13.6
 */

type JsonValue = string | number | boolean | null | JsonObject | JsonValue[]
interface JsonObject {
  [key: string]: JsonValue
}

/** Collect every leaf path (dot-joined) of a nested JSON object. */
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
    if (current === null || typeof current !== "object" || Array.isArray(current)) {
      return undefined
    }
    current = (current as JsonObject)[segment]
  }
  return current
}

/** Leaf paths scoped to the `aiRun` subtree of each locale's pipeline namespace. */
const ptAiRunPaths = collectLeafPaths(
  getByPath(ptPipeline as JsonValue, "aiRun") as JsonValue,
).sort()
const enAiRunPaths = collectLeafPaths(
  getByPath(enPipeline as JsonValue, "aiRun") as JsonValue,
).sort()
const allAiRunPaths = Array.from(new Set([...ptAiRunPaths, ...enAiRunPaths]))

/**
 * The dynamic enum keys the view builds at runtime from backend enum values
 * (`t("aiRun.status.${status}")` and `t("aiRun.decisao.${decision}")`). These
 * must exist in both locales so no verdict/status ever falls back to the raw
 * enum token (Req. 13.5, 13.6).
 */
const STATUS_KEYS: AgentRunStatus[] = [
  "pendente",
  "em_execucao",
  "concluido",
  "falhou",
  "abortado",
]
const DECISION_KEYS: EvaluatorDecision[] = ["refinar", "finalizar"]

describe("aiRun i18n parity (property, ai-curation-agents)", () => {
  // Property: for any leaf key present in EITHER locale under `aiRun`, it is
  // present in BOTH, both are non-empty strings, and the two locales declare the
  // exact same key tree (set equality).
  it("Property: paridade de chaves pt-BR/en em aiRun.* (Req. 13.5, 13.6)", () => {
    // Sanity guard: there is a non-trivial aiRun key tree to exercise.
    expect(allAiRunPaths.length).toBeGreaterThan(20)
    // Set equality: neither locale has a missing or extra key.
    expect(ptAiRunPaths).toEqual(enAiRunPaths)

    fc.assert(
      fc.property(fc.constantFrom(...allAiRunPaths), (path) => {
        const full = `aiRun.${path}`
        const ptValue = getByPath(ptPipeline as JsonValue, full)
        const enValue = getByPath(enPipeline as JsonValue, full)

        // Key parity: the path exists in both locales.
        expect(ptValue, `${full} ausente em pt-BR`).toBeDefined()
        expect(enValue, `${full} ausente em en`).toBeDefined()

        // The aiRun namespace is string-valued.
        expect(typeof ptValue).toBe("string")
        expect(typeof enValue).toBe("string")

        // No empty translations.
        expect((ptValue as string).length).toBeGreaterThan(0)
        expect((enValue as string).length).toBeGreaterThan(0)
      }),
      { numRuns: 200 },
    )
  })

  // Property: every dynamic status/decisao enum key resolves in BOTH locales.
  it("Property: todas as chaves de enum status/decisao existem em ambos os locales (Req. 13.5, 13.6)", () => {
    const enumKeys = [
      ...STATUS_KEYS.map((s) => `aiRun.status.${s}`),
      ...DECISION_KEYS.map((d) => `aiRun.decisao.${d}`),
    ]

    fc.assert(
      fc.property(fc.constantFrom(...enumKeys), (key) => {
        const ptValue = getByPath(ptPipeline as JsonValue, key)
        const enValue = getByPath(enPipeline as JsonValue, key)

        expect(typeof ptValue, `${key} ausente em pt-BR`).toBe("string")
        expect(typeof enValue, `${key} ausente em en`).toBe("string")
        expect((ptValue as string).length).toBeGreaterThan(0)
        expect((enValue as string).length).toBeGreaterThan(0)
      }),
      { numRuns: 100 },
    )
  })
})
