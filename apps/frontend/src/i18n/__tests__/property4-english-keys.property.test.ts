// Feature: i18n-key-english-standardization, Property 4: Key paths contain only English words
//
// Property-based test (fast-check, >= 100 runs) for design Property 4
// (Requirement 1.1): after the key migration, every i18n_Key segment across the
// four namespaces (`common`, `pipeline`, `preferences`, `stix`) in BOTH locales
// (`pt-BR`, `en`) is composed only of English-language words.
//
// The test collects every fully-qualified leaf path via the shared helper
// (`catalog-utils.ts`), splits each path into its segments, and — over many
// generated segments — asserts that no segment matches a Portuguese-word
// dictionary. Interpolation placeholders (`{{...}}`) are value content, never
// key segments, so they never enter the segment stream and are excluded by
// construction.
//
// Intentionally-retained non-English LEAF segments are allow-listed: they are
// backend enum values or dynamic-consumer runtime leaves keyed by a runtime
// value (a `t(`prefix.${x}`)` lookup), NOT natural-language identifiers.
// Renaming them would break the dynamic lookup against the backend contract.
// Domain / English tokens such as `flow`, `stix`, `substages`, `buildOnline`
// are English identifiers and are likewise never rejected.
//
// This test does NOT add or modify any i18n keys; it only reads the locale JSON.
//
// Validates: Requirements 1.1

import { describe, it, expect } from "vitest"
import fc from "fast-check"

import ptCommon from "@/i18n/locales/pt-BR/common.json"
import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import ptPreferences from "@/i18n/locales/pt-BR/preferences.json"
import ptStix from "@/i18n/locales/pt-BR/stix.json"
import enCommon from "@/i18n/locales/en/common.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"
import enPreferences from "@/i18n/locales/en/preferences.json"
import enStix from "@/i18n/locales/en/stix.json"

import { collectLeafPaths, type JsonValue } from "./catalog-utils"

// Every namespace in both locales, so the property ranges over the ENTIRE key
// surface the migration touched (Req. 1.1 scopes all four namespaces).
const CATALOGS: ReadonlyArray<{
  readonly locale: "pt-BR" | "en"
  readonly namespace: string
  readonly resource: JsonValue
}> = [
  { locale: "pt-BR", namespace: "common", resource: ptCommon as JsonValue },
  { locale: "pt-BR", namespace: "pipeline", resource: ptPipeline as JsonValue },
  {
    locale: "pt-BR",
    namespace: "preferences",
    resource: ptPreferences as JsonValue,
  },
  { locale: "pt-BR", namespace: "stix", resource: ptStix as JsonValue },
  { locale: "en", namespace: "common", resource: enCommon as JsonValue },
  { locale: "en", namespace: "pipeline", resource: enPipeline as JsonValue },
  {
    locale: "en",
    namespace: "preferences",
    resource: enPreferences as JsonValue,
  },
  { locale: "en", namespace: "stix", resource: enStix as JsonValue },
]

/**
 * Intentionally-retained non-English LEAF segments.
 *
 * Each of these is a VALUE-CONTENT key: its segment text equals a backend enum
 * value or a dynamic-consumer runtime key, reached via a `t(`prefix.${x}`)`
 * lookup where `x` is driven by the backend contract or a protected `data-*`
 * attribute. They are documented as NOT-renamed in the rename maps and must
 * survive the Portuguese-word check so the dynamic lookups keep resolving.
 */
