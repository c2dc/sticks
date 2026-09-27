import i18n from "i18next"
import { initReactI18next } from "react-i18next"

// Namespaces used across the app. Resource files start EMPTY ({}); real strings
// are added in later tasks (Req. 8, task 14). Fallback always resolves to pt-BR.
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
 * pt-BR for invalid values (Req. 8.7). Full property coverage lands in task 14.
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
  fallbackLng: DEFAULT_LANGUAGE,
  ns: NAMESPACES,
  defaultNS: "common",
  interpolation: {
    escapeValue: false,
  },
})

export default i18n
