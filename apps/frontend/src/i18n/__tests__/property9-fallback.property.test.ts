// Feature: i18n-key-english-standardization, Property 9: Fallback resolution preserved
//
// Property-based test (the repo's existing fast-check harness, >= 100 runs) for
// the design's Property 9 / Requirements 3.4, 3.5, 3.6: the i18n KEY migration
// preserves fallback resolution. For every (now English) key path, resolving
// the key in a locale where its VALUE is absent yields the `pt-BR` value for
// that SAME English key path — never a raw key, and never an empty string
// beyond what `returnEmptyString: true` already allowed pre-migration.
//
// The i18n wiring under test is `src/i18n/index.ts`, which configures
// `fallbackLng: "pt-BR"` and `returnEmptyString: true` and exports the pure
// `resolveWithFallback(key, language)` helper. This test drives that helper and
// does NOT modify the i18n source or any app namespace.
//
// Two complementary input populations exercise the property:
//
//   1. REAL catalog keys. Because key parity holds (Property 1), every English
//      key path exists in BOTH locales, so the realistic "value absent in the
//      active locale" case is the per-key fallback path i18next walks whenever
//      a selected-locale lookup misses. Over every real English key path we
//      assert `resolveWithFallback` never returns the raw key and that an `en`
//      resolution that would be empty is backed by a non-empty pt-BR value —
//      i.e. the pt-BR fallback value for the same English path is what surfaces.
//
//   2. A CONTROLLED test-only namespace. To exercise a genuinely-absent `en`
//      value without depending on which real keys happen to be present, a
//      test-only bundle registers a key in `pt-BR` that is deliberately missing
//      from `en`. Resolving it in `en` must return the `pt-BR` value for that
//      same key path (never the raw key, never ""). This mirrors the existing
//      Property 11 scenario in `i18n.property.test.ts` and never touches the
//      app namespaces (`deep + overwrite` keeps registration idempotent).
//
// Validates: Requirements 3.4, 3.5, 3.6

import { describe, it, expect } from "vitest"
import fc from "fast-check"
import i18n from "i18next"

import enCommon from "@/i18n/locales/en/common.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"
import enPreferences from "@/i18n/locales/en/preferences.json"
import enStix from "@/i18n/locales/en/stix.json"
import ptCommon from "@/i18n/locales/pt-BR/common.json"
import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import ptPreferences from "@/i18n/locales/pt-BR/preferences.json"
import ptStix from "@/i18n/locales/pt-BR/stix.json"

import { resolveWithFallback } from "@/i18n/index"
import {
  collectNamespaceLeafPaths,
  getByPath,
  type JsonValue,
} from "@/i18n/__tests__/catalog-utils"

// ---------------------------------------------------------------------------
// Population 1: real catalog key paths (English, post-migration).
// ---------------------------------------------------------------------------

interface NamespaceResources {
  readonly name: string
  readonly pt: JsonValue
  readonly en: JsonValue
}

