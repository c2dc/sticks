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
  type JsonValue,
  collectNamespaceLeafPaths,
} from "@/i18n/__tests__/catalog-utils"

/**
 * Property-based test (fast-check, >= 100 runs) for Property 1 of the i18n key
 * English standardization migration: per-namespace key parity between locales.
 *
 * This is a dedicated property file for Property 1 so it can live alongside the
 * other per-property files (5.2-5.9) without editing the pre-existing shared
 * parity tests. It reuses the shared leaf-path helper
 * (`collectNamespaceLeafPaths`) from `catalog-utils.ts` and the repo's existing
 * fast-check harness; it does NOT reimplement a PBT library and does NOT add or
 * modify any i18n keys (it only reads the locale JSON).
 *
 * Covered namespaces (all four, both locales): common, pipeline, preferences,
 * stix.
 *
 * Feature: i18n-key-english-standardization, Property 1: Per-namespace key parity between locales
 * Validates: Requirements 1.3, 6.1, 6.2, 6.3
 */

interface NamespacePair {
  name: string
  pt: JsonValue
  en: JsonValue
}

const NAMESPACES: NamespacePair[] = [
  { name: "common", pt: ptCommon as JsonValue, en: enCommon as JsonValue },
  {
    name: "pipeline",
    pt: ptPipeline as JsonValue,
    en: enPipeline as JsonValue,
  },
  {
    name: "preferences",
    pt: ptPreferences as JsonValue,
    en: enPreferences as JsonValue,
  },
  { name: "stix", pt: ptStix as JsonValue, en: enStix as JsonValue },
]

describe("i18n Property 1 — per-namespace key parity between locales", () => {
  // Property 1: For all namespaces, the set of fully-qualified `en` key paths
  // equals the set of `pt-BR` key paths. Set equality implies equal counts, so
  // this single invariant subsumes the "equal key count per namespace" and
  // "adding/removing a key preserves parity" candidate properties.
  it("Property 1: en and pt-BR declare identical key sets for every namespace", () => {
    // Sanity guard: every namespace contributes a non-empty key tree, so the
    // property is exercising real data rather than vacuously passing.
    for (const ns of NAMESPACES) {
      expect(
        collectNamespaceLeafPaths(ns.pt).length,
        `${ns.name} (pt-BR) should have keys`,
      ).toBeGreaterThan(0)
      expect(
        collectNamespaceLeafPaths(ns.en).length,
        `${ns.name} (en) should have keys`,
      ).toBeGreaterThan(0)
    }

    fc.assert(
      fc.property(fc.constantFrom(...NAMESPACES), (ns) => {
        const ptPaths = collectNamespaceLeafPaths(ns.pt)
        const enPaths = collectNamespaceLeafPaths(ns.en)

        // Set equality of fully-qualified leaf paths across the two locales.
        // `collectNamespaceLeafPaths` already returns a sorted, de-duplicated
        // array, so `toEqual` compares the two key SETS directly.
        expect(
          enPaths,
          `${ns.name}: en/pt-BR key sets differ`,
        ).toEqual(ptPaths)

        // Equal counts follow from set equality, asserted explicitly for a
        // clearer failure signal if the sets ever diverge in size.
        expect(
          enPaths.length,
          `${ns.name}: en/pt-BR key counts differ`,
        ).toBe(ptPaths.length)
      }),
      { numRuns: 100 },
    )
  })

  // Companion per-path property: for any key path present in EITHER locale of a
  // namespace, it is present in BOTH. This drives the parity invariant at the
  // granularity of individual paths (not just the aggregate set), so a single
  // divergent key surfaces the exact path that broke parity.
  it("Property 1: every key path exists in both locales of its namespace", () => {
    const universe = NAMESPACES.flatMap((ns) => {
      const union = Array.from(
        new Set([
          ...collectNamespaceLeafPaths(ns.pt),
          ...collectNamespaceLeafPaths(ns.en),
        ]),
      )
      return union.map((path) => ({ namespace: ns.name, path }))
    })

    expect(universe.length).toBeGreaterThan(50)

    fc.assert(
      fc.property(fc.constantFrom(...universe), ({ namespace, path }) => {
        const ns = NAMESPACES.find((n) => n.name === namespace)!
        const inPt = collectNamespaceLeafPaths(ns.pt).includes(path)
        const inEn = collectNamespaceLeafPaths(ns.en).includes(path)

        expect(inPt, `${namespace}: ${path} missing in pt-BR`).toBe(true)
        expect(inEn, `${namespace}: ${path} missing in en`).toBe(true)
      }),
      { numRuns: 200 },
    )
  })
})
