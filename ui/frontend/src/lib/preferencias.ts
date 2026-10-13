/**
 * Typed API wrappers for user preferences (theme + language) persistence
 * (Req. 7.4, 7.5, 8.4, 8.5).
 *
 * Persistence is write-through `PUT /api/preferencias` (body `{ tema?, idioma? }`),
 * which returns the persisted `{ tema, idioma }`. On open we restore the last
 * persisted preferences by reading the session state via `GET /api/estado-sessao`,
 * which carries the preferences block. When no preference is persisted yet
 * (first visit), the theme defaults to Modo_Claro (Req. 7.6) and the language to
 * pt-BR (Req. 8.6) — those defaults live in the callers/normalizers, not here.
 *
 * The backend contracts are defined in the pipeline-ui design (Endpoints REST,
 * in pt-BR): `GET /api/estado-sessao` and `PUT /api/preferencias`.
 */
import { api, ApiError } from "@/lib/api"

/** Persisted theme value (matches backend `Theme` enum: "claro" | "escuro"). */
export type Tema = "claro" | "escuro"

/** Persisted language value (matches backend `Language` enum: "pt-BR" | "en"). */
export type Idioma = "pt-BR" | "en"

/** The persisted preferences block (`PUT /api/preferencias` response). */
export interface Preferencias {
  tema: Tema
  idioma: Idioma
}

/** Partial update accepted by `PUT /api/preferencias` (body `{ tema?, idioma? }`). */
export type PreferenciasUpdate = Partial<Preferencias>

/**
 * Session state (`GET /api/estado-sessao`). Only the `preferencias` block is
 * consumed here; the rest of the session state (current case, per-case stages,
 * operation results) is handled elsewhere (Req. 9). `preferencias` may be absent
 * on first visit, in which case there is nothing persisted to restore.
 */
export interface EstadoSessao {
  preferencias?: Preferencias | null
}

/**
 * Read the persisted preferences from the session state (Req. 7.5, 8.5).
 * Returns `null` when no preferences have been persisted yet (first visit),
 * so callers can fall back to the defaults (Modo_Claro / pt-BR).
 */
export async function getPreferencias(): Promise<Preferencias | null> {
  const estado = await api.get<EstadoSessao>("/estado-sessao")
  return estado.preferencias ?? null
}

/**
 * Persist the theme and/or language preference (Req. 7.4, 8.4) via
 * `PUT /api/preferencias`. Returns the persisted `{ tema, idioma }`, which the
 * caller treats as the source of truth.
 */
export function putPreferencias(update: PreferenciasUpdate): Promise<Preferencias> {
  return api.put<Preferencias>("/preferencias", update)
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
 * invalid value is never stored, then persists via `putPreferencias`.
 */
export function persistIdioma(idioma: string | null | undefined): Promise<Preferencias> {
  return putPreferencias({ idioma: normalizeLanguage(idioma) })
}

/**
 * Restore the last persisted language on open (Req. 8.5). With no persisted
 * preference (first visit) it defaults to pt-BR (Req. 8.6); an invalid persisted
 * value is normalized to pt-BR (Req. 8.7). Reading never throws — a failure
 * yields the pt-BR default so the app still opens.
 */
export async function restaurarIdioma(): Promise<SupportedLanguage> {
  try {
    const prefs = await getPreferencias()
    return normalizeLanguage(prefs?.idioma)
  } catch {
    return DEFAULT_LANGUAGE
  }
}
