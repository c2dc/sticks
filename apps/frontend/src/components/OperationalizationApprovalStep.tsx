import { useTranslation } from "react-i18next"

import { CommandProposalList } from "@/components/CommandProposalList"
import { EnvironmentRequirementsSummary } from "@/components/EnvironmentRequirementsSummary"
import { NegotiationSummary, objectionsFromTechniques } from "@/components/NegotiationSummary"
import { PlannedCoverageBadge } from "@/components/PlannedCoverageBadge"
import type { AiOperationalizationReview } from "@/lib/ai"
import { cn } from "@/lib/utils"

/**
 * Content of the operationalization approval pause. The approve button is NOT
 * rendered here: the caller (GuidedRunView) owns the approval affordance so every
 * step is approved the same way.
 *
 * `negotiationRounds` is the number of rounds used (`agent_runs.negotiation_rounds_used`);
 * the summary is shown only when it is provided.
 */
export function OperationalizationApprovalStep({
  review,
  negotiationRounds,
}: {
  review: AiOperationalizationReview
  negotiationRounds?: number | null
}) {
  const { t } = useTranslation("pipeline")
  const showNegotiation = typeof negotiationRounds === "number"

  return (
    <div className="flex flex-col gap-4" data-section="operationalization-approval-step">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">{t("guided.operationalization.title")}</p>
        <PlannedCoverageBadge coverage={review.planned_coverage} />
      </div>

      {review.techniques.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("guided.operationalization.noTechniques")}</p>
      ) : (
        review.techniques.map((technique) => {
          const status = technique.emulability_status
          return (
            <section
              key={technique.technique_id}
              className="flex flex-col gap-3 rounded-xl border bg-card p-4"
              data-section="operationalization-technique"
              data-technique-id={technique.technique_id}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm font-medium">{technique.technique_id}</span>
                {status ? (
                  <span
                    className={cn(
                      "rounded-full border px-2 py-0.5 text-xs",
                      status === "not_emulable" ? "border-team-red/40 bg-team-red/5" : "border-team-blue/40 bg-team-blue/5",
                    )}
                    data-section="emulability-status"
                    data-status={status}
                  >
                    {t(`guided.emulability.${status}`, { defaultValue: status })}
                  </span>
                ) : null}
              </div>
              {technique.emulability_reason_code ? (
                <p className="text-xs text-muted-foreground" data-section="emulability-reason">
                  {t(`guided.emulability.reason.${technique.emulability_reason_code}`, {
                    defaultValue: technique.emulability_reason_code,
                  })}
                </p>
              ) : null}
              {technique.commands.length > 0 ? <CommandProposalList commands={technique.commands} /> : null}
              {technique.environment_requirements ? (
                <EnvironmentRequirementsSummary requirements={technique.environment_requirements} />
              ) : null}
            </section>
          )
        })
      )}

      {showNegotiation ? (
        <NegotiationSummary
          rounds={negotiationRounds}
          objections={objectionsFromTechniques(review.techniques)}
        />
      ) : null}
    </div>
  )
}
