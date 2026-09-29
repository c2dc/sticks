/**
 * Local source of truth for the theme (Modo_Claro / Modo_Escuro).
 *
 * The theme must survive a page reload instantly and, on first visit, respect
 * the operating system preference (`prefers-color-scheme`). Relying on a network
 * round-trip to the backend for this is fragile: the request can fail or lag,
 * which is what caused the theme to fall back to Modo_Claro on every refresh.
 *
 * So the resolution order for the *initial* theme is:
 *   1. A value the user explicitly chose before (persisted in localStorage).
 *   2. Otherwise, the OS preference via `prefers-color-scheme: dark`.
 *   3. Otherwise, Modo_Claro (the product default, Req. 7.6).
 *
 * The backend (`PUT /api/preferencias`) stays as secondary, cross-machine
 * persistence — but it is no longer what the first paint depends on.
 */
import type { Theme } from "@/components/theme-provider"

/** localStorage key holding the user's explicit theme choice, when any. */
export const THEME_STORAGE_KEY = "sticks.tema"

/** True when the OS currently reports a dark color-scheme preference. */
export function systemPrefersDark(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  )
}

/** Read the user's explicitly stored theme, or `null` if none was stored. */
export function readStoredTheme(): Theme | null {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY)
    return value === "claro" || value === "escuro" ? value : null
  } catch {
    return null
  }
}

/** Persist the user's explicit theme choice locally (best-effort). */
export function writeStoredTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // Storage may be unavailable (private mode, quota). The in-memory theme
    // still applies for this session; we just cannot remember it locally.
  }
}

/**
 * Resolve the theme to use at startup: an explicit local choice wins; otherwise
 * follow the OS; otherwise Modo_Claro.
 */
export function resolveInitialTheme(): Theme {
  return readStoredTheme() ?? (systemPrefersDark() ? "escuro" : "claro")
}
