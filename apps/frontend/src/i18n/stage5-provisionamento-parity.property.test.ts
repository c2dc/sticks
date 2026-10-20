import { describe, it, expect } from "vitest"
import fc from "fast-check"

import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"

/**
 * Focused property-based test (fast-check, >= 100 runs) for pt-BR/en key parity
 * of the NEW i18n keys introduced by the `stage5-target-provisioning` spec
 * (task 11), scoped to the `stage5.provisionamento.*` subtree.
 *
 * The full-namespace parity test (`pipeline-i18n-parity.property.test.ts`)
 * already enumerates every leaf path and transitively covers these keys. This
 * test narrows the lens to exactly the subtree this spec touched so that the
 * spec's requirement 6.3 (i18n parity for the new provisioning sub-stages and
 * prerequisites) is directly traceable.
 *
 * It does NOT add or modify any i18n keys; it only reads the locale JSON.
 *
 * Feature: stage5-target-provisioning, Property: paridade de chaves i18n pt-BR/en
 * Validates: Requirements 6.3
 */

type JsonValue = string | number | boolean | null | JsonObject | JsonValue[]
interface JsonObject {
  [key: string]: JsonValue
}

/** Dot-joined leaf paths of a nested JSON object (order-independent key view). */
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

const SUBTREE = "stage5.provisionamento"

const ptSubtreePaths = collectLeafPaths(ptPipeline as JsonValue)
  .filter((p) => p === SUBTREE || p.startsWith(`${SUBTREE}.`))
  .sort()
const enSubtreePaths = collectLeafPaths(enPipeline as JsonValue)
  .filter((p) => p === SUBTREE || p.startsWith(`${SUBTREE}.`))
  .sort()
const unionSubtreePaths = Array.from(
  new Set([...ptSubtreePaths, ...enSubtreePaths]),
)

// The specific NEW keys this spec added (task 11): the online/offline sub-stage
// console steps, the sub-stage group, and the two new prerequisite items.
const NEW_KEYS = [
  "stage5.provisionamento.console.etapas.build_online",
  "stage5.provisionamento.console.etapas.encerrar_egresso",
  "stage5.provisionamento.console.etapas.reisolar",
  "stage5.provisionamento.console.etapas.verificar_egresso",
  "stage5.provisionamento.console.etapas.setup_offline",
  "stage5.provisionamento.console.subetapas.s51",
  "stage5.provisionamento.console.subetapas.s52",
  "stage5.provisionamento.prerequisitos.prerequisitosOnline",
  "stage5.provisionamento.prerequisitos.egressoPerdido",
] as const

describe("stage5 provisionamento i18n parity (property, stage5-target-provisioning)", () => {
  // Property: for every leaf path under `stage5.provisionamento` present in
  // EITHER locale, it is present in BOTH pt-BR and en with a non-empty string
  // value. Driven over the whole subtree key set.
  it("Property: paridade de chaves i18n pt-BR/en no subárvore stage5.provisionamento", () => {
    // Sanity guard: the subtree is non-trivial and both locales declare it.
    expect(unionSubtreePaths.length).toBeGreaterThan(10)
    expect(ptSubtreePaths.length).toBeGreaterThan(0)
    expect(enSubtreePaths.length).toBeGreaterThan(0)

    fc.assert(
      fc.property(fc.constantFrom(...unionSubtreePaths), (path) => {
        const ptValue = getByPath(ptPipeline as JsonValue, path)
        const enValue = getByPath(enPipeline as JsonValue, path)

        // Key parity: the path exists in both locales.
        expect(ptValue, `${path} ausente em pt-BR`).toBeDefined()
        expect(enValue, `${path} ausente em en`).toBeDefined()

        // Both leaves are non-empty strings.
        expect(typeof ptValue, `${path} não é string em pt-BR`).toBe("string")
        expect(typeof enValue, `${path} não é string em en`).toBe("string")
        expect((ptValue as string).length).toBeGreaterThan(0)
        expect((enValue as string).length).toBeGreaterThan(0)
      }),
      { numRuns: 200 },
    )
  })

  // Property: the two key SETS under the subtree are exactly equal.
  it("Property: os conjuntos de chaves pt-BR/en do subárvore são idênticos", () => {
    expect(ptSubtreePaths).toEqual(enSubtreePaths)
  })

  // Example-backed assertion: the specific NEW keys this spec introduced exist
  // in BOTH locales with non-empty string values (requirement 6.3 traceability).
  it("as novas chaves do stage5-target-provisioning existem em pt-BR e en", () => {
    fc.assert(
      fc.property(fc.constantFrom(...NEW_KEYS), (path) => {
        const ptValue = getByPath(ptPipeline as JsonValue, path)
        const enValue = getByPath(enPipeline as JsonValue, path)

        expect(ptValue, `${path} ausente em pt-BR`).toBeDefined()
        expect(enValue, `${path} ausente em en`).toBeDefined()
        expect(typeof ptValue).toBe("string")
        expect(typeof enValue).toBe("string")
        expect((ptValue as string).length).toBeGreaterThan(0)
        expect((enValue as string).length).toBeGreaterThan(0)
      }),
      { numRuns: 100 },
    )

    // Every expected new key must be covered by the subtree enumeration too.
    for (const key of NEW_KEYS) {
      expect(ptSubtreePaths, `${key} não enumerada em pt-BR`).toContain(key)
      expect(enSubtreePaths, `${key} não enumerada em en`).toContain(key)
    }
  })
})
