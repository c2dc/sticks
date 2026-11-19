import { AlertTriangle } from "lucide-react"
import { useTranslation } from "react-i18next"

import type { AiRunFailure } from "@/lib/ai"

/**
 * Failure banner: title, the line for `failure.code` and, when present, the cause.
 * `cause_code` is translated through `guided.error.{cause_code}` when that key
 * exists; otherwise the raw code is shown.
 */
export function RunFailure({ failure }: { failure: AiRunFailure }) {
  const { t } = useTranslation("pipeline")
  return (
    <div
      className="ml-14 flex flex-col gap-1 rounded-xl border border-team-red/40 bg-team-red/5 p-4 text-sm"
      role="alert"
      data-section="run-failure"
      data-code={failure.code}
    >
      <p className="flex items-center gap-2 font-medium">
        <AlertTriangle className="size-4" aria-hidden />
        {t("guided.failure.title")}
      </p>
      <p>{t(`guided.failure.code.${failure.code}`, { defaultValue: failure.code })}</p>
      {failure.cause_code ? (
        <p className="text-muted-foreground" data-section="run-failure-cause">
          {t(`guided.error.${failure.cause_code}`, { defaultValue: failure.cause_code })}
        </p>
      ) : null}
    </div>
  )
}
