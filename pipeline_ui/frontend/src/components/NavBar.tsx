import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"
import { FlagBR, FlagUS } from "@/components/FlagIcon"
import { useTheme } from "@/components/theme-provider"
import { applyLanguage, normalizeLanguage } from "@/i18n"
import { persistIdioma } from "@/lib/preferencias"

/**
 * Barra de navegação superior (topo) do site.
 *
 * Contém a logomarca do projeto STICKS e os dois seletores globais:
 *  - Tema: botão com ícone de Sol (Modo Claro) / Lua (Modo Escuro).
 *  - Idioma: botão com bandeira SVG do Brasil (pt-BR) / EUA (en).
 *
 * O carregamento do padrão do usuário (tema + idioma persistidos) acontece no
 * `App.tsx` na abertura; aqui apenas expomos as alternâncias, que aplicam a
 * mudança instantaneamente e persistem a escolha via `PUT /api/preferencias`.
 */
export function NavBar() {
  const { t } = useTranslation("preferences")
  const { theme, toggleTheme } = useTheme()
  const { i18n } = useTranslation()

  const isDark = theme === "escuro"
  const currentLanguage = normalizeLanguage(i18n.language)

  const handleLanguageToggle = () => {
    const next = currentLanguage === "pt-BR" ? "en" : "pt-BR"
    void applyLanguage(next)
    void persistIdioma(next).catch(() => {})
  }

  return (
    <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <nav className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4">
        <Logo />

        <div className="flex items-center gap-1.5">
          {/* Seletor de tema: Sol ↔ Lua */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={toggleTheme}
            aria-label={t("navbar.toggleTheme")}
            title={isDark ? t("navbar.toThemeLight") : t("navbar.toThemeDark")}
          >
            {isDark ? (
              <Sun className="size-5" aria-hidden />
            ) : (
              <Moon className="size-5" aria-hidden />
            )}
          </Button>

          {/* Seletor de idioma: bandeira SVG do Brasil ↔ EUA. Mostra a bandeira
              do idioma ATUAL; clicar alterna para o outro. */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={handleLanguageToggle}
            aria-label={t("navbar.toggleLanguage")}
            title={
              currentLanguage === "pt-BR"
                ? t("navbar.toLanguageEn")
                : t("navbar.toLanguagePtBR")
            }
          >
            {currentLanguage === "pt-BR" ? (
              <FlagBR className="h-5 w-7 rounded-sm shadow-sm" />
            ) : (
              <FlagUS className="h-5 w-7 rounded-sm shadow-sm" />
            )}
          </Button>
        </div>
      </nav>
    </header>
  )
}

/**
 * Logomarca do projeto STICKS, responsiva:
 *
 *  - Em telas pequenas (mobile), mostra apenas o ícone/símbolo quadrado, que
 *    ocupa pouco espaço horizontal.
 *  - A partir de `sm`, mostra a logomarca completa (símbolo + wordmark),
 *    escolhendo a versão adequada ao tema (fundo claro x fundo escuro).
 *
 * Os arquivos ficam em `frontend/public/`. Se as imagens falharem ao carregar,
 * cai para uma marca textual para o site nunca ficar sem identidade.
 */
function Logo() {
  const { theme } = useTheme()
  const [imgOk, setImgOk] = useState(true)

  const wordmarkSrc =
    theme === "escuro"
      ? "/sticks-logomarca-modo-escuro.png"
      : "/sticks-logomarca-modo-claro.png"

  if (!imgOk) {
    return (
      <a href="/" className="flex items-center gap-2 font-semibold">
        <span className="text-lg font-bold tracking-tight">STICKS</span>
        <span className="hidden text-sm text-muted-foreground sm:inline">
          Pipeline UI
        </span>
      </a>
    )
  }

  return (
    <a href="/" className="flex items-center font-semibold" aria-label="STICKS">
      {/* Mobile: apenas o símbolo (fundo transparente, serve nos dois temas) */}
      <img
        src="/sticks-logo-mais-fina.png"
        alt="STICKS"
        className="h-11 w-11 sm:hidden"
        onError={() => setImgOk(false)}
      />
      {/* Desktop (>= sm): logomarca completa, adaptada ao tema */}
      <img
        src={wordmarkSrc}
        alt="STICKS"
        className="hidden h-12 w-auto sm:block"
        onError={() => setImgOk(false)}
      />
    </a>
  )
}
