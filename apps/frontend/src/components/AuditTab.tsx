import { useMemo } from "react"
import { useTranslation } from "react-i18next"

import { AgentTracePanel } from "@/components/AgentTracePanel"
import { EmptyNote } from "@/components/StageSection"
import type { AiIteration, AiStageOutput } from "@/lib/ai"

/**
 * AuditTab — the consolidated Audit view (task 9.5, Req. 1.1, 1.5). Where the
 * per-iteration Run view navigates ONE iteration at a time, this tab lists the
 * WHOLE Audit_Trace at once, grouped by iteration → agent step. Each agent step
 * reuses the same {@link AgentTracePanel} so every row carries the identical
 * Input → Processing → Output rendering — including the environment step's
 * specialized fixed-vs-agent-decided split / gate verdicts / sub-phases and the
 * raw LLM prompt/response behind the collapsible "show details" disclosure
 * (closed by default).
 *
 * It is reachable from the existing AI run overlay/view: {@link AiRunView}
 * toggles between the per-iteration navigation and this consolidated Audit view.
 *
 * Every user-facing string is routed through i18next `t()` under the
 * `aiRun.audit.*` / `aiRun.stageName.*` keys (pt-BR source of truth, en parity).
 * Component identifiers (`data-section`) are English per Req. 9.1/9.4.
 */

/**
 * The meaningful per-iteration agent-step sequence (mirrors AiRunView's order).
 * Semantic, number-free `AgentStage` values (Req. 1.1, 1.5).
 */
const AGENT_STEP_ORDER = [
  "dag",
  "operationalization",
  "environment",
] as const

/** Order an iteration's stage outputs by the meaningful agent-step sequence. */
function orderStageOutputs(stageOutputs: AiStageOutput[]): AiStageOutput[] {
  const rank = (stage: string) => {
    const idx = AGENT_STEP_ORDER.indexOf(stage as (typeof AGENT_STEP_ORDER)[number])
    return idx === -1 ? AGENT_STEP_ORDER.length : idx
  }
  return [...stageOutputs].sort((a, b) => rank(a.stage) - rank(b.stage))
}

export function AuditTab({ iterations }: { iterations: AiIteration[] }) {
  const { t } = useTranslation("pipeline")

  if (iterations.length === 0) {
    return (
      <div className="flex flex-col gap-2" data-section="ai-run-audit">
        <EmptyNote>{t("aiRun.audit.empty")}</EmptyNote>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3" data-section="ai-run-audit">
      <p className="text-xs text-muted-foreground">{t("aiRun.audit.description")}</p>
      {iterations.map((iteration) => (
        <AuditIteration key={iteration.id} iteration={iteration} />
      ))}
    </div>
  )
}

/** One iteration group: its index header then each ordered agent step's trace. */
function AuditIteration({ iteration }: { iteration: AiIteration }) {
  const { t } = useTranslation("pipeline")
  const stageOutputs = useMemo(
    () => orderStageOutputs(iteration.stage_outputs ?? []),
    [iteration.stage_outputs],
  )

  return (
    <div
      className="flex flex-col gap-2 rounded-md border p-3"
      data-section="ai-run-audit-iteration"
      data-index={iteration.index}
    >
      <span className="text-sm font-semibold">
        {t("aiRun.audit.iteration", { indice: iteration.index + 1 })}
      </span>
      {stageOutputs.length === 0 ? (
        <EmptyNote>{t("aiRun.audit.noSteps")}</EmptyNote>
      ) : (
        <div className="flex flex-col gap-3" data-section="ai-run-audit-agent-steps">
          {stageOutputs.map((stageOutput) => (
            <AgentTracePanel
              key={stageOutput.id}
              stageName={t(`aiRun.stageName.${stageOutput.stage}`, {
                defaultValue: stageOutput.stage,
              })}
              stageOutput={stageOutput}
            />
          ))}
        </div>
      )}
    </div>
  )
}
