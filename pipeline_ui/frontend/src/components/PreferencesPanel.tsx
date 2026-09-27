import { useTranslation } from "react-i18next"
import { Languages, Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useTheme, type Theme } from "@/components/theme-provider"
import { applyLanguage, normalizeLanguage } from "@/i18n"
import { persistIdioma } from "@/lib/preferencias"
import { cn } from "@/lib/utils"

/**
 * Preferences panel (Req. 7). Offers Modo_Claro (default, Req. 7.6) and
 * Modo_Escuro as two mutually-exclusive themes (Req. 7.1). Selecting a theme
 * applies it to every visible element instantly, with no reload and no
 * intermediate loading state (Req. 7.2) — the class-based ThemeProvider swaps
 * the `dark` class synchronously and the whole tree re-styles from the CSS
 * variables. Persistence of the chosen theme (Req. 7.4) and restoration of the
 * last persisted theme (Req. 7.5) are handled by the ThemeProvider owner (App),
 * which wires `onThemeChange` to `PUT /api/preferencias`.
 *
 * The blue/red/purple team palette (Req. 7.3) is documented and previewed via
 * the team tokens (`bg-team-blue`/`red`/`purple`), which resolve from CSS
 * variables defined for BOTH themes in `index.css`.
 *
 * All user-facing strings come from the `preferences` i18n namespace.
 */
export function PreferencesPanel() {
  const { theme, setTheme } = useTheme()
  const { t, i18n } = useTranslation("preferences")

  const currentLanguage = normalizeLanguage(i18n.language)

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <ThemeSelector theme={theme} onSelect={setTheme} t={t} />
        <PaletteLegend t={t} />
        <LanguageToggle
          currentLanguage={currentLanguage}
          onToggle={() => {
            // Switch in-place without reload (Req. 8.2) and persist the choice
            // (Req. 8.4). Persistence failures are non-fatal — the UI already
            // switched and the write can be retried later.
            const next = currentLanguage === "pt-BR" ? "en" : "pt-BR"
            void applyLanguage(next)
            void persistIdioma(next).catch(() => {})
          }}
          t={t}
        />
      </CardContent>
    </Card>
  )
}

type Translate = ReturnType<typeof useTranslation>["t"]

/**
 * Mutually-exclusive theme selection (Req. 7.1). Rendered as a radio group so
 * the two options are clearly exclusive and accessible; picking one applies it
 * instantly (Req. 7.2).
 */
function ThemeSelector({
  theme,
  onSelect,
  t,
}: {
  theme: Theme
  onSelect: (theme: Theme) => void
  t: Translate
}) {
  const options: { value: Theme; label: string; hint: string; icon: typeof Sun }[] = [
    { value: "claro", label: t("theme.light"), hint: t("theme.lightHint"), icon: Sun },
    { value: "escuro", label: t("theme.dark"), hint: t("theme.darkHint"), icon: Moon },
  ]

  return (
    <div className="flex flex-col gap-2" role="radiogroup" aria-label={t("theme.label")}>
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("theme.label")}
      </p>
      <div className="grid grid-cols-2 gap-2">
        {options.map(({ value, label, hint, icon: Icon }) => {
          const selected = theme === value
          return (
            <Button
              key={value}
              type="button"
              variant={selected ? "default" : "outline"}
              role="radio"
              aria-checked={selected}
              title={hint}
              onClick={() => onSelect(value)}
              className="h-auto flex-col items-start gap-1 py-2"
            >
              <span className="flex items-center gap-2">
                <Icon className="size-4" aria-hidden />
                {label}
              </span>
            </Button>
          )
        })}
      </div>
    </div>
  )
}

/**
 * Blue/red/purple team palette legend (Req. 7.3). Uses the team tokens directly
 * so the same swatches render sensibly in both Modo_Claro and Modo_Escuro.
 */
function PaletteLegend({ t }: { t: Translate }) {
  const swatches: { key: string; className: string; label: string }[] = [
    { key: "blue", className: "bg-team-blue", label: t("palette.blue") },
    { key: "red", className: "bg-team-red", label: t("palette.red") },
    { key: "purple", className: "bg-team-purple", label: t("palette.purple") },
  ]

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("palette.label")}
      </p>
      <ul className="flex flex-col gap-1.5">
        {swatches.map(({ key, className, label }) => (
          <li key={key} className="flex items-center gap-2 text-sm">
            <span
              className={cn("size-3.5 shrink-0 rounded-full", className)}
              aria-hidden
            />
            <span className="text-muted-foreground">{label}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * Language toggle. Full i18n behavior (persistence, fallback, normalization) is
 * owned by task 14; this keeps an instant in-app language switch wired to the
 * i18n instance and localized via the preferences namespace.
 */
function LanguageToggle({
  currentLanguage,
  onToggle,
  t,
}: {
  currentLanguage: "pt-BR" | "en"
  onToggle: () => void
  t: Translate
}) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("language.label")}
      </p>
      <Button variant="outline" onClick={onToggle}>
        <Languages />
        {currentLanguage === "pt-BR" ? t("language.en") : t("language.ptBR")}
      </Button>
    </div>
  )
}
