import { describe, it, expect } from "vitest"
import fc from "fast-check"

import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"

/**
 * Property-based test (fast-check, >= 100 runs) for pt-BR/en key parity across
 * the FULL `pipeline` i18n namespace (pipeline-seis-estagios, task 14.7).
 *
 * Task 14.5 unified the six-stage i18n keys (stages.1..6) and added the new key
 * groups `parametrosAmbiente.*`, `relatorio.*` and `metricas.*`. This test is
 * the property-level complement to the example-based `pipeline-parity.test.ts`
 * and `stages.terminology.test.ts`: instead of checking a handful of hand-picked
 * keys, it enumerates EVERY leaf path in both locales and asserts, over many
 * generated paths, that the two locales declare exactly the same key tree and
 * that no value is empty. It also asserts the "Estágio"/"Stage" terminology for
 * every stage 1..6.
 *
 * This test does NOT add or modify any i18n keys; it only reads the locale JSON.
 *
 * Feature: pipeline-seis-estagios, Property: paridade de chaves pt-BR/en
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

describe("pipeline i18n parity (property, pipeline-seis-estagios)", () => {
  // Property: for any leaf key present in EITHER locale, it is present in BOTH,
  // and neither value is empty. Driven over the whole namespace key set.
  it("Property: paridade de chaves pt-BR/en em todo o namespace pipeline", () => {
    // Sanity guard: there is a non-trivial key tree to exercise.
    expect(allPaths.length).toBeGreaterThan(50)

    fc.assert(
      fc.property(fc.constantFrom(...allPaths), (path) => {
        const ptValue = getByPath(ptPipeline as JsonValue, path)
        const enValue = getByPath(enPipeline as JsonValue, path)

        // Key parity: the path exists in both locales.
        expect(ptValue, `${path} ausente em pt-BR`).toBeDefined()
        expect(enValue, `${path} ausente em en`).toBeDefined()

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
  it("Property: conjuntos de chaves de stages/parametrosAmbiente/relatorio/metricas são idênticos", () => {
    const groups = [
      "stages",
      "parametrosAmbiente",
      "relatorio",
      "metricas",
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
  it("Property: terminologia 'Estágio'/'Stage' para os seis Estágios", () => {
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
