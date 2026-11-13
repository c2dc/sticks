import { describe, it, expect } from "vitest"

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
  findLegacyStageWording,
  findUnusedKeys,
  flattenKeys,
} from "@/i18n/__tests__/catalog-utils"

/**
 * Guard test for the Locale_Catalog (agentic-only-flow-redesign, task 42.3).
 * Fails with the offending keys when a key is unused, exists in only one
 * locale, or a value carries Legacy_Stage_Wording.
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

// Values that legitimately use "stage"/"etapa" with a non-pipeline meaning.
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
  it("reads application sources", () => {
    expect(SOURCES.length).toBeGreaterThan(10)
  })

  it("has no unused keys in any locale", () => {
    const offenders: string[] = []
    for (const ns of NAMESPACES) {
      for (const [locale, catalog] of [
        ["en", ns.en],
        ["pt-BR", ns.pt],
      ] as const) {
        for (const key of findUnusedKeys(flattenKeys(catalog), SOURCES)) {
          offenders.push(`${locale}:${ns.name}:${key}`)
        }
      }
    }
    expect(offenders, `Unused keys:\n${offenders.join("\n")}`).toEqual([])
  })

  it("keeps the same key set in en and pt-BR", () => {
    const offenders: string[] = []
    for (const ns of NAMESPACES) {
      const en = new Set(flattenKeys(ns.en))
      const pt = new Set(flattenKeys(ns.pt))
      for (const key of en) {
        if (!pt.has(key)) offenders.push(`${ns.name}:${key} missing in pt-BR`)
      }
      for (const key of pt) {
        if (!en.has(key)) offenders.push(`${ns.name}:${key} missing in en`)
      }
    }
    expect(offenders, `Key parity:\n${offenders.join("\n")}`).toEqual([])
  })

  it("has no Legacy_Stage_Wording in values", () => {
    const offenders: string[] = []
    for (const ns of NAMESPACES) {
      for (const [locale, catalog] of [
        ["en", ns.en],
        ["pt-BR", ns.pt],
      ] as const) {
        for (const [key, value] of findLegacyStageWording(catalog)) {
          if (LEGACY_WORDING_EXCEPTIONS.has(`${locale}:${ns.name}:${key}`)) {
            continue
          }
          offenders.push(`${locale}:${ns.name}:${key} = "${value}"`)
        }
      }
    }
    expect(offenders, `Legacy wording:\n${offenders.join("\n")}`).toEqual([])
  })
})

describe("Locale_Catalog guard detectors (synthetic)", () => {
  it("flags an offending unused key and accepts literal references", () => {
    const keys = ["a.used", "a.orphan"]
    expect(findUnusedKeys(keys, [`t("a.used")`])).toEqual(["a.orphan"])
  })

  it("treats a dynamic template prefix as a reference", () => {
    const keys = ["status.running", "status.done", "other.x"]
    expect(findUnusedKeys(keys, ["t(`status.${s}`)"])).toEqual(["other.x"])
  })

  it("treats plural variants as used when the base key is referenced", () => {
    const keys = ["items_one", "items_other", "lonely_one"]
    expect(findUnusedKeys(keys, [`t("items", { count })`])).toEqual([
      "lonely_one",
    ])
  })

  it("reports key and value for legacy stage wording", () => {
    const catalog = {
      ok: "Attributes extracted.",
      bad: "Attributes extracted — Stage 2 completed.",
      ptBad: "Etapa 2 concluída",
      placeholder: "Value {{stage}}",
    }
    expect(findLegacyStageWording(catalog)).toEqual([
      ["bad", "Attributes extracted — Stage 2 completed."],
      ["ptBad", "Etapa 2 concluída"],
    ])
  })
})
