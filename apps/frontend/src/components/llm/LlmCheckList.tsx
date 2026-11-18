import { AlertTriangle, CheckCircle2, MinusCircle, XCircle } from "lucide-react"
import { useTranslation } from "react-i18next"

import type { CheckStatus, PreflightCheck } from "@/lib/llm"
import { cn } from "@/lib/utils"

const STATUS_ICON = {
  pass: { Icon: CheckCircle2, className: "text-green-600 dark:text-green-400" },
  warn: { Icon: AlertTriangle, className: "text-yellow-600 dark:text-yellow-400" },
  fail: { Icon: XCircle, className: "text-red-600 dark:text-red-400" },
  skipped: { Icon: MinusCircle, className: "text-muted-foreground" },
} as const satisfies Record<CheckStatus, { Icon: unknown; className: string }>

/**
 * Result of the Preflight checks. Each item uses an icon with a distinct shape
 * plus translated text and a screen-reader status, so color is never the only cue.
 */
export function LlmCheckList({ checks }: { checks: PreflightCheck[] }) {
  const { t } = useTranslation("llm")

  return (
    <ul className="space-y-1.5" aria-live="polite" aria-label={t("check.title")}>
      {checks.map((check) => {
        const { Icon, className } = STATUS_ICON[check.status]
        const text =
          check.code === "skipped"
            ? t("check.status.skipped")
            : t(`check.${check.name}.${check.code}`)
        return (
          <li key={check.name} className="flex items-start gap-2 text-sm">
            <Icon className={cn("mt-0.5 size-4 shrink-0", className)} aria-hidden />
            <span>
              <span className="sr-only">{t(`check.status.${check.status}`)}: </span>
              {text}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
