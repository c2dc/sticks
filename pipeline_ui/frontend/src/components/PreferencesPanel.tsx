import { useTranslation } from "react-i18next"
import { Languages, Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useTheme } from "@/components/theme-provider"
import { normalizeLanguage } from "@/i18n"

/**
 * Placeholder preferences panel (Req. 7, 8). Wires theme toggle and language
 * switch to the scaffold providers. Full persistence + palette work lands in
 * tasks 13 and 14; strings are intentionally hard-coded fallbacks for now
 * because the i18n resource files start empty.
 */
export function PreferencesPanel() {
  const { theme, toggleTheme } = useTheme()
  const { i18n } = useTranslation()

  const currentLanguage = normalizeLanguage(i18n.language)

  return (
    <Card>
      <CardHeader>
        <CardTitle>Preferências</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Button variant="outline" onClick={toggleTheme}>
          {theme === "claro" ? <Moon /> : <Sun />}
          {theme === "claro" ? "Modo Escuro" : "Modo Claro"}
        </Button>
        <Button
          variant="outline"
          onClick={() =>
            void i18n.changeLanguage(currentLanguage === "pt-BR" ? "en" : "pt-BR")
          }
        >
          <Languages />
          {currentLanguage === "pt-BR" ? "English" : "Português (pt-BR)"}
        </Button>
      </CardContent>
    </Card>
  )
}
