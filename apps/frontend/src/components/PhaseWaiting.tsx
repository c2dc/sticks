import { Loader2 } from "lucide-react"
import { useTranslation } from "react-i18next"

import type { AiRunPhase } from "@/lib/ai"

/** Waiting text for the phase that is currently running, with a spinner. */
export function PhaseWaiting({ phase }: { phase: AiRunPhase }) {
  const { t } = useTranslation("pipeline")
  return (
    <div
      className="ml-14 flex items-center gap-2 text-sm text-muted-foreground"
      role="status"
      data-section="phase-waiting"
      data-phase={phase}
    >
      <Loader2 className="size-4 animate-spin" aria-hidden />
      <span>{t(`guided.waiting.${phase}`, { defaultValue: t("guided.working") })}</span>
    </div>
  )
}
