import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ThemeProvider } from "@/components/theme-provider"
import { PreferencesPanel } from "@/components/PreferencesPanel"

// Mock the preferences persistence wrappers so no backend is needed. The theme
// tests only exercise the in-app theme switch / restore behavior; persistence is
// covered by task 13.1 wiring and is stubbed here to a no-op resolved value.
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

describe("Frontend theme (Req. 7.2, 7.3, 7.5, 7.6)", () => {
  beforeEach(() => {
    // Reset the shared <html> class between tests so each assertion starts from
    // a known state (jsdom keeps document.documentElement across renders).
    document.documentElement.classList.remove("dark")
  })

  it("switches to Modo_Escuro and back without reloading (Req. 7.2)", async () => {
    const user = userEvent.setup()

    renderWithI18n(
      <ThemeProvider>
        <PreferencesPanel />
      </ThemeProvider>,
    )

    // Default is Modo_Claro: no dark class applied on mount.
    expect(isDark()).toBe(false)

    // The theme options render as an exclusive radio group.
    const escuro = screen.getByRole("radio", { name: /escuro/i })
    const claro = screen.getByRole("radio", { name: /claro/i })

    // Selecting Modo_Escuro flips the dark class in place — the switch is
    // applied synchronously by the class-based provider (no reload).
    await user.click(escuro)
    expect(isDark()).toBe(true)
    expect(escuro).toHaveAttribute("aria-checked", "true")
    expect(claro).toHaveAttribute("aria-checked", "false")

    // Selecting Modo_Claro flips it back — again with no reload.
    await user.click(claro)
    expect(isDark()).toBe(false)
    expect(claro).toHaveAttribute("aria-checked", "true")
    expect(escuro).toHaveAttribute("aria-checked", "false")
  })

  it("renders the blue/red/purple team palette in the contexts (Req. 7.3)", () => {
    const { container } = renderWithI18n(
      <ThemeProvider>
        <PreferencesPanel />
      </ThemeProvider>,
    )

    // The palette legend previews the three team swatches, one per context:
    // blue (defensive), red (offensive), purple (combined).
    expect(container.querySelector(".bg-team-blue")).not.toBeNull()
    expect(container.querySelector(".bg-team-red")).not.toBeNull()
    expect(container.querySelector(".bg-team-purple")).not.toBeNull()
  })

  it("restores the last persisted theme on mount (Req. 7.5)", () => {
    // A restored preference of Modo_Escuro is passed via the `theme` prop; the
    // provider applies it on mount without any user action.
    renderWithI18n(
      <ThemeProvider theme="escuro">
        <PreferencesPanel />
      </ThemeProvider>,
    )

    expect(isDark()).toBe(true)
    expect(screen.getByRole("radio", { name: /escuro/i })).toHaveAttribute(
      "aria-checked",
      "true",
    )
  })

  it("defaults to Modo_Claro with no persisted preference (Req. 7.6)", () => {
    // No `theme` prop => the default (Modo_Claro) applies: dark class absent.
    renderWithI18n(
      <ThemeProvider>
        <PreferencesPanel />
      </ThemeProvider>,
    )

    expect(isDark()).toBe(false)
    expect(screen.getByRole("radio", { name: /claro/i })).toHaveAttribute(
      "aria-checked",
      "true",
    )
  })
})