const ALLOWED_NON_ENGLISH_LEAVES: ReadonlySet<string> = new Set([
  // stix.flow.source.* — FlowOrigin / TranslationSource enum values.
  // (also parametrosAmbiente.origem.* -> environmentParameters.source.*)
  "curadoria_humana",
  "gerado_ia",
  // stage5 provisioning console step enum values (backend pipeline steps),
  // reached via `t(`...console.steps.${line.step}`)`.
  "subir_conteineres",
  "aguardar_saude",
  "encerrar_egresso",
  "reisolar",
  "verificar_egresso",
  "verificar_isolamento",
  "desprovisionar",
  "concluir",
  // stage5 provisioning console sub-step keys, reached via `${substageKey}`.
  "s51",
  "s52",
  // environmentParameters.platform.* — EnvironmentPlatform enum values.
  "maquina_virtual",
  "conteiner",
  // StageState enum values, reached via `t(`state.${state}`)`.
  "nao_iniciado",
  "em_andamento",
  "concluido",
  "erro",
  "bloqueado",
  // stage3.status.* — AbilityResultStatus enum values, reached via
  // `t(`stage3.status.${result.status}`)`; also prerequisites status keys.
  "pendente",
  "em_execucao",
  "sucesso",
  "falha",
  // emulation.error.* — RunOutcome-derived error leaves, reached via a dynamic
  // error-type lookup (`contencao_recusada` / `isolamento_falhou` /
  // `caldera_indisponivel` outcomes); documented NOT-renamed in
  // rename-map.pipeline.ts.
  "contencao",
  "isolamento",
  "indisponivel",
  // sections.* — reached via `t(`sections.${section}`)` where `section` is the
  // frontend `SECTION_KEYS` array AND the protected `data-section` attribute.
  "entrada",
  "processamento",
  "saida",
  // state.preConcluido — sibling of the StageState enum labels (state.*), a
  // value-content leaf retained with the rest of the `state` group.
  "preConcluido",
])

/**
 * Domain / English tokens explicitly treated as English even though a naive
 * dictionary might flag them (initialisms, compound camelCase, or terms that
 * collide with a Portuguese substring). Listed for documentation; the actual
 * acceptance is driven by their ABSENCE from the Portuguese dictionary below.
 */
const ALLOWED_DOMAIN_TOKENS: ReadonlySet<string> = new Set([
  "stix",
  "substages",
  "substeps",
  "buildOnline",
  // `aria` is the ARIA accessibility initialism (an English/domain token used in
  // segments like `nextAria`, `advanceAria`).
  "aria",
])

/**
 * Portuguese-word dictionary. A key segment is rejected when one of its
 * sub-words (after splitting a camelCase / snake_case segment into lowercase
 * sub-tokens) is a Portuguese word in this set. The dictionary is derived from
 * the pre-migration Portuguese key corpus (the `oldPath` leaves in the rename
 * maps) so it names the exact vocabulary the migration had to eliminate.
 */
const PORTUGUESE_WORDS: ReadonlySet<string> = new Set([
  "titulo",
  "subtitulo",
  "descricao",
  "carregando",
  "erro",
  "contagens",
  "contagem",
  "selecao",
  "campanha",
  "campanhas",
  "ator",
  "atores",
  "escolher",
  "baixada",
  "baixar",
  "baixando",
  "selecionar",
  "selecionando",
  "selecionada",
  "buscar",
  "busca",
  "limpar",
  "curada",
  "curadas",
  "curadoria",
  "resultados",
  "origem",
  "passa",
  "direto",
  "avancar",
  "voltar",
  "gerar",
  "gerado",
  "breve",
  "humana",
  "nome",
  "versao",
  "trocar",
  "pronto",
  "aguardando",
  "verificando",
  "verificar",
  "tecnica",
  "tecnicas",
  "atributos",
  "extraido",
  "concluido",
  "concluir",
  "falha",
  "etapa",
  "entrada",
  "saida",
  "processamento",
  "ambiente",
  "isolado",
  "diagrama",
  "redes",
  "atacante",
  "alvo",
  "topologia",
  "isolamento",
  "indisponivel",
  "bloqueado",
  "provisionamento",
  "vazio",
  "etapas",
  "subetapas",
  "fluxo",
  "comando",
  "prerequisitos",
  "conteineres",
  "saudaveis",
  "egresso",
  "perdido",
  "aprovado",
  "parametros",
  "plataforma",
  "conteiner",
  "maquina",
  "virtual",
  "endereco",
  "vulnerabilidades",
  "servicos",
  "relatorio",
  "veredito",
  "sucesso",
  "desprovisionado",
  "recursos",
  "remanescentes",
  "telemetria",
  "metricas",
  "nota",
  "cobertura",
  "tempo",
  "porcentagem",
  "segundos",
  "preparacao",
  "preparo",
  "progresso",
  "dica",
  "cartao",
  "pacote",
  "passo",
  "iteracoes",
  "comparacao",
  "estagio",
  "fala",
  "subir",
  "aguardar",
  "saude",
  "encerrar",
  "reisolar",
  "iniciado",
  "andamento",
  "execucao",
  "pendente",
  "contencao",
])

