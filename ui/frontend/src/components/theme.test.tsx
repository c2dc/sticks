import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import i18n from "@/i18n"
import { ThemeProvider } from "@/components/theme-provider"
import { NavBar } from "@/components/NavBar"

// Mock the preferences persistence wrappers so no backend is needed. The theme
// tests only exercise the in-app theme switch / restore behavior; persistence is
// stubbed here to a no-op resolved value.
vi.mock("@/lib/preferencias", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/preferencias")>()
  return {
    ...actual,
    getPreferencias: vi.fn().mockResolvedValue(null),
    putPreferencias: vi.fn().mockResolvedValue({ tema: "claro", idioma: "pt-BR" }),
    persistIdioma: vi.fn().mockResolvedValue({ tema: "claro", idioma: "pt-BR" }),
  }
})

/** True when the app-wide dark theme is active (tailwind darkMode: "class"). */
function isDark() {
  return document.documentElement.classList.contains("dark")
}

/** The navbar theme toggle button, found by its stable (language-agnostic key) aria-label. */
function themeToggle() {
  return screen.getByRole("button", { name: i18n.t("navbar.toggleTheme", { ns: "preferences" }) })
}

describe("Frontend theme via NavBar (Req. 7.2, 7.5, 7.6)", () => {
  beforeEach(async () => {
    // Reset the shared <html> class and language between tests so each assertion
    // starts from a known state (jsdom keeps document.documentElement across renders).
    document.documentElement.classList.remove("dark")
    await i18n.changeLanguage("pt-BR")
  })

  it("switches to Modo_Escuro and back without reloading (Req. 7.2)", async () => {
    const user = userEvent.setup()

    renderWithI18n(
      <ThemeProvider>
        <NavBar />
      </ThemeProvider>,
    )

    // Default is Modo_Claro: no dark class applied on mount.
    expect(isDark()).toBe(false)

    // Clicking the toggle flips the dark class in place — applied synchronously
    // by the class-based provider (no reload).
    await user.click(themeToggle())
    expect(isDark()).toBe(true)

    // Clicking again flips it back — again with no reload.
    await user.click(themeToggle())
    expect(isDark()).toBe(false)
  })

  it("restores the last persisted theme on mount (Req. 7.5)", () => {
    // A restored preference of Modo_Escuro is passed via the `theme` prop; the
    // provider applies it on mount without any user action.
    renderWithI18n(
      <ThemeProvider theme="escuro">
        <NavBar />
      </ThemeProvider>,
    )

    expect(isDark()).toBe(true)
  })

  it("defaults to Modo_Claro with no persisted preference (Req. 7.6)", () => {
    // No `theme` prop => the default (Modo_Claro) applies: dark class absent.
    renderWithI18n(
      <ThemeProvider>
        <NavBar />
      </ThemeProvider>,
    )

    expect(isDark()).toBe(false)
  })
})
