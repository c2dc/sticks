import { describe, it, expect } from "vitest"
import fc from "fast-check"

import ptCommon from "@/i18n/locales/pt-BR/common.json"
import enCommon from "@/i18n/locales/en/common.json"
import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"
import ptPreferences from "@/i18n/locales/pt-BR/preferences.json"
import enPreferences from "@/i18n/locales/en/preferences.json"
import ptStix from "@/i18n/locales/pt-BR/stix.json"
import enStix from "@/i18n/locales/en/stix.json"

import {
  type JsonObject,
  type JsonValue,
  findLegacyStageWording,
  findUnusedKeys,
  flattenKeys,
} from "@/i18n/__tests__/catalog-utils"

/**
 * Property-based tests for the Locale_Catalog guard helpers
 * (agentic-only-flow-redesign, task 42.4).
 *
 * Property 15: Unused-key detection is exact and locale-symmetric
 * Property 16: No legacy stage wording in locale values
 *
 * **Validates: Requirements 9.1, 9.3, 9.4, 9.6, 9.7**
 */

// ---------------------------------------------------------------------------
// Property 15
// ---------------------------------------------------------------------------

const GROUPS = ["alpha", "beta", "gamma", "delta"] as const

// Plain leaves start with "k", plural bases with "p", so a plain key can never
// equal the base of a plural variant and the oracle stays unambiguous.
const plainLeaf = fc.stringMatching(/^k[a-z]{1,5}$/)
const pluralLeaf = fc.stringMatching(/^p[a-z]{1,5}$/)

interface Plan {
  group: (typeof GROUPS)[number]
  dynamic: boolean
  plain: Array<{ leaf: string; referenced: boolean }>
  plural: Array<{ leaf: string; referenced: boolean }>
}

const planArb: fc.Arbitrary<Plan[]> = fc.tuple(
  ...GROUPS.map((group) =>
    fc.record({
      group: fc.constant(group),
      dynamic: fc.boolean(),
      plain: fc.uniqueArray(
        fc.record({ leaf: plainLeaf, referenced: fc.boolean() }),
        { selector: (e) => e.leaf, maxLength: 6 },
      ),
      plural: fc.uniqueArray(
        fc.record({ leaf: pluralLeaf, referenced: fc.boolean() }),
        { selector: (e) => e.leaf, maxLength: 4 },
      ),
    }),
  ),
) as fc.Arbitrary<Plan[]>

function buildCatalog(keys: string[]): JsonObject {
  const root: JsonObject = {}
  for (const key of keys) {
    const [group, leaf] = key.split(".")
    const node = (root[group] ?? (root[group] = {})) as JsonObject
    node[leaf] = `value of ${key}`
  }
  return root
}

function planKeys(plans: Plan[]) {
  const keys: string[] = []
  const expectedUnused: string[] = []
  const sources: string[] = []
  for (const plan of plans) {
    if (plan.dynamic) {
      sources.push("t(`" + plan.group + ".${name}`)")
    }
    for (const { leaf, referenced } of plan.plain) {
      const key = `${plan.group}.${leaf}`
      keys.push(key)
      if (referenced) sources.push(`t("${key}")`)
      else if (!plan.dynamic) expectedUnused.push(key)
    }
    for (const { leaf, referenced } of plan.plural) {
      const base = `${plan.group}.${leaf}`
      keys.push(`${base}_one`, `${base}_other`)
      if (referenced) sources.push(`t("${base}", { count })`)
      else if (!plan.dynamic) {
        expectedUnused.push(`${base}_one`, `${base}_other`)
      }
    }
  }
  return { keys, expectedUnused: expectedUnused.sort(), sources }
}

