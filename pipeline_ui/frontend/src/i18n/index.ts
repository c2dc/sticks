import i18n from "i18next"
import { initReactI18next } from "react-i18next"

// Namespaces used across the app. pt-BR is always the fallback source, so every
// key that exists in `en` must also exist in pt-BR (Req. 8.3). Real strings for
// the pipeline/preferences namespaces are curated in tasks 12.x/13.1; this file
// owns the i18n wiring, fallback and language normalization (task 14.1).
import commonPtBR from "./locales/pt-BR/common.json"
import pipelinePtBR from "./locales/pt-BR/pipeline.json"
import preferencesPtBR from "./locales/pt-BR/preferences.json"
import commonEn from "./locales/en/common.json"
import pipelineEn from "./locales/en/pipeline.json"
import preferencesEn from "./locales/en/preferences.json"

export const DEFAULT_LANGUAGE = "pt-BR" as const
export const SUPPORTED_LANGUAGES = ["pt-BR", "en"] as const
export type SupportedLanguage = (typeof SUPPORTED_LANGUAGES)[number]

export const NAMESPACES = ["common", "pipeline", "preferences"] as const

/**
 * Normalize any persisted/requested language to a supported one, defaulting to
 * pt-BR for invalid values (Req. 8.6, 8.7). This is the single source of truth
 * for "what language do we actually use"; it is applied wherever a persisted,
 * stored or requested language is loaded. Property 12 (task 14.3) drives this.
 */
export function normalizeLanguage(value: string | null | undefined): SupportedLanguage {
  return SUPPORTED_LANGUAGES.includes(value as SupportedLanguage)
    ? (value as SupportedLanguage)
    : DEFAULT_LANGUAGE
}

export const resources = {
  "pt-BR": {
    common: commonPtBR,
    pipeline: pipelinePtBR,
    preferences: preferencesPtBR,
  },
  en: {
    common: commonEn,
    pipeline: pipelineEn,
    preferences: preferencesEn,
  },
} as const

void i18n.use(initReactI18next).init({
  resources,
  lng: DEFAULT_LANGUAGE,
  // Per-key fallback: when a key is missing in the selected language, i18next
  // resolves it from pt-BR (Req. 8.3). `fallbackLng` applies key-by-key, not
  // just when a whole language is missing.
  fallbackLng: DEFAULT_LANGUAGE,
  ns: NAMESPACES,
  defaultNS: "common",
  // Keep behavior predictable for the fallback property: an empty string is a
  // real (present) value and must NOT trigger fallback; only truly missing keys
  // fall back to pt-BR.
  returnEmptyString: true,
  interpolation: {
    escapeValue: false,
  },
})

/**
 * Resolve a translation key the same way the running UI does: return the value
 * from the selected language when present, otherwise the pt-BR value, otherwise
 * the key itself. This mirrors i18next's `fallbackLng` resolution and gives
 * Property 11 (task 14.2) a pure function to drive without a live component.
 *
 * `language` is normalized first, so an invalid language resolves against pt-BR.
 */
export function resolveWithFallback(
  key: string,
  language: string | null | undefined,
  options?: Record<string, unknown>,
): string {
  const lng = normalizeLanguage(language)
  return i18n.t(key, { lng, fallbackLng: DEFAULT_LANGUAGE, ...options })
}

/**
 * Apply a language to the running app without reloading the page (Req. 8.2).
 * The value is normalized first (Req. 8.7), so callers may pass raw persisted or
 * user-selected values. Returns the normalized language that was applied.
 *
 * react-i18next re-renders every component using `useTranslation` when the
 * language changes, so the switch takes effect in-place (well within 2s).
 */
export async function applyLanguage(
  value: string | null | undefined,
): Promise<SupportedLanguage> {
  const next = normalizeLanguage(value)
  if (i18n.language !== next) {
    await i18n.changeLanguage(next)
  }
  return next
}

export default i18n
