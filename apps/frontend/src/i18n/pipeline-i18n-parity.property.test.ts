import { describe, it, expect } from "vitest"
import fc from "fast-check"

import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"
import ptCommon from "@/i18n/locales/pt-BR/common.json"
import enCommon from "@/i18n/locales/en/common.json"
import ptStix from "@/i18n/locales/pt-BR/stix.json"
import enStix from "@/i18n/locales/en/stix.json"
import ptPreferences from "@/i18n/locales/pt-BR/preferences.json"
import enPreferences from "@/i18n/locales/en/preferences.json"

/**
 * Property-based test (fast-check, >= 100 runs) for pt-BR/en key parity across
 * the FULL `pipeline` i18n namespace (pipeline-six-stages, task 14.7).
 *
 * It enumerates EVERY leaf path in both locales and asserts, over many\r\n * generated paths, that the two locales declare exactly the same key tree and\r\n * that no value is empty.\r\n *
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
})

/**
 * Property 14: i18n key parity for the keys added by agentic-only-flow-redesign
 * (task 10.6): `flow.*` and `operationalization.*` live in `pipeline`, while
 * `ingestion.*` lives in `common`. Each locale must declare exactly the same
 * leaf paths under these groups, all non-empty.
 *
 * Feature: agentic-only-flow-redesign, Property 14
 * Validates: Requirements 5.7, 5.8, 6.3, 6.4, 6.5
 */
