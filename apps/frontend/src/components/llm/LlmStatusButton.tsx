import { AlertTriangle, CheckCircle2, HelpCircle, Loader2, XCircle } from "lucide-react"
import { useTranslation } from "react-i18next"

import { LlmPanel } from "@/components/llm/LlmPanel"
import { useLlmStatus } from "@/components/llm/LlmStatusProvider"
import { Button } from "@/components/ui/button"
import { Popover } from "@/components/ui/popover"
import type { LlmState } from "@/lib/llm"
import { cn } from "@/lib/utils"

const STATE_ICON = {
  ok: { Icon: CheckCircle2, className: "text-green-600 dark:text-green-400" },
  warning: { Icon: AlertTriangle, className: "text-yellow-600 dark:text-yellow-400" },
  blocked: { Icon: XCircle, className: "text-red-600 dark:text-red-400" },
  checking: { Icon: Loader2, className: "animate-spin text-muted-foreground" },
  unknown: { Icon: HelpCircle, className: "text-muted-foreground" },
} as const satisfies Record<LlmState, { Icon: unknown; className: string }>

/** Single status icon for the language model; clicking it opens the model panel. */
export function LlmStatusButton() {
  const { t } = useTranslation("llm")
  const { state, preflight, profiles, selectedId, panelOpen, openPanel, closePanel } = useLlmStatus()

  const model =
    preflight?.model_id ?? profiles.find((profile) => profile.id === selectedId)?.model_id ?? ""
  const tooltip = t("icon.tooltip", { model, state: t(`state.${state}`) })
  const { Icon, className } = STATE_ICON[state]

  return (
    <div className="relative">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={() => (panelOpen ? closePanel() : openPanel())}
        aria-label={`${t("icon.label")}: ${tooltip}`}
        title={tooltip}
        aria-haspopup="dialog"
        aria-expanded={panelOpen}
      >
        <Icon className={cn("size-5", className)} aria-hidden />
      </Button>
      <Popover
        open={panelOpen}
        onOpenChange={(open) => (open ? openPanel() : closePanel())}
        aria-label={t("panel.title")}
      >
        <LlmPanel />
      </Popover>
    </div>
  )
}
