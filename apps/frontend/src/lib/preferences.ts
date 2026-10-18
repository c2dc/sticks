/**
 * Typed API wrappers for user preferences (theme + language) persistence
 * (Req. 7.4, 7.5, 8.4, 8.5).
 *
 * Persistence is write-through `PUT /api/preferences` (body `{ theme?, language? }`),
 * which returns the persisted `{ theme, language }`. On open we restore the last
 * persisted preferences by reading the session state via `GET /api/session-state`,
 * which carries the preferences block. When no preference is persisted yet
 * (first visit), the theme defaults to light (Req. 7.6) and the language to
 * pt-BR (Req. 8.6) — those defaults live in the callers/normalizers, not here.
 *
 * The backend contracts are defined in the pipeline-ui design (REST endpoints):
 * `GET /api/session-state` and `PUT /api/preferences`.
 */
import { api, ApiError } from "@/lib/api"

/** Persisted theme value (matches backend `Theme` enum: "claro" | "escuro"). */
export type Theme = "claro" | "escuro"

/** Persisted language value (matches backend `Language` enum: "pt-BR" | "en"). */
export type Language = "pt-BR" | "en"

/** The persisted preferences block (`PUT /api/preferences` response). */
export interface Preferences {
  theme: Theme
  language: Language
}

/** Partial update accepted by `PUT /api/preferences` (body `{ theme?, language? }`). */
export type PreferencesUpdate = Partial<Preferences>

/**
 * Session state (`GET /api/session-state`). Only the `preferences` block is
 * consumed here; the rest of the session state (current campaign, per-campaign
 * stages, operation results) is handled elsewhere (Req. 9). `preferences` may be
 * absent on first visit, in which case there is nothing persisted to restore.
 */
export interface SessionState {
  preferences?: Preferences | null
}

/**
 * Read the persisted preferences from the session state (Req. 7.5, 8.5).
 * Returns `null` when no preferences have been persisted yet (first visit),
 * so callers can fall back to the defaults (light / pt-BR).
 */
export async function getPreferences(): Promise<Preferences | null> {
  const state = await api.get<SessionState>("/session-state")
  return state.preferences ?? null
}

/**
 * Persist the theme and/or language preference (Req. 7.4, 8.4) via
 * `PUT /api/preferences`. Returns the persisted `{ theme, language }`, which the
 * caller treats as the source of truth.
 */
export function putPreferences(update: PreferencesUpdate): Promise<Preferences> {
  return api.put<Preferences>("/preferences", update)
}

/** Re-export so callers can narrow persistence failures without importing api. */
export { ApiError }

/* --- i18n language helpers (task 14.1) ------------------------------------
 * Thin language-focused wrappers over the preferences persistence above,
 * normalizing through the i18n single source of truth. Kept additive so the
 * theme-owned exports (task 13.1) stay untouched.
 */
import { DEFAULT_LANGUAGE, normalizeLanguage, type SupportedLanguage } from "@/i18n"

/**
 * Persist the language preference (Req. 8.4). Normalizes before sending so an
 * invalid value is never stored, then persists via `putPreferences`.
 */
export function persistLanguage(language: string | null | undefined): Promise<Preferences> {
  return putPreferences({ language: normalizeLanguage(language) })
}

/**
 * Restore the last persisted language on open (Req. 8.5). With no persisted
 * preference (first visit) it defaults to pt-BR (Req. 8.6); an invalid persisted
 * value is normalized to pt-BR (Req. 8.7). Reading never throws — a failure
 * yields the pt-BR default so the app still opens.
 */
export async function restoreLanguage(): Promise<SupportedLanguage> {
  try {
    const prefs = await getPreferences()
    return normalizeLanguage(prefs?.language)
  } catch {
    return DEFAULT_LANGUAGE
  }
}
