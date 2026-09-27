import * as React from "react"

/**
 * Class-based theme provider driving Modo_Claro / Modo_Escuro (Req. 7).
 * Toggles the `dark` class on <html>, matching tailwind darkMode: "class".
 * Persistence via the backend (PUT /api/preferencias) is wired in task 13;
 * this scaffold keeps the default Modo_Claro (Req. 7.6).
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
}: {
  children: React.ReactNode
  defaultTheme?: Theme
}) {
  const [theme, setTheme] = React.useState<Theme>(defaultTheme)

  React.useEffect(() => {
    const root = document.documentElement
    root.classList.toggle("dark", theme === "escuro")
  }, [theme])

  const value = React.useMemo<ThemeContextValue>(
    () => ({
      theme,
      setTheme,
      toggleTheme: () => setTheme((t) => (t === "claro" ? "escuro" : "claro")),
    }),
    [theme],
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
