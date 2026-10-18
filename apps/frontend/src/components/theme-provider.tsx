import * as React from "react"

/**
 * Class-based theme provider driving Modo_Claro / Modo_Escuro (Req. 7).
 * Toggles the `dark` class on <html>, matching tailwind darkMode: "class".
 *
 * Switching is instant: the class is applied synchronously via a layout effect,
 * so the whole tree re-styles from the CSS variables without reloading the page
 * and without any intermediate loading state (Req. 7.2). The default theme is
 * Modo_Claro (Req. 7.6).
 *
 * Persistence is delegated to the caller through `onThemeChange`: whenever the
 * theme changes as a result of a user action, the provider invokes it so the
 * caller can persist via `PUT /api/preferencias` (Req. 7.4). The `theme` prop
 * lets the caller restore the last persisted theme on open (Req. 7.5).
 */

export type Theme = "claro" | "escuro"

interface ThemeContextValue {
  theme: Theme
  setTheme: (theme: Theme) => void
  toggleTheme: () => void
}

const ThemeContext = React.createContext<ThemeContextValue | undefined>(undefined)

export function ThemeProvider({
  children,
  defaultTheme = "claro",
  theme: controlledTheme,
  onThemeChange,
}: {
  children: React.ReactNode
  defaultTheme?: Theme
  /**
   * Restored theme to apply (e.g. from persisted preferences). When provided,
   * it seeds and keeps the internal theme in sync (Req. 7.5).
   */
  theme?: Theme
  /** Called when the user changes the theme, so the caller can persist it. */
  onThemeChange?: (theme: Theme) => void
}) {
  const [theme, setThemeState] = React.useState<Theme>(controlledTheme ?? defaultTheme)

  // Keep the internal theme in sync with a restored/controlled value so the
  // last persisted theme is applied on open without a user action (Req. 7.5).
  React.useEffect(() => {
    if (controlledTheme && controlledTheme !== theme) {
      setThemeState(controlledTheme)
    }
    // Only react to changes of the controlled value itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [controlledTheme])

  // Apply the theme synchronously before paint so the switch is instant and has
  // no intermediate loading state (Req. 7.2).
  React.useLayoutEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "escuro")
  }, [theme])

  const setTheme = React.useCallback(
    (next: Theme) => {
      setThemeState((current) => {
        if (next !== current) onThemeChange?.(next)
        return next
      })
    },
    [onThemeChange],
  )

  const value = React.useMemo<ThemeContextValue>(
    () => ({
      theme,
      setTheme,
      toggleTheme: () => setTheme(theme === "claro" ? "escuro" : "claro"),
    }),
    [theme, setTheme],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const context = React.useContext(ThemeContext)
  if (!context) {
    throw new Error("useTheme must be used within a ThemeProvider")
  }
  return context
}
