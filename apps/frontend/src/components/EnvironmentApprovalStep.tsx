import { CheckCircle2, XCircle } from "lucide-react"
import { useTranslation } from "react-i18next"

import { PlannedCoverageBadge } from "@/components/PlannedCoverageBadge"

/**
 * Content of the environment approval pause. The approve button is NOT rendered
 * here (the caller owns it, as in OperationalizationApprovalStep).
 *
 * `isolationVerified` is the isolation verdict, shown as an icon next to the
 * `guided.environment.isolation` label; null hides it. `commands` are the final
 * commands that will run; `plannedCoverage` is the recalculated percentage (0-100).
 */
export function EnvironmentApprovalStep({
  isolationVerified,
  commands,
  plannedCoverage,
}: {
  isolationVerified?: boolean | null
  commands: string[]
  plannedCoverage?: number | null
}) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-3 rounded-xl border bg-card p-4" data-section="environment-approval-step">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">{t("guided.environment.title")}</p>
        <PlannedCoverageBadge coverage={plannedCoverage} />
      </div>
      {typeof isolationVerified === "boolean" ? (
        <p className="flex items-center gap-2 text-sm" data-section="environment-isolation" data-verified={isolationVerified}>
          {isolationVerified ? (
            <CheckCircle2 className="size-4 text-team-blue" aria-hidden />
          ) : (
            <XCircle className="size-4 text-team-red" aria-hidden />
          )}
          {t("guided.environment.isolation")}
        </p>
      ) : null}
      {commands.length > 0 ? (
        <div data-section="environment-commands">
          <p className="text-xs text-muted-foreground">{t("guided.environment.commands")}</p>
          <ul className="mt-1 flex flex-col gap-1.5">
            {commands.map((command, index) => (
              <li key={`${index}:${command}`}>
                <pre className="overflow-x-auto whitespace-pre-wrap rounded-md bg-muted/50 p-2 font-mono text-xs">
                  {command}
                </pre>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}
