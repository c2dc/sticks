import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import { useTheme } from "@/components/theme-provider"
import { cn } from "@/lib/utils"

/**
 * Tela inicial (Landing) do STICKS — ponto de entrada do fluxo guiado
 * (Req. 1.1–1.5).
 *
 * Exibe a marca STICKS e um convite claro para iniciar a jornada de emulação de
 * campanha de adversário. O único controle de ação (CTA) dispara `onIniciar`,
 * que o `App.tsx` liga ao evento `INICIAR` da máquina de estados do fluxo
 * (task 17.2) — a Landing em si não conhece o reducer.
 *
 * As animações de entrada têm duração limitada a {@link LANDING_TRANSITION_MS}
 * (≤ 600 ms, Req. 1.4) e são completamente desligadas quando o usuário tem a
 * preferência de redução de movimento do sistema habilitada (Req. 1.5), via
 * {@link usePrefersReducedMotion}.
 */

/**
 * Teto único de duração para toda animação/transição da Landing, em
 * milissegundos. Mantido ≤ 600 ms (Req. 1.4) e usado tanto para a classe
 * utilitária do Tailwind quanto para o `style.animationDuration` inline, de
 * modo que exista uma só fonte da verdade.
 */
export const LANDING_TRANSITION_MS = 600

/**
 * Hook que informa se o usuário prefere movimento reduzido no sistema
 * operacional, observando `prefers-reduced-motion: reduce` via `matchMedia`.
 *
 * É seguro em SSR / ambientes sem `window` (retorna `false`) e reage a mudanças
 * da preferência enquanto a tela está montada.
 */
export function usePrefersReducedMotion(): boolean {
  const [reduzido, setReduzido] = useState<boolean>(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return false
    }
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches
  })

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return
    }
    const media = window.matchMedia("(prefers-reduced-motion: reduce)")
    const atualizar = () => setReduzido(media.matches)
    atualizar()
    media.addEventListener("change", atualizar)
    return () => media.removeEventListener("change", atualizar)
  }, [])

  return reduzido
}

export interface LandingProps {
  /** Dispara o início do fluxo (o `App.tsx` o liga ao evento `INICIAR`). */
  onIniciar: () => void
}

export function Landing({ onIniciar }: LandingProps) {
  const { t } = useTranslation("stix")
  const prefereMovimentoReduzido = usePrefersReducedMotion()

  // Com movimento reduzido não aplicamos nenhuma classe de animação; o conteúdo
  // é renderizado estaticamente (Req. 1.5).
  const anim = (classes: string) => (prefereMovimentoReduzido ? "" : classes)
  const duracao = prefereMovimentoReduzido
    ? undefined
    : { animationDuration: `${LANDING_TRANSITION_MS}ms` }

  return (
    <main className="flex min-h-[70vh] flex-col items-center justify-center px-4 text-center">
      <div
        className={cn(
          "flex max-w-2xl flex-col items-center gap-6",
          anim("animate-in fade-in slide-in-from-bottom-4 fill-mode-both"),
        )}
        style={duracao}
      >
        <LandingWordmark />

        <h1 className="text-balance text-4xl font-bold tracking-tight text-foreground sm:text-5xl">
          {t("landing.titulo")}
        </h1>

        <p className="text-balance text-lg text-muted-foreground sm:text-xl">
          {t("landing.subtitulo")}
        </p>

        <Button
          type="button"
          size="lg"
          onClick={onIniciar}
          className={cn(
            "mt-2 px-10 text-base",
            anim("animate-in fade-in fill-mode-both"),
          )}
          style={duracao}
        >
          {t("landing.cta")}
        </Button>
      </div>
    </main>
  )
}

/**
 * Wordmark da Landing, adaptada ao tema (fundo claro × fundo escuro) seguindo o
 * mesmo padrão do `NavBar`: escolhe a arte conforme o tema e, se a imagem
 * falhar ao carregar, cai para a marca textual "STICKS" (grande), de modo que a
 * identidade nunca desapareça.
 */
function LandingWordmark() {
  const { theme } = useTheme()
  const [imgOk, setImgOk] = useState(true)

  const wordmarkSrc =
    theme === "escuro"
      ? "/sticks-logomarca-modo-escuro.png"
      : "/sticks-logomarca-modo-claro.png"

  if (!imgOk) {
    return (
      <span className="text-6xl font-black tracking-tight text-foreground sm:text-7xl">
        STICKS
      </span>
    )
  }

  return (
    <img
      src={wordmarkSrc}
      alt="STICKS"
      className="h-20 w-auto sm:h-24"
      onError={() => setImgOk(false)}
    />
  )
}
