import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import { useTheme } from "@/components/theme-provider"
import { cn } from "@/lib/utils"

/**
 * STICKS Landing screen — entry point of the guided flow (Req. 1.1–1.5).
 *
 * Shows the STICKS brand and a clear invitation to start the adversary-campaign
 * emulation journey. The single action control (CTA) fires `onStart`, which
 * `App.tsx` wires to the flow state-machine's `START` event (task 17.2) — the
 * Landing itself does not know the reducer.
 *
 * The entrance animations are capped at {@link LANDING_TRANSITION_MS}
 * (≤ 600 ms, Req. 1.4) and are fully disabled when the user has the system's
 * reduced-motion preference enabled (Req. 1.5), via
 * {@link usePrefersReducedMotion}.
 */

/**
 * Single duration cap for every Landing animation/transition, in milliseconds.
 * Kept ≤ 600 ms (Req. 1.4) and used both for the Tailwind utility class and the
 * inline `style.animationDuration`, so there is a single source of truth.
 */
export const LANDING_TRANSITION_MS = 600

/**
 * Hook that reports whether the user prefers reduced motion at the OS level,
 * observing `prefers-reduced-motion: reduce` via `matchMedia`.
 *
 * It is safe under SSR / environments without `window` (returns `false`) and
 * reacts to preference changes while the screen is mounted.
 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
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
    const update = () => setReduced(media.matches)
    update()
    media.addEventListener("change", update)
    return () => media.removeEventListener("change", update)
  }, [])

  return reduced
}

export interface LandingProps {
  /** Fires the flow start (`App.tsx` wires it to the `START` event). */
  onStart: () => void
}

export function Landing({ onStart }: LandingProps) {
  const { t } = useTranslation("stix")
  const prefersReducedMotion = usePrefersReducedMotion()

  // With reduced motion we apply no animation class; the content is rendered
  // statically (Req. 1.5).
  const anim = (classes: string) => (prefersReducedMotion ? "" : classes)
  const duration = prefersReducedMotion
    ? undefined
    : { animationDuration: `${LANDING_TRANSITION_MS}ms` }

  return (
    <main className="flex min-h-[70vh] flex-col items-center justify-center px-4 text-center">
      <div
        className={cn(
          "flex max-w-2xl flex-col items-center gap-6",
          anim("animate-in fade-in slide-in-from-bottom-4 fill-mode-both"),
        )}
        style={duration}
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
          onClick={onStart}
          className={cn(
            "mt-2 px-10 text-base",
            anim("animate-in fade-in fill-mode-both"),
          )}
          style={duration}
        >
          {t("landing.cta")}
        </Button>
      </div>
    </main>
  )
}

/**
 * The Landing wordmark, adapted to the theme (light × dark background) following
 * the same pattern as `NavBar`: it picks the artwork per theme and, if the image
 * fails to load, falls back to the textual "STICKS" mark (large), so the
 * identity never disappears.
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
