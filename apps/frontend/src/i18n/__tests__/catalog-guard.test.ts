import { describe, expect, it } from "vitest"

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
  findLegacyPhaseWording,
  findUnusedKeys,
  flattenKeys,
} from "@/i18n/__tests__/catalog-utils"

/**
 * Guard test for the Locale_Catalog (agentic-only-flow-redesign, task 42.3).
 * Fails with the offending keys when a key is unused, exists in only one
 * locale, or a value carries Legacy_Phase_Wording.
 *
 * Validates: Requirements 9.6, 9.7, 9.8
 */

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

// Values that legitimately use legacy phase wording with a non-pipeline meaning.
// Add entries as `"<locale>:<namespace>:<key>"` with a comment explaining why.
const LEGACY_WORDING_EXCEPTIONS = new Set<string>([])

// Application sources (components, hooks, pages), excluding locale JSON and
// tests, read as raw text so no external script is needed.
const rawSources = import.meta.glob("/src/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>

const SOURCES = Object.entries(rawSources)
  .filter(
    ([path]) =>
      !path.includes("/src/i18n/locales/") &&
      !path.includes("/__tests__/") &&
      !/\.(test|spec)\.tsx?$/.test(path),
  )
  .map(([, text]) => text)

describe("Locale_Catalog guard", () => {
  it("has no unused keys, keeps en/pt-BR parity and has no legacy phase wording", () => {
    expect(SOURCES.length).toBeGreaterThan(10)

    const unused: string[] = []
    const parity: string[] = []
    const legacy: string[] = []
    for (const ns of NAMESPACES) {
      for (const [locale, catalog] of [
        ["en", ns.en],
        ["pt-BR", ns.pt],
      ] as const) {
        for (const key of findUnusedKeys(flattenKeys(catalog), SOURCES)) {
          unused.push(`${locale}:${ns.name}:${key}`)
        }
        for (const [key, value] of findLegacyPhaseWording(catalog)) {
          if (LEGACY_WORDING_EXCEPTIONS.has(`${locale}:${ns.name}:${key}`)) continue
          legacy.push(`${locale}:${ns.name}:${key} = "${value}"`)
        }
      }
      const en = new Set(flattenKeys(ns.en))
      const pt = new Set(flattenKeys(ns.pt))
      for (const key of en) if (!pt.has(key)) parity.push(`${ns.name}:${key} missing in pt-BR`)
      for (const key of pt) if (!en.has(key)) parity.push(`${ns.name}:${key} missing in en`)
    }

    expect(unused, `Unused keys:\n${unused.join("\n")}`).toEqual([])
    expect(parity, `Key parity:\n${parity.join("\n")}`).toEqual([])
    expect(legacy, `Legacy wording:\n${legacy.join("\n")}`).toEqual([])
  })
})
