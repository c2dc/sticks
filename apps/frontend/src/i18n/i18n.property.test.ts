import { describe, it, expect } from "vitest"
import fc from "fast-check"
import i18n from "i18next"

import {
  DEFAULT_LANGUAGE,
  SUPPORTED_LANGUAGES,
  normalizeLanguage,
  resolveWithFallback,
} from "./index"

// These are property-based tests for the pure i18n helpers exposed by
// `src/i18n/index.ts`. They drive `resolveWithFallback` and `normalizeLanguage`
// over many generated inputs (fast-check, >= 100 runs each) without a live
// React component. The i18n source is NOT modified by these tests; Property 11
// adds a test-only namespace via i18next `addResourceBundle` to build a
// controlled fallback scenario, and never touches the app namespaces.

// A test-only namespace so we can assert fallback precisely without depending on
// which real keys happen to be missing in `en`. pt-BR is the fallback source, so
// every key present here in pt-BR exists, while `onlyPt` is deliberately absent
// from `en` to exercise the per-key fallback path (Req. 8.3).
const TEST_NS = "propTestI18n"

const PT_BUNDLE = {
  shared: "valor-pt",
  onlyPt: "somente-pt",
} as const

const EN_BUNDLE = {
  shared: "value-en",
  // `onlyPt` intentionally missing in `en` -> must fall back to pt-BR.
} as const

// `deep: true, overwrite: true` keeps the bundle registration idempotent across
// repeated test runs within the same process.
i18n.addResourceBundle("pt-BR", TEST_NS, PT_BUNDLE, true, true)
i18n.addResourceBundle("en", TEST_NS, EN_BUNDLE, true, true)

describe("i18n property tests", () => {
  // Feature: pipeline-ui, Property 11: Fallback de i18n sempre resolve para pt-BR.
  // Para qualquer chave de tradução e qualquer idioma selecionado, se a chave
  // não possui valor no idioma selecionado, o texto exibido é o valor
  // correspondente em pt-BR.
  // Validates: Requisitos 8.3
  it("Property 11: fallback de i18n sempre resolve para pt-BR", () => {
    // Generate a language (valid or invalid) and a key that is either shared
    // (present in both languages) or pt-only (missing in `en`).
    const languageArb = fc.constantFrom<string>(
      "pt-BR",
      "en",
      "es",
      "fr",
      "",
      "PT-br",
      "en-US",
    )
    const keyArb = fc.constantFrom<keyof typeof PT_BUNDLE>("shared", "onlyPt")

    fc.assert(
      fc.property(languageArb, keyArb, (language, key) => {
        const effective = normalizeLanguage(language)
        const fullKey = `${TEST_NS}:${key}`
        const resolved = resolveWithFallback(fullKey, language)

        const ptValue = PT_BUNDLE[key]
        const hasEnValue = Object.prototype.hasOwnProperty.call(EN_BUNDLE, key)

        if (effective === "en" && !hasEnValue) {
          // Key missing in the selected language -> displayed text is pt-BR.
          expect(resolved).toBe(ptValue)
        } else if (effective === "en") {
          // Key present in the selected language -> that language's value.
          expect(resolved).toBe(EN_BUNDLE[key as keyof typeof EN_BUNDLE])
        } else {
          // Invalid/other languages normalize to pt-BR -> pt-BR value.
          expect(resolved).toBe(ptValue)
        }

        // Invariant across all cases: a key that exists in pt-BR never resolves
        // to the raw key (i.e. it is never "missing" once fallback is applied).
        expect(resolved).toBe(
          effective === "en" && hasEnValue
            ? EN_BUNDLE[key as keyof typeof EN_BUNDLE]
            : ptValue,
        )
      }),
      { numRuns: 200 },
    )
  })

  // Feature: pipeline-ui, Property 12: Normalização de idioma inválido para pt-BR.
  // Para qualquer valor de preferência de idioma persistido, o idioma efetivo é
  // esse valor quando ele é pt-BR ou en, e é pt-BR em qualquer outro caso.
  // Validates: Requisitos 8.7
  it("Property 12: normalização de idioma inválido para pt-BR", () => {
    // Generate arbitrary strings plus the valid languages and null/undefined,
    // covering the whole persisted-preference input space.
    const valueArb = fc.oneof(
      fc.constantFrom<string>(...SUPPORTED_LANGUAGES),
      fc.string(),
      fc.constant(null),
      fc.constant(undefined),
    )

    fc.assert(
      fc.property(valueArb, (value) => {
        const result = normalizeLanguage(value)

        if (value === "pt-BR" || value === "en") {
          // Valid preference -> effective language is exactly that value.
          expect(result).toBe(value)
        } else {
          // Any other value (invalid string, null, undefined) -> pt-BR.
          expect(result).toBe(DEFAULT_LANGUAGE)
        }

        // The result is always one of the supported languages.
        expect(SUPPORTED_LANGUAGES).toContain(result)
      }),
      { numRuns: 200 },
    )
  })
})
