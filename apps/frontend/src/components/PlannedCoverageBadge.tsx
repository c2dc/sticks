import { useTranslation } from "react-i18next"

/**
 * Badge with the planned emulation coverage. `coverage` is already a percentage
 * (0-100), as sent by the backend in `planned_coverage`. Renders nothing while
 * the value is not available.
 */
export function PlannedCoverageBadge({ coverage }: { coverage?: number | null }) {
  const { t } = useTranslation("pipeline")
  if (typeof coverage !== "number") return null
  return (
    <span
      className="inline-flex w-fit items-center rounded-full border border-team-blue/40 bg-team-blue/5 px-3 py-1 text-xs font-medium"
      data-section="planned-coverage-badge"
    >
      {t("guided.coverage.planned", { coverage: Math.round(coverage) })}
    </span>
  )
}