describe("Property 15: unused-key detection is exact and locale-symmetric", () => {
  it("findUnusedKeys returns exactly the keys without literal, dynamic-prefix or plural-base reference", () => {
    fc.assert(
      fc.property(planArb, (plans) => {
        const { keys, expectedUnused, sources } = planKeys(plans)
        const catalog = buildCatalog(keys)
        expect(findUnusedKeys(flattenKeys(catalog), sources)).toEqual(
          expectedUnused,
        )
      }),
      { numRuns: 100 },
    )
  })

  it("is independent of source order and splitting across files", () => {
    fc.assert(
      fc.property(planArb, fc.integer({ min: 0, max: 5 }), (plans, cut) => {
        const { keys, sources } = planKeys(plans)
        const catalog = buildCatalog(keys)
        const flat = flattenKeys(catalog)
        const reversed = [...sources].reverse()
        const split = [
          sources.slice(0, cut).join("\n"),
          sources.slice(cut).join("\n"),
        ]
        const baseline = findUnusedKeys(flat, sources)
        expect(findUnusedKeys(flat, reversed)).toEqual(baseline)
        expect(findUnusedKeys(flat, split)).toEqual(baseline)
      }),
      { numRuns: 100 },
    )
  })

  it("locale catalogs built from the same keys flatten to equal key sets, and a dropped key shows up as the only difference", () => {
    fc.assert(
      fc.property(
        planArb,
        fc.nat(),
        (plans, pick) => {
          const { keys } = planKeys(plans)
          fc.pre(keys.length > 0)
          const en = buildCatalog(keys)
          const pt = buildCatalog(keys)
          expect(flattenKeys(en)).toEqual(flattenKeys(pt))

          const dropped = keys[pick % keys.length]
          const ptWithout = buildCatalog(keys.filter((k) => k !== dropped))
          const enSet = new Set(flattenKeys(en))
          const ptSet = new Set(flattenKeys(ptWithout))
          const diff = [
            ...[...enSet].filter((k) => !ptSet.has(k)),
            ...[...ptSet].filter((k) => !enSet.has(k)),
          ]
          expect(diff).toEqual([dropped])
        },
      ),
      { numRuns: 100 },
    )
  })
})

// ---------------------------------------------------------------------------
// Property 16
// ---------------------------------------------------------------------------

// Words deliberately free of "stage" / "etapa" (as whole words).
const safeWord = fc.constantFrom(
  "Attributes",
  "extracted",
  "Atributos",
  "extraídos",
  "Campaign",
  "Execution",
  "Evaluation",
  "concluída",
  "running",
  "step",
  "Operationalization",
  "{{stage}}",
  "{{etapa}}",
)
const safeValue = fc
  .array(safeWord, { minLength: 1, maxLength: 6 })
  .map((w) => w.join(" "))

const legacyFragment = fc
  .tuple(
    fc.constantFrom("Stage", "stage", "Etapa", "etapa"),
    fc.integer({ min: 1, max: 9 }),
  )
  .map(([word, n]) => `${word} ${n}`)

const leafName = fc.stringMatching(/^[a-z]{1,6}$/)

const NAMESPACES: Array<{ name: string; en: JsonValue; pt: JsonValue }> = [
  { name: "common", en: enCommon as JsonValue, pt: ptCommon as JsonValue },
  { name: "pipeline", en: enPipeline as JsonValue, pt: ptPipeline as JsonValue },
  {
    name: "preferences",
    en: enPreferences as JsonValue,
    pt: ptPreferences as JsonValue,
  },
  { name: "stix", en: enStix as JsonValue, pt: ptStix as JsonValue },
]

describe("Property 16: no legacy stage wording in locale values", () => {
  it("the shipped en and pt-BR catalogs contain no Legacy_Stage_Wording", () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...NAMESPACES),
        fc.constantFrom("en", "pt"),
        (ns, locale) => {
          expect(findLegacyStageWording(ns[locale])).toEqual([])
        },
      ),
      { numRuns: 100 },
    )
  })

  it("reports no key for synthetic values free of stage wording (placeholders ignored)", () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.tuple(leafName, safeValue), {
          selector: ([k]) => k,
          maxLength: 10,
        }),
        (entries) => {
          const catalog: JsonObject = Object.fromEntries(entries)
          expect(findLegacyStageWording(catalog)).toEqual([])
        },
      ),
      { numRuns: 100 },
    )
  })

  it("reports exactly the keys whose value contains 'Stage N' or 'Etapa N'", () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(
          fc.record({
            key: leafName,
            prefix: safeValue,
            suffix: safeValue,
            legacy: fc.option(legacyFragment, { nil: null }),
          }),
          { selector: (e) => e.key, minLength: 1, maxLength: 10 },
        ),
        (entries) => {
          const catalog: JsonObject = {}
          const expected: Array<[string, string]> = []
          for (const { key, prefix, suffix, legacy } of entries) {
            const value =
              legacy === null
                ? `${prefix} ${suffix}`
                : `${prefix} ${legacy} ${suffix}`
            catalog[key] = value
            if (legacy !== null) expected.push([key, value])
          }
          const byKey = (a: [string, string], b: [string, string]) =>
            a[0].localeCompare(b[0])
          expect([...findLegacyStageWording(catalog)].sort(byKey)).toEqual(
            expected.sort(byKey),
          )
        },
      ),
      { numRuns: 100 },
    )
  })
})