describe("redesign i18n key parity (agentic-only-flow-redesign)", () => {
  const groups: Array<[string, JsonValue, JsonValue]> = [
    ["flow", getByPath(ptPipeline as JsonValue, "flow")!, getByPath(enPipeline as JsonValue, "flow")!],
    [
      "operationalization",
      getByPath(ptPipeline as JsonValue, "operationalization")!,
      getByPath(enPipeline as JsonValue, "operationalization")!,
    ],
    [
      "ingestion",
      getByPath(ptCommon as JsonValue, "ingestion")!,
      getByPath(enCommon as JsonValue, "ingestion")!,
    ],
  ]

  it("Property: flow/operationalization/ingestion have identical, non-empty leaves in both locales", () => {
    fc.assert(
      fc.property(fc.constantFrom(...groups), ([name, pt, en]) => {
        expect(pt, `${name} missing in pt-BR`).toBeDefined()
        expect(en, `${name} missing in en`).toBeDefined()
        const ptLeaves = collectLeafPaths(pt).sort()
        const enLeaves = collectLeafPaths(en).sort()
        expect(ptLeaves.length).toBeGreaterThan(0)
        expect(ptLeaves).toEqual(enLeaves)
        for (const leaf of ptLeaves) {
          const ptValue = getByPath(pt, leaf)
          const enValue = getByPath(en, leaf)
          expect(typeof ptValue, `${name}.${leaf} (pt-BR)`).toBe("string")
          expect(typeof enValue, `${name}.${leaf} (en)`).toBe("string")
          expect((ptValue as string).length).toBeGreaterThan(0)
          expect((enValue as string).length).toBeGreaterThan(0)
        }
      }),
      { numRuns: 30 },
    )
  })

  it("Property: required ingestion source/status keys exist in both locales", () => {
    const keys = [
      ...["stix_packages", "curated_seed", "atomic", "docker_catalog", "attack_flow"].map((s) => `ingestion.source.${s}`),
      ...["pending", "running", "ready", "failed"].map((s) => `ingestion.status.${s}`),
    ]
    fc.assert(
      fc.property(fc.constantFrom(...keys), (key) => {
        expect(getByPath(ptCommon as JsonValue, key), `${key} (pt-BR)`).toBeTypeOf("string")
        expect(getByPath(enCommon as JsonValue, key), `${key} (en)`).toBeTypeOf("string")
      }),
      { numRuns: 30 },
    )
  })

  it("Property: displayed values say 'attack flow', never 'DAG'", () => {
    fc.assert(
      fc.property(fc.constantFrom(...groups), ([, pt, en]) => {
        for (const tree of [pt, en]) {
          for (const leaf of collectLeafPaths(tree)) {
            expect(getByPath(tree, leaf) as string).not.toMatch(/\bDAGs?\b/)
          }
        }
      }),
      { numRuns: 30 },
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
    "operationalization",
    "flow",
    "environment",
    "execution",
    "evaluation",
  ] as const

  // The full `guided.speech.<phase>` and `aiRun.stageName.<phase>` keys.
  const PHASE_KEYS = [
    ...AGENTIC_PHASES.map((p) => `guided.speech.${p}`),
    ...AGENTIC_PHASES.map((p) => `aiRun.stageName.${p}`),
    "guided.generatingFlow",
    "flow.provenance.exact",
    "flow.provenance.adapted",
    "flow.provenance.created",
    "flow.cycles.title",
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
  // Property: the Input copy treats the STIX version AS the package (one choice),
  // never "package version" / "package and version" as distinct picks.
  it("Property: input copy never presents package and version as separate choices", () => {
    const INPUT_KEYS = [
      "input.heroDescription",
      "input.card.packageTitle",
      "input.card.campaignTitle",
      "input.card.campaignBlocked",
    ]
    const SPLIT = /versão do pacote|pacote e versão|pacote, versão|package version|package and version|package, version/i
    fc.assert(
      fc.property(fc.constantFrom(...INPUT_KEYS), (key) => {
        for (const catalog of [ptPipeline, enPipeline]) {
          const value = getByPath(catalog as JsonValue, key) as string
          expect(typeof value, `${key} missing`).toBe("string")
          expect(value, `${key} splits package and version`).not.toMatch(SPLIT)
        }
      }),
      { numRuns: 50 },
    )
  })})

/**
 * Terminology parity: "input" vs "setup" (agentic-only-flow-redesign, task 22.3).
 *
 * "input" is the phase where the user picks the STIX version (the package) and
 * the campaign. "setup" means exclusively the installation / first-run ingestion
 * of the knowledge bases (`ingestion.*`). The two concepts must not mix in i18n
 * keys, displayed values or component sources. Deterministic: reads the locale
 * JSON and the component sources statically.
 *
 * Validates: Requirements 5.7, 5.8, 5.9
 */
describe("input vs setup terminology (agentic-only-flow-redesign, task 22.3)", () => {
  const locales: Record<string, Record<string, JsonValue>> = {
    en: {
      common: enCommon as JsonValue,
      pipeline: enPipeline as JsonValue,
      stix: enStix as JsonValue,
      preferences: enPreferences as JsonValue,
    },
    "pt-BR": {
      common: ptCommon as JsonValue,
      pipeline: ptPipeline as JsonValue,
      stix: ptStix as JsonValue,
      preferences: ptPreferences as JsonValue,
    },
  }

  const SETUP_WORD = /set[\s-]?up/i

  /** Every leaf of every namespace of a locale, as `{ key, value }`. */
  function entriesOf(locale: string): Array<{ key: string; value: string }> {
    const out: Array<{ key: string; value: string }> = []
    for (const tree of Object.values(locales[locale])) {
      for (const key of collectLeafPaths(tree)) {
        out.push({ key, value: String(getByPath(tree, key)) })
      }
    }
    return out
  }

  it("(a) input.* keys exist in en and pt-BR", () => {
    const required = [
      "input.heroTitle",
      "input.heroDescription",
      "input.card.packageTitle",
      "input.card.campaignTitle",
      "input.card.campaignBlocked",
      "input.back",
      "input.next",
      "input.nextHint",
      "input.run.title",
      "input.run.changeCampaign",
      "input.run.campaign",
    ]
    for (const locale of Object.keys(locales)) {
      for (const key of required) {
        const value = getByPath(locales[locale].pipeline, key)
        expect(typeof value, `${key} missing in ${locale}`).toBe("string")
        expect((value as string).length, `${key} empty in ${locale}`).toBeGreaterThan(0)
      }
    }
    // Same input.* leaf set in both locales.
    const leaves = (tree: JsonValue) =>
      collectLeafPaths(getByPath(tree, "input")!).sort()
    expect(leaves(ptPipeline as JsonValue)).toEqual(leaves(enPipeline as JsonValue))
  })

  it("(b) no selection-domain value says 'setup' / 'set up'", () => {
    const SELECTION = /^(input|baseStix|campaign|stix)\./
    for (const locale of Object.keys(locales)) {
      const selection = entriesOf(locale).filter((e) => SELECTION.test(e.key))
      expect(selection.length).toBeGreaterThan(0)
      for (const { key, value } of selection) {
        expect(value, `${locale}:${key} mentions setup`).not.toMatch(SETUP_WORD)
      }
    }
  })

  it("(b) no i18n key has a 'setup' segment outside ingestion.*", () => {
    for (const locale of Object.keys(locales)) {
      for (const { key } of entriesOf(locale)) {
        if (key.startsWith("ingestion.")) continue
        const segments = key.split(".")
        expect(
          segments.some((s) => /setup/i.test(s)),
          `${locale}:${key} has a 'setup' segment`,
        ).toBe(false)
      }
    }
  })

  it("(c) component sources do not use the legacy setup identifiers", () => {
    const sources = import.meta.glob("../components/**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>
    const files = Object.keys(sources)
    expect(files.some((f) => f.endsWith("ExecutionWizard.tsx"))).toBe(true)

    const LEGACY = /SetupScreen|handleBackToSetup|setup-panel|back-to-setup/
    for (const file of files) {
      expect(sources[file], `${file} uses a legacy setup identifier`).not.toMatch(LEGACY)
    }
  })
})
