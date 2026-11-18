import { Loader2 } from "lucide-react"
import { useTranslation } from "react-i18next"

import { LlmCheckList } from "@/components/llm/LlmCheckList"
import { useLlmStatus } from "@/components/llm/LlmStatusProvider"
import { Button } from "@/components/ui/button"

/**
 * Model verification shown above the "start" button: the Preflight checks, the
 * reason when the start is blocked (with a shortcut to change the model) and a
 * summary line when it is ready or has only warnings.
 */
export function PreflightPanel({ runError }: { runError?: string | null }) {
  const { t } = useTranslation("llm")
  const { state, preflight, openPanel } = useLlmStatus()

  const firstBlockingCode = preflight?.blocking_codes[0]
  const blocked = state === "blocked"

  return (
    <section
      className="flex flex-col gap-3 rounded-xl border bg-card p-4"
      data-section="preflight-panel"
      data-state={state}
      aria-label={t("preflight.title")}
    >
      <h3 className="text-sm font-semibold">{t("preflight.title")}</h3>

      {state === "checking" ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          {t("preflight.checking")}
        </p>
      ) : null}

      {state === "unknown" ? (
        <p className="text-sm text-muted-foreground" role="status">
          {t("preflight.loadError")}
        </p>
      ) : null}

      {preflight && state !== "checking" && state !== "unknown" ? (
        <LlmCheckList checks={preflight.checks} />
      ) : null}

      {state === "ok" ? <p className="text-sm">{t("preflight.ready")}</p> : null}
      {state === "warning" ? <p className="text-sm">{t("preflight.warnings")}</p> : null}

      {blocked ? (
        <div className="flex flex-col gap-2" role="alert">
          <p className="text-sm font-medium text-red-600 dark:text-red-400">{t("block.title")}</p>
          {firstBlockingCode ? (
            <p className="text-sm">{t(`block.reason.${firstBlockingCode}`)}</p>
          ) : null}
        </div>
      ) : null}

      {runError ? (
        <p className="text-sm text-red-600 dark:text-red-400" role="alert">
          {runError}
        </p>
      ) : null}

      {blocked || runError ? (
        <div>
          <Button type="button" variant="outline" size="sm" onClick={openPanel}>
            {t("block.changeModel")}
          </Button>
        </div>
      ) : null}
    </section>
  )
}