/**
 * Split a key segment into lowercase sub-tokens. A segment can be camelCase
 * (`noCampaigns`), snake_case (`curadoria_humana`), or carry a plural suffix
 * (`techniques_one`). Splitting into sub-words lets the Portuguese check inspect
 * each word of a compound identifier rather than requiring a whole-segment match.
 */
function splitSubTokens(segment: string): string[] {
  return segment
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_\s]+/g, " ")
    .trim()
    .split(" ")
    .filter((token) => token.length > 0)
    .map((token) => token.toLowerCase())
}

/**
 * Decide whether a single key segment is acceptable (English-only). A segment
 * passes when it is an allow-listed enum/runtime leaf or domain token, or when
 * none of its sub-tokens is a Portuguese dictionary word.
 */
function isEnglishSegment(segment: string): boolean {
  if (ALLOWED_NON_ENGLISH_LEAVES.has(segment)) {
    return true
  }
  if (ALLOWED_DOMAIN_TOKENS.has(segment)) {
    return true
  }
  return splitSubTokens(segment).every(
    (token) => !PORTUGUESE_WORDS.has(token),
  )
}

// Collect every (catalog, path, segment) triple once, so the property can range
// over the full population of leaf-path segments. `collectLeafPaths` yields
// fully-qualified dot-joined paths; we split each into its individual segments.
interface SegmentSample {
  readonly locale: string
  readonly namespace: string
  readonly path: string
  readonly segment: string
}

const ALL_SEGMENTS: SegmentSample[] = CATALOGS.flatMap(
  ({ locale, namespace, resource }) =>
    collectLeafPaths(resource).flatMap((path) =>
      path.split(".").map((segment) => ({
        locale,
        namespace,
        path,
        segment,
      })),
    ),
)

describe("Property 4: i18n key paths contain only English words", () => {
  it("sanity: the segment population is non-empty across all catalogs", () => {
    expect(ALL_SEGMENTS.length).toBeGreaterThan(0)
    // Every namespace in both locales contributed at least one segment.
    for (const { locale, namespace, resource } of CATALOGS) {
      const count = collectLeafPaths(resource).length
      expect(count, `${locale}/${namespace} has leaf paths`).toBeGreaterThan(0)
    }
  })

  // Feature: i18n-key-english-standardization, Property 4: Key paths contain only English words
  // Validates: Requirements 1.1
  it("every leaf-path segment across all four namespaces in both locales is an English word (or an allow-listed enum/domain leaf)", () => {
    fc.assert(
      fc.property(
        fc.constantFrom<SegmentSample>(...ALL_SEGMENTS),
        (sample) => {
          const ok = isEnglishSegment(sample.segment)
          expect(
            ok,
            `${sample.locale}/${sample.namespace}: segment "${sample.segment}" in path "${sample.path}" is a Portuguese word; rename it to English or allow-list it if it is an intentionally-retained enum/runtime leaf`,
          ).toBe(true)
        },
      ),
      { numRuns: 200 },
    )
  })

  it("exhaustive guard: no leaf-path segment anywhere is a Portuguese word", () => {
    const offenders = ALL_SEGMENTS.filter(
      (sample) => !isEnglishSegment(sample.segment),
    ).map((sample) => `${sample.locale}/${sample.namespace}:${sample.path}`)
    expect(offenders).toEqual([])
  })
})
