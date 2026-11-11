import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import {
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Loader2,
  XCircle,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { AgentTracePanel } from "@/components/AgentTracePanel"
import { AuditTab } from "@/components/AuditTab"
import { EmptyNote, StageSection } from "@/components/StageSection"
import {
  compareAiRunWithCuration,
  getAiRun,
  startAiRun,
  type AiEvaluation,
  type AiIteration,
  type AiRun,
  type AiRunDetail,
  type AiStageOutput,
  type GroundTruthComparison,
} from "@/lib/ai"

/**
 * AiRunView — thin frontend surface to start an AI curation run and observe its
 * iterations and the evaluator verdict (Req. 13.3, 13.4).
 *
 * The user clicks "Start AI run" ({@link startAiRun}); once a run exists the view
 * polls {@link getAiRun} on a 3-second interval (≤ 5s) while the run is in a
 * non-terminal state. Polling at ≤ 5s guarantees each new iteration is displayed
 * within 5 seconds of becoming available (Req. 13.3) and the evaluator verdict
 * within 5 seconds of becoming available (Req. 13.4). Polling stops once the run
 * reaches a terminal status (`completed` / `failed` / `aborted`), after one
 * final read so the last iteration/verdict is never missed.
 *
 * The view is intentionally thin: it renders the run summary (status, iterations
 * used, max iterations, final verdict), the list of iterations (index + refined
 * stage) and, per iteration, the evaluator verdict (decision + verdict success +
 * metrics + rationale). Every user-facing string goes through i18next `t()` under
 * the `aiRun.*` keys of the `pipeline` namespace (pt-BR source of truth, en
 * parity — tasks 17.2/17.3).
 */

/** Poll interval while the run is non-terminal — ≤ 5s to satisfy Req. 13.3/13.4. */
const POLL_INTERVAL_MS = 3000

/** Run statuses after which no further iteration/verdict can appear. */
const TERMINAL_STATUSES = new Set(["completed", "failed", "aborted"])

function isTerminal(run: AiRun | null | undefined): boolean {
  return run != null && TERMINAL_STATUSES.has(run.status)
}

/**
 * The meaningful per-iteration agent sequence (design §Components A). The Run is
 * an iterative plan→execute→measure→improve loop, NOT a linear 1..6 ruler; within
 * each iteration these agent steps run in order. Each `AgentStage` value maps to a
 * meaningful English name via the `aiRun.stageName.*` i18n subtree — the
 * semantic, number-free phase names (`dag` / `operationalization` /
 * `environment`), never a numbered "Stage 3/4/5" (Req. 1.1, 1.5, 9.1, 9.4).
 */
const AGENT_STEP_ORDER = [
  "dag",
  "operationalization",
  "environment",
] as const

/** Order a resolved iteration's stage outputs by the meaningful agent-step sequence. */
function orderStageOutputs(stageOutputs: AiStageOutput[]): AiStageOutput[] {
  const rank = (stage: string) => {
    const idx = AGENT_STEP_ORDER.indexOf(stage as (typeof AGENT_STEP_ORDER)[number])
    return idx === -1 ? AGENT_STEP_ORDER.length : idx
  }
  return [...stageOutputs].sort((a, b) => rank(a.stage) - rank(b.stage))
}

export function AiRunView({
  campaignId,
  curated = false,
}: {
  campaignId: string
  /**
   * Whether this campaign is one of the eight Curated_Campaigns. Only then is
   * the on-demand "Compare with curation" action offered on a completed Run
   * (Req. 5.1, 5.2); for a non-curated campaign the action is never rendered.
   */
  curated?: boolean
}) {
  const { t } = useTranslation("pipeline")
  const [detail, setDetail] = useState<AiRunDetail | null>(null)
  const [runId, setRunId] = useState<number | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Keep the latest run id in a ref so the polling effect can read it without
  // re-subscribing on every detail update.
  const runIdRef = useRef<number | null>(null)
  runIdRef.current = runId

  const start = useCallback(async () => {
    setStarting(true)
    setStartError(null)
    setLoadError(null)
    setDetail(null)
    try {
      const run = await startAiRun(campaignId)
      setRunId(run.id)
      // Seed the view with the summary row immediately; the poll fills in the
      // iterations/verdict as they become available.
      setDetail({ run, iterations: [] })
    } catch {
      setStartError(t("aiRun.startError"))
    } finally {
      setStarting(false)
    }
  }, [campaignId, t])

  // Poll the run while it exists and is non-terminal. A single final read after
  // the run turns terminal guarantees the last iteration/verdict is shown.
  useEffect(() => {
    if (runId == null) return

    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined

    const poll = async () => {
      const id = runIdRef.current
      if (!active || id == null) return
      try {
        const next = await getAiRun(campaignId, id)
        if (!active) return
        setDetail(next)
        setLoadError(null)
        if (!isTerminal(next.run)) {
          timer = setTimeout(poll, POLL_INTERVAL_MS)
        }
      } catch {
        if (!active) return
        setLoadError(t("aiRun.loadError"))
        // Keep polling on a transient read error so the view recovers on its own.
        timer = setTimeout(poll, POLL_INTERVAL_MS)
      }
    }

    void poll()

    return () => {
      active = false
      if (timer) clearTimeout(timer)
    }
  }, [campaignId, runId, t])

  return (
    <div className="flex flex-col gap-4" data-section="ai-run">
      <StageSection
        data-section="ai-run-control"
        label={t("aiRun.title")}
        kind="purple"
        description={t("aiRun.description")}
      >
        <div className="flex items-center gap-3">
          <Button type="button" onClick={() => void start()} disabled={starting}>
            {starting ? (
              <Loader2 className="size-4 animate-spin" aria-hidden />
            ) : null}
            {starting ? t("aiRun.starting") : t("aiRun.start")}
          </Button>
        </div>
        {startError ? <EmptyNote>{startError}</EmptyNote> : null}
      </StageSection>

      {detail ? (
        <>
          <RunSummary
            run={detail.run}
            campaignId={campaignId}
            curated={curated}
          />
          {loadError ? <EmptyNote>{loadError}</EmptyNote> : null}
          <IterationsSection iterations={detail.iterations} />
        </>
      ) : !startError ? (
        <EmptyNote>{t("aiRun.noRun")}</EmptyNote>
      ) : null}
    </div>
  )
}

/** Run summary: status, iterations used / max and the overall final verdict. */
function RunSummary({
  run,
  campaignId,
  curated,
}: {
  run: AiRun
  campaignId: string
  curated: boolean
}) {
  const { t } = useTranslation("pipeline")
  const statusLabel = t(`aiRun.status.${run.status}`, { defaultValue: run.status })

  return (
    <StageSection data-section="ai-run-summary" label={t("aiRun.run.title")} kind="neutral">
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryItem label={t("aiRun.run.status")} value={statusLabel} />
        <SummaryItem
          label={t("aiRun.run.iterationsUsed")}
          value={String(run.iterations_used)}
        />
        <SummaryItem
          label={t("aiRun.run.maxIterations")}
          value={String(run.max_iterations)}
        />
        <SummaryItem
          label={t("aiRun.run.verdict")}
          value={<VerdictBadge success={run.final_verdict_success} />}
        />
      </dl>
      {curated && run.status === "completed" ? (
        <GroundTruthCompare campaignId={campaignId} runId={run.id} />
      ) : null}
    </StageSection>
  )
}

/**
 * Curated-only "Compare with curation" action on the completed-run summary
 * (Req. 5.1, 5.3). Rendered only for a completed Run of one of the eight
 * Curated_Campaigns (the parent gates on `curated` + `status === "completed"`).
 * On click it fetches the on-demand Ground_Truth_Comparison and renders the
 * per-stage matched / missing / extra sets. Curation is used only as the
 * comparison baseline — it never drives a Run (Req. 5.4).
 */
function GroundTruthCompare({
  campaignId,
  runId,
}: {
  campaignId: string
  runId: number
}) {
  const { t } = useTranslation("pipeline")
  const [comparison, setComparison] = useState<GroundTruthComparison | null>(null)
  const [comparing, setComparing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const compare = useCallback(async () => {
    setComparing(true)
    setError(null)
    try {
      const result = await compareAiRunWithCuration(campaignId, runId)
      setComparison(result)
    } catch {
      setError(t("aiRun.comparison.error"))
    } finally {
      setComparing(false)
    }
  }, [campaignId, runId, t])

  return (
    <div className="mt-3 flex flex-col gap-3" data-section="ai-run-ground-truth">
      <div>
        <Button
          type="button"
          variant="outline"
          onClick={() => void compare()}
          disabled={comparing}
          data-action="compare-with-curation"
        >
          {comparing ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
          {comparing ? t("aiRun.comparison.comparing") : t("aiRun.comparison.action")}
        </Button>
      </div>
      {error ? <EmptyNote>{error}</EmptyNote> : null}
      {comparison ? (
        <div
          className="flex flex-col gap-3"
          data-section="ai-run-ground-truth-result"
        >
          {comparison.stages.map((stage) => (
            <div
              key={stage.stage}
              className="flex flex-col gap-2 rounded-md border p-3"
              data-section="ai-run-ground-truth-stage"
              data-stage={stage.stage}
            >
              <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t(`aiRun.comparison.stage.${stage.stage}`, {
                  defaultValue: stage.stage,
                })}
              </span>
              <dl className="grid gap-2 sm:grid-cols-3">
                <SummaryItem
                  label={t("aiRun.comparison.matched")}
                  value={String(stage.matched.length)}
                />
                <SummaryItem
                  label={t("aiRun.comparison.missing")}
                  value={String(stage.missing.length)}
                />
                <SummaryItem
                  label={t("aiRun.comparison.extra")}
                  value={String(stage.extra.length)}
                />
              </dl>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function SummaryItem({
  label,
  value,
}: {
  label: string
  value: ReactNode
}) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md border p-3">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="text-sm font-semibold">{value}</dd>
    </div>
  )
}

/**
 * The iterative Run as per-ITERATION navigation (design §The iterative Run view).
 * The Run is a plan→execute→measure→improve loop of `1..N` iterations — NOT a
 * linear 1..6 ruler. The Researcher navigates one iteration at a time; the
 * selected iteration renders its agent steps (DAG generation agent,
 * Operationalization agent, Environment agent, Execution, Evaluation) each as an
 * Input → Processing → Output {@link AgentTracePanel} (Req. 1.1, 1.5, 6.1–6.4).
 *
 * As new iterations surface on the ≤5s poll, the navigator keeps the Researcher's
 * current selection stable and auto-advances to the latest iteration only while
 * they are already parked on what was the last one.
 */
function IterationsSection({ iterations }: { iterations: AiIteration[] }) {
  const { t } = useTranslation("pipeline")
  const [selected, setSelected] = useState(0)
  const [view, setView] = useState<"iterations" | "audit">("iterations")
  const prevCount = useRef(0)

  // Keep the selection valid as iterations arrive. If the Researcher was parked
  // on the last iteration, auto-advance to the newest; otherwise hold their place.
  useEffect(() => {
    const count = iterations.length
    setSelected((current) => {
      if (count === 0) return 0
      if (current > count - 1) return count - 1
      if (prevCount.current > 0 && current === prevCount.current - 1) return count - 1
      return current
    })
    prevCount.current = count
  }, [iterations.length])

  if (iterations.length === 0) {
    return (
      <StageSection
        data-section="ai-run-iterations"
        label={t("aiRun.iterations.title")}
        kind="blue"
      >
        <EmptyNote>{t("aiRun.iterations.empty")}</EmptyNote>
      </StageSection>
    )
  }

  const current = Math.min(selected, iterations.length - 1)
  const iteration = iterations[current]

  return (
    <StageSection
      data-section="ai-run-iterations"
      label={t("aiRun.iterations.title")}
      kind="blue"
    >
      <ViewToggle view={view} onSelect={setView} />
      {view === "iterations" ? (
        <>
          <IterationNavigator
            count={iterations.length}
            current={current}
            onSelect={setSelected}
          />
          <IterationPanel iteration={iteration} />
        </>
      ) : (
        <AuditTab iterations={iterations} />
      )}
    </StageSection>
  )
}

/**
 * The tab/toggle between the per-iteration navigation and the consolidated
 * {@link AuditTab} (task 9.5). It sits between the Run summary and the chosen
 * view; "By iteration" is the default. Both labels route through i18next.
 */
function ViewToggle({
  view,
  onSelect,
}: {
  view: "iterations" | "audit"
  onSelect: (view: "iterations" | "audit") => void
}) {
  const { t } = useTranslation("pipeline")

  return (
    <div
      className="flex items-center gap-2"
      role="tablist"
      data-section="ai-run-view-toggle"
    >
      <Button
        type="button"
        variant={view === "iterations" ? "default" : "outline"}
        size="sm"
        role="tab"
        aria-selected={view === "iterations"}
        onClick={() => onSelect("iterations")}
        data-action="view-iterations"
      >
        {t("aiRun.audit.iterationsTab")}
      </Button>
      <Button
        type="button"
        variant={view === "audit" ? "default" : "outline"}
        size="sm"
        role="tab"
        aria-selected={view === "audit"}
        onClick={() => onSelect("audit")}
        data-action="view-audit"
      >
        {t("aiRun.audit.auditTab")}
      </Button>
    </div>
  )
}

/** Per-iteration navigator: previous / next and a position readout (Req. 9.4). */
function IterationNavigator({
  count,
  current,
  onSelect,
}: {
  count: number
  current: number
  onSelect: (index: number) => void
}) {
  const { t } = useTranslation("pipeline")

  return (
    <div
      className="flex items-center justify-between gap-2"
      data-section="ai-run-iteration-nav"
    >
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => onSelect(Math.max(0, current - 1))}
        disabled={current <= 0}
        data-action="prev-iteration"
        aria-label={t("aiRun.iterations.previous")}
      >
        <ChevronLeft className="size-4" aria-hidden />
      </Button>
      <span className="text-sm font-semibold" data-part="iteration-position">
        {t("aiRun.iterations.navigation", { atual: current + 1, total: count })}
      </span>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => onSelect(Math.min(count - 1, current + 1))}
        disabled={current >= count - 1}
        data-action="next-iteration"
        aria-label={t("aiRun.iterations.next")}
      >
        <ChevronRight className="size-4" aria-hidden />
      </Button>
    </div>
  )
}

/**
 * One iteration rendered in full: its index + refined stage, then each agent step
 * as an Input → Processing → Output {@link AgentTracePanel} ordered by the
 * meaningful agent-step sequence, then the Execution and Evaluation steps.
 */
function IterationPanel({ iteration }: { iteration: AiIteration }) {
  const { t } = useTranslation("pipeline")
  const refined = iteration.refined_stage
  const stageOutputs = useMemo(
    () => orderStageOutputs(iteration.stage_outputs ?? []),
    [iteration.stage_outputs],
  )

  return (
    <div
      className="flex flex-col gap-3 rounded-md border p-3"
      data-section="ai-run-iteration"
      data-index={iteration.index}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold">
          {t("aiRun.iterations.index", { indice: iteration.index + 1 })}
        </span>
        <span className="text-xs text-muted-foreground">
          {t("aiRun.iterations.refinedStage")}:{" "}
          {refined
            ? t(`aiRun.stageName.${refined}`, { defaultValue: refined })
            : t("aiRun.iterations.noRefinedStage")}
        </span>
      </div>
      {iteration.notes ? (
        <p className="text-xs text-muted-foreground">{iteration.notes}</p>
      ) : null}
      {iteration.failing_agent ? (
        <EmptyNote>
          {t("aiRun.iterations.failedAgent", {
            agente: t(`aiRun.stageName.${iteration.failing_agent}`, {
              defaultValue: iteration.failing_agent,
            }),
          })}
        </EmptyNote>
      ) : null}

      {/* Each AI agent step as an Input → Processing → Output trace panel. */}
      {stageOutputs.length === 0 ? (
        <EmptyNote>{t("aiRun.trace.empty")}</EmptyNote>
      ) : (
        <div className="flex flex-col gap-3" data-section="ai-run-agent-steps">
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

      {/* Execution + Evaluation steps close the iteration's agent sequence. */}
      <div className="flex flex-col gap-2" data-section="ai-run-execution">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t("aiRun.stageName.execution")}
        </span>
        <EvaluationBlock evaluation={iteration.evaluation} />
      </div>
    </div>
  )
}

/** The evaluator verdict for an iteration, or a neutral "not yet available" note. */
function EvaluationBlock({
  evaluation,
}: {
  evaluation?: AiEvaluation | null
}) {
  const { t } = useTranslation("pipeline")

  if (!evaluation) {
    return (
      <p className="text-xs text-muted-foreground" data-section="ai-run-evaluation-absent">
        {t("aiRun.evaluation.absent")}
      </p>
    )
  }

  const decisionLabel = t(`aiRun.decision.${evaluation.decision}`, {
    defaultValue: evaluation.decision,
  })

  return (
    <div
      className="flex flex-col gap-2 rounded-md border bg-muted/30 p-2"
      data-section="ai-run-evaluation"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-team-purple">
          {t("aiRun.evaluation.title")}
        </span>
        <VerdictBadge success={evaluation.verdict_success} />
      </div>
      <dl className="grid gap-2 sm:grid-cols-3">
        <SummaryItem label={t("aiRun.evaluation.decision")} value={decisionLabel} />
        <SummaryItem
          label={t("aiRun.evaluation.successRate")}
          value={formatPercent(evaluation.success_rate)}
        />
        <SummaryItem
          label={t("aiRun.evaluation.proceduralCoverage")}
          value={formatPercent(evaluation.procedural_coverage)}
        />
      </dl>
      {evaluation.rationale ? (
        <div className="flex flex-col gap-0.5">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("aiRun.evaluation.rationale")}
          </span>
          <p className="whitespace-pre-wrap text-xs text-muted-foreground">
            {evaluation.rationale}
          </p>
        </div>
      ) : null}
    </div>
  )
}

/**
 * A pass/fail/pending verdict badge. `null`/`undefined` renders the neutral
 * "awaiting verdict" state (the run has not reached a terminal verdict yet).
 */
function VerdictBadge({ success }: { success?: boolean | null }) {
  const { t } = useTranslation("pipeline")

  if (success == null) {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm text-muted-foreground">
        <CircleDashed className="size-4" aria-hidden />
        {t("aiRun.verdict.pending")}
      </span>
    )
  }
  if (success) {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm font-medium text-team-blue">
        <CheckCircle2 className="size-4" aria-hidden />
        {t("aiRun.verdict.approved")}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm font-medium text-team-red">
      <XCircle className="size-4" aria-hidden />
      {t("aiRun.verdict.rejected")}
    </span>
  )
}

/** Format a ratio in [0, 1] as an integer percentage; non-finite → 0%. */
function formatPercent(value: number): string {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
  return `${Math.round(clamped * 100)}%`
}