const NAMESPACES: readonly NamespaceResources[] = [
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

interface CatalogKey {
  readonly namespace: string
  /** Fully-qualified key path within the namespace, e.g. "app.title". */
  readonly path: string
  /** The namespaced lookup key i18next expects, e.g. "common:app.title". */
  readonly lookup: string
  /** The byte-for-byte pt-BR value for this English key path. */
  readonly ptValue: string
}

// Every real English leaf key across all four namespaces. Parity guarantees the
// path exists in both locales, so the pt-BR value is always defined.
const ALL_CATALOG_KEYS: readonly CatalogKey[] = NAMESPACES.flatMap((ns) =>
  collectNamespaceLeafPaths(ns.pt).map((path) => {
    const ptValue = getByPath(ns.pt, path)
    if (typeof ptValue !== "string") {
      throw new Error(
        `${ns.name}: expected a string value at "${path}", got ${JSON.stringify(ptValue)}`,
      )
    }
    return {
      namespace: ns.name,
      path,
      lookup: `${ns.name}:${path}`,
      ptValue,
    }
  }),
)

// ---------------------------------------------------------------------------
// Population 2: a controlled test-only namespace with a genuinely-absent `en`
// value, so the per-key fallback path is exercised directly rather than relying
// on which real keys happen to be missing.
// ---------------------------------------------------------------------------

const TEST_NS = "property9Fallback"

// `onlyPt` is present in pt-BR but deliberately absent in `en`, so resolving it
// in `en` must fall back to the pt-BR value. `shared` exists in both. `emptyPt`
// is an intentionally-empty pt-BR value: with `returnEmptyString: true` it is a
// real (present) value and must resolve to "" rather than falling back further.
const PT_BUNDLE = {
  shared: "compartilhado-pt",
  onlyPt: "somente-pt",
  emptyPt: "",
} as const

const EN_BUNDLE = {
  shared: "shared-en",
  // `onlyPt` intentionally missing in en -> must fall back to the pt-BR value.
  // `emptyPt` intentionally missing in en -> falls back to the empty pt-BR value.
} as const

i18n.addResourceBundle("pt-BR", TEST_NS, PT_BUNDLE, true, true)
i18n.addResourceBundle("en", TEST_NS, EN_BUNDLE, true, true)

describe("i18n Property 9 — fallback resolution preserved", () => {
  it("sanity: the real-catalog key population is non-empty and all paths are present in pt-BR", () => {
    expect(ALL_CATALOG_KEYS.length).toBeGreaterThan(50)
    for (const key of ALL_CATALOG_KEYS) {
      expect(
        typeof key.ptValue,
        `${key.namespace}:${key.path} has a string pt-BR value`,
      ).toBe("string")
    }
  })

  // Feature: i18n-key-english-standardization, Property 9: Fallback resolution preserved
  // Validates: Requirements 3.4, 3.5, 3.6
  it("Property 9: resolving any real English key (en or pt-BR) yields the pt-BR value when the en value is absent, never a raw key", () => {
    // Range over (real English key path) x (active locale). pt-BR is the
    // fallback source; `en` either has its own value or falls back per-key to
    // pt-BR. In every case the resolved text must be a real value — never the
    // raw key path — and whenever `en` falls back it must land on the pt-BR
    // value for that SAME English key path.
    const localeArb = fc.constantFrom<"pt-BR" | "en">("pt-BR", "en")

    fc.assert(
      fc.property(
        fc.constantFrom(...ALL_CATALOG_KEYS),
        localeArb,
        (key, locale) => {
          const resolved = resolveWithFallback(key.lookup, locale)

          // Req. 3.5: a migrated key never resolves to the raw key path. i18next
          // returns the key itself (bare leaf or "ns:path") when nothing
          // resolves, so the resolved text must differ from both forms.
          expect(
            resolved,
            `${key.lookup} (@${locale}) resolved to the raw key`,
          ).not.toBe(key.lookup)
          expect(
            resolved,
            `${key.lookup} (@${locale}) resolved to the raw leaf path`,
          ).not.toBe(key.path)

          // Req. 3.6: pt-BR is the fallback locale. Resolving in pt-BR always
          // returns the pt-BR value for the same English key path.
          if (locale === "pt-BR") {
            expect(
              resolved,
              `${key.lookup} (@pt-BR) should be the pt-BR value`,
            ).toBe(key.ptValue)
          }

          // Req. 3.4/3.5: when the en lookup would be empty, the pt-BR fallback
          // value for the SAME English key path is what surfaces; a non-empty
          // pt-BR value is therefore never reduced to "" by the en resolution.
          const enValue = getByPath(
            NAMESPACES.find((n) => n.name === key.namespace)!.en,
            key.path,
          )
          const enIsNonEmptyString =
            typeof enValue === "string" && enValue.length > 0
          if (locale === "en" && !enIsNonEmptyString && key.ptValue !== "") {
            // en value absent/empty but pt-BR value is real -> fallback surfaces
            // the pt-BR value, so the resolution is not an unexpected "".
            expect(
              resolved,
              `${key.lookup} (@en) should fall back to the non-empty pt-BR value`,
            ).toBe(key.ptValue)
          }
        },
      ),
      { numRuns: 200 },
    )
  })

  // Feature: i18n-key-english-standardization, Property 9: Fallback resolution preserved
  // Validates: Requirements 3.4, 3.5, 3.6
  it("Property 9: a key absent in en falls back to the pt-BR value for the same key path, never a raw key or unexpected empty string", () => {
    // Controlled scenario: `onlyPt` genuinely missing from en, `shared` present
    // in both, `emptyPt` intentionally empty in pt-BR (a present value under
    // returnEmptyString: true).
    const keyArb = fc.constantFrom<keyof typeof PT_BUNDLE>(
      "shared",
      "onlyPt",
      "emptyPt",
    )
    const localeArb = fc.constantFrom<"pt-BR" | "en">("pt-BR", "en")

    fc.assert(
      fc.property(keyArb, localeArb, (key, locale) => {
        const lookup = `${TEST_NS}:${key}`
        const resolved = resolveWithFallback(lookup, locale)

        const ptValue = PT_BUNDLE[key]
        const hasEnValue = Object.prototype.hasOwnProperty.call(EN_BUNDLE, key)

        if (locale === "en" && hasEnValue) {
          // en has its own value -> that value is used (no fallback needed).
          expect(resolved).toBe(EN_BUNDLE[key as keyof typeof EN_BUNDLE])
        } else {
          // Either pt-BR is active, or the en value is absent -> the pt-BR value
          // for this SAME key path surfaces (Req. 3.5, 3.6).
          expect(
            resolved,
            `${lookup} (@${locale}) should resolve to the pt-BR value`,
          ).toBe(ptValue)
        }

        // Req. 3.5: never the raw key, for any present key path.
        expect(
          resolved,
          `${lookup} (@${locale}) resolved to the raw key`,
        ).not.toBe(lookup)
        expect(resolved).not.toBe(key)

        // Req. 3.4: an empty string only appears where returnEmptyString already
        // allowed it — i.e. the pt-BR value itself is "". A non-empty pt-BR
        // value is never reduced to "" by fallback.
        if (ptValue !== "") {
          expect(
            resolved,
            `${lookup} (@${locale}) unexpectedly resolved to empty string`,
          ).not.toBe("")
        } else {
          // emptyPt: returnEmptyString: true means the present empty value wins.
          expect(resolved).toBe("")
        }
      }),
      { numRuns: 200 },
    )
  })
})
