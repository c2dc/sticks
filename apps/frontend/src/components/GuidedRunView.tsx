import { type ReactNode, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  CheckCircle2,
  ChevronDown,
  Loader2,
  Sparkles,
  XCircle,
} from "lucide-react"

import { EnvironmentApprovalStep } from "@/components/EnvironmentApprovalStep"
import { FlowPhaseView } from "@/components/FlowPhaseView"
import { FlowProvenanceCard } from "@/components/FlowProvenanceCard"
import { OperationalizationApprovalStep } from "@/components/OperationalizationApprovalStep"
import { OperationalizationScreen } from "@/components/OperationalizationScreen"
import { OrchestratorAvatar, type OrchestratorMood } from "@/components/OrchestratorAvatar"
import { RunFailure } from "@/components/RunFailure"
import { RunNotice } from "@/components/RunNotice"
import { useLlmStatus } from "@/components/llm/LlmStatusProvider"
import { PreflightPanel } from "@/components/llm/PreflightPanel"
import { Button } from "@/components/ui/button"
import { campaignDisplayName } from "@/lib/campaign"
import { reasonCodeFrom } from "@/lib/llm"
import { cn } from "@/lib/utils"
import { aiRunReasonFrom, type AiRun } from "@/lib/ai"
import { type UseAiRun } from "@/lib/use-ai-run"

/**
 * GuidedRunView — the conversational, orchestrator-led Run screen (Layer 2).
 *
 * The screen drives the real interactive backend run through `useAiRun`. The
 * backend pauses at `awaiting_approval` after the `operationalization`, `flow`
 * and `environment` phases; each pause renders its artifact plus the explicit
 * approval action. While a phase runs in the background (`running`), the
 * orchestrator bubble shows the phase-specific waiting text. Terminal states show the execution report,
 * the evaluator verdict with the achieved coverage and, for failed runs, the
 * structured failure.
 *
 * Known gap: `AiRun` has no typed isolation verdict, so the environment pause
 * shows the final commands and the planned coverage but not the isolation check.
 */

const TERMINAL_STATUSES = new Set(["completed", "failed", "aborted"])

function isTerminal(run: AiRun): boolean {
  return TERMINAL_STATUSES.has(run.status)
}

export function GuidedRunView({
  campaignId,
  campaignName,
  version,
  ai,
}: {
  /** The run state and actions, owned by the parent (`ExecutionWizard`). */
  ai: UseAiRun
  /** The chosen campaign key (`campaign_key` of the STIX listing). */
  campaignId: string
  /** The campaign's readable name; falls back to a humanized key. */
  campaignName?: string
  /** The chosen STIX base version, used by the operationalization listing. */
  version: string
}) {
  const { t } = useTranslation("pipeline")
  const { t: tLlm } = useTranslation("llm")
  const { state: llmState, recheck } = useLlmStatus()
  const displayName = campaignDisplayName(campaignName, campaignId)
  const { run, busy, error } = ai

  if (ai.loading) {
    return (
      <GuidedShell>
        <OrchestratorBubble mood="thinking" pulse>
          {t("guided.thinking")}
        </OrchestratorBubble>
        <ThinkingDots />
      </GuidedShell>
    )
  }

  // Not started yet → the robot's welcome, the operationalization listing and the
  // single "start" action that begins the AI run.
  if (!run) {
    return (
      <GuidedShell>
        <OrchestratorBubble mood={busy ? "thinking" : "talking"} pulse>
          {busy ? t("guided.thinking") : t("guided.greeting")}
        </OrchestratorBubble>
        <div className="ml-14" data-section="guided-artifact" data-phase="operationalization">
          <OperationalizationScreen version={version} campaignKey={campaignId} />
        </div>
        <div className="ml-14">
          <PreflightPanel runError={error ? startErrorText(error, t, tLlm) : null} />
        </div>
        <div className="flex flex-col items-center gap-3">
          <Button
            size="lg"
            onClick={() => {
              void ai.start().then((started) => {
                if (!started) recheck()
              })
            }}
            disabled={busy || llmState === "blocked" || llmState === "checking"}
            className="px-8"
          >
            {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Sparkles className="size-4" aria-hidden />}
            {busy ? t("guided.preparing") : t("guided.start")}
          </Button>
        </div>
      </GuidedShell>
    )
  }

  const terminal = isTerminal(run)
  const pending = run.status === "awaiting_approval" ? run.pending_phase : null
  const proposals = run.flow_proposals ?? []
  const currentProposal = proposals.find((proposal) => proposal.current)
  const review = run.operationalization_review
  // The approve action of the current pause, rendered in the shared action row
  // next to "Cancel the run".
  const approveLabels = {
    operationalization: t("guided.approveOperationalization"),
    flow: t("guided.approveFlow"),
    environment: t("guided.approveEnvironment"),
  } as const
  const approveAction =
    pending === "operationalization" || pending === "flow" || pending === "environment"
      ? { phase: pending, label: approveLabels[pending] }
      : null

  // Final commands that will run, from the operationalization review (the only
  // typed source of resolved commands in `AiRun`).
  const environmentCommands = (review?.techniques ?? [])
    .filter((technique) => technique.emulability_status !== "not_emulable")
    .flatMap((technique) => technique.commands.map((command) => command.resolved_command))

  return (
    <GuidedShell>
      <RunHeader run={run} />

      {run.notice_code ? <RunNotice code={run.notice_code} /> : null}

      {run.status === "running" ? (
        <div className="flex flex-col gap-4" data-section="guided-phases">
          {/* The bubble carries the phase-specific text with an inline spinner, so
              the screen shows one coherent message instead of a generic bubble
              stacked over a second waiting line. The avatar stays in the bubble. */}
          <OrchestratorBubble mood="thinking" pulse>
            {run.active_phase ? (
              <WaitingMessage phase={run.active_phase} />
            ) : (
              t("guided.working")
            )}
          </OrchestratorBubble>
          {run.active_phase ? null : <ThinkingDots />}
        </div>
      ) : null}

      {pending === "operationalization" ? (
        <PauseTurn line={t("guided.speech.operationalization", { campaign: displayName })} phase="operationalization">
          {review ? <OperationalizationApprovalStep review={review} /> : null}
        </PauseTurn>
      ) : null}

      {pending === "flow" ? (
        <PauseTurn line={t("guided.speech.flow", { campaign: displayName })} phase="flow">
          <FlowPhaseView
            campaign={campaignId}
            currentProposal={currentProposal}
            showProvenance={false}
            proposals={proposals}
            maxProposals={run.flow_proposal_max}
            onSelectProposal={(proposalId) => void ai.selectFlowProposal(proposalId)}
            onRegenerate={() => void ai.regenerateFlow()}
            busy={busy}
          />
          {currentProposal ? <FlowProvenanceCard proposal={currentProposal} /> : null}
        </PauseTurn>
      ) : null}

      {pending === "environment" ? (
        <PauseTurn line={t("guided.speech.environment", { campaign: displayName })} phase="environment">
          <EnvironmentApprovalStep
            commands={environmentCommands}
            plannedCoverage={run.coverage?.planned_coverage ?? review?.planned_coverage}
          />
        </PauseTurn>
      ) : null}

      {terminal ? (
        <div className="flex flex-col gap-4 animate-in fade-in slide-in-from-bottom-2 fill-mode-both">
          <OrchestratorBubble
            mood={run.status === "completed" && run.final_verdict_success ? "happy" : "alert"}
          >
            {run.status === "completed"
              ? t("guided.speech.evaluation", { campaign: displayName })
              : t("guided.failure.title")}
          </OrchestratorBubble>
          {run.failure ? <RunFailure failure={run.failure} /> : null}
          <ExecutionReport run={run} />
          <EvaluationVerdict run={run} />
        </div>
      ) : null}

      {error && !terminal ? (
        <p className="ml-14 text-sm text-team-red" role="alert" data-section="guided-error">
          {actionErrorText(error, t)}
        </p>
      ) : null}

      {!terminal ? (
        <div
          className="flex flex-wrap items-center justify-center gap-3"
          data-section="guided-actions"
        >
          {approveAction ? (
            <ApproveButton
              label={approveAction.label}
              busy={busy}
              onClick={() => void ai.approve(approveAction.phase)}
            />
          ) : null}
          <Button
            variant="outline"
            size="sm"
            className="border-team-red/30 bg-team-red/5 text-team-red shadow-none hover:bg-team-red/10 hover:text-team-red"
            disabled={busy}
            onClick={() => void ai.abort()}
          >
            <XCircle className="size-4" aria-hidden />
            {t("guided.abort")}
          </Button>
        </div>
      ) : null}
    </GuidedShell>
  )
}

/** Translates a start failure (LLM block, run failure or generic). */
function startErrorText(
  error: unknown,
  t: (key: string) => string,
  tLlm: (key: string) => string,
): string {
  const { reason, codes, causeCode } = reasonCodeFrom(error)
  if (reason === "llm_blocked" && codes.length > 0) return tLlm(`block.reason.${codes[0]}`)
  if (reason === "run_failed" && causeCode) return tLlm(`reason.${causeCode}`)
  const { reason: runReason } = aiRunReasonFrom(error)
  if (runReason) return t(`guided.error.${runReason}`)
  return t("aiRun.startError")
}

/** Translates the error of an approval or other run action. */
function actionErrorText(error: unknown, t: (key: string, options?: object) => string): string {
  const { reason } = aiRunReasonFrom(error)
  return reason
    ? t(`guided.error.${reason}`, { defaultValue: t("aiRun.startError") })
    : t("aiRun.startError")
}

/** A centered conversation column with a soft backdrop. */
function GuidedShell({ children }: { children: ReactNode }) {
  return (
    <div
      className="mx-auto flex w-full max-w-3xl flex-col gap-6 py-2"
      data-section="guided-run"
    >
      {children}
    </div>
  )
}

/** Compact run status strip at the top of the conversation. */
function RunHeader({ run }: { run: AiRun }) {
  const { t } = useTranslation("pipeline")
  const statusLabel = t(`aiRun.status.${run.status}`, { defaultValue: run.status })
  const terminal = isTerminal(run)
  return (
    <div
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-muted/30 px-4 py-2 text-sm"
      data-section="guided-run-header"
    >
      <span className="inline-flex items-center gap-2">
        <span
          className={cn(
            "inline-block size-2 rounded-full",
            terminal ? "bg-team-blue" : "animate-pulse bg-team-purple",
          )}
        />
        <span className="font-medium">{statusLabel}</span>
      </span>
      <span className="text-muted-foreground">
        {t("aiRun.run.iterationsUsed")}: {run.iterations_used} / {run.max_iterations}
      </span>
    </div>
  )
}

/** One approval pause: the robot's line followed by the artifact and its actions. */
function PauseTurn({
  line,
  phase,
  children,
}: {
  line: string
  phase: string
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-4 animate-in fade-in slide-in-from-bottom-2 fill-mode-both">
      <OrchestratorBubble mood="talking">{line}</OrchestratorBubble>
      <div className="ml-14 flex flex-col gap-3" data-section="guided-artifact" data-phase={phase}>
        {children}
      </div>
    </div>
  )
}

function ApproveButton({
  label,
  busy,
  onClick,
}: {
  label: string
  busy: boolean
  onClick: () => void
}) {
  return (
    <Button onClick={onClick} disabled={busy} className="px-6">
      {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <CheckCircle2 className="size-4" aria-hidden />}
      {label}
    </Button>
  )
}

/**
 * The execution artifact: a neutral report that the offensive emulation ran
 * against the targets and the real result was recorded. It carries NO verdict —
 * the judgement belongs to the evaluation that follows (Req. 2.5, 7.3).
 */
function ExecutionReport({ run }: { run: AiRun }) {
  const { t } = useTranslation("pipeline")
  return (
    <div
      className="ml-14 flex flex-col gap-2 rounded-xl border bg-muted/30 p-4"
      data-section="guided-execution"
    >
      <p className="text-sm text-muted-foreground">{t("guided.executionReport")}</p>
      {run.error ? <p className="text-sm text-team-red">{run.error}</p> : null}
    </div>
  )
}

/**
 * The evaluation artifact: the evaluator's verdict as the closing turn, with the
 * achieved coverage (Req. 22) and the closing summary.
 */
function EvaluationVerdict({ run }: { run: AiRun }) {
  const { t } = useTranslation("pipeline")
  const success = run.final_verdict_success
  const failed = run.status === "failed" || run.status === "aborted"
  const achieved = run.coverage?.achieved_coverage

  return (
    <div
      className={cn(
        "ml-14 flex flex-col gap-2 rounded-xl border p-4",
        success ? "border-team-blue/40 bg-team-blue/5" : failed ? "border-team-red/40 bg-team-red/5" : "bg-muted/30",
      )}
      data-section="guided-verdict"
    >
      <div className="flex items-center gap-2">
        {success ? (
          <CheckCircle2 className="size-5 text-team-blue" aria-hidden />
        ) : failed ? (
          <XCircle className="size-5 text-team-red" aria-hidden />
        ) : null}
        <span className="font-semibold">
          {success
            ? t("aiRun.verdict.approved")
            : failed
              ? t("aiRun.verdict.rejected")
              : t("aiRun.verdict.pending")}
        </span>
      </div>
      {typeof achieved === "number" ? (
        <p className="text-sm font-medium" data-section="achieved-coverage">
          {t("guided.coverage.achieved", { coverage: Math.round(achieved) })}
        </p>
      ) : null}
      <p className="text-sm text-muted-foreground">
        {t("guided.finalSummary", { iterations: run.iterations_used })}
      </p>
    </div>
  )
}

/** The robot avatar + a speech bubble with its current line. */
function OrchestratorBubble({
  mood,
  pulse = false,
  children,
}: {
  mood: OrchestratorMood
  pulse?: boolean
  children: ReactNode
}) {
  return (
    <div className="flex items-start gap-3" data-section="orchestrator-bubble">
      <OrchestratorAvatar mood={mood} pulse={pulse} className="size-12 shrink-0" />
      <div className="relative mt-1 flex-1 rounded-2xl rounded-tl-sm border bg-card px-4 py-3 text-sm shadow-sm">
        <span className="absolute -left-1.5 top-3 size-3 rotate-45 border-b-0 border-r-0 border bg-card" />
        {children}
      </div>
    </div>
  )
}

/** Delay before the "this can take a while" reassurance appears. */
const STILL_WORKING_DELAY_MS = 20_000

/**
 * The slow-phase hint per phase, limited to what is known to be in flight: the
 * operationalization and flow nodes wait on a language model call, the environment
 * node waits on the lab containers (it makes no language model call). Other
 * phases show only the elapsed time.
 */
const STILL_WORKING_HINTS: Partial<Record<string, string>> = {
  operationalization: "guided.stillWorkingHint",
  flow: "guided.stillWorkingHint",
  environment: "guided.stillWorkingHintEnvironment",
}

/**
 * The phase-specific waiting text with a spinner. After a while in the same
 * phase it adds a reassurance line; the timer restarts when the phase changes.
 */
function WaitingMessage({ phase }: { phase: string }) {
  const { t } = useTranslation("pipeline")
  const [slow, setSlow] = useState<string | null>(null)
  // Wall-clock seconds since this phase started waiting. Keyed by phase so a phase
  // change restarts the count without a synchronous reset inside the effect.
  const [elapsed, setElapsed] = useState<{ phase: string; seconds: number }>({ phase, seconds: 0 })

  useEffect(() => {
    const startedAt = Date.now()
    const timer = setTimeout(() => setSlow(phase), STILL_WORKING_DELAY_MS)
    const ticker = setInterval(
      () => setElapsed({ phase, seconds: Math.floor((Date.now() - startedAt) / 1000) }),
      1000,
    )
    return () => {
      clearTimeout(timer)
      clearInterval(ticker)
    }
  }, [phase])
  const seconds = elapsed.phase === phase ? elapsed.seconds : 0

  return (
    <div className="flex flex-col gap-1" role="status" data-section="phase-waiting" data-phase={phase}>
      <span className="inline-flex items-center gap-2">
        <Loader2 className="size-4 shrink-0 animate-spin" aria-hidden />
        {t(`guided.waiting.${phase}`, { defaultValue: t("guided.working") })}
      </span>
      <span className="text-xs tabular-nums text-muted-foreground" data-section="elapsed-time">
        {t("guided.waiting.elapsed", { seconds })}
      </span>
      {slow === phase && STILL_WORKING_HINTS[phase] ? (
        <span className="text-xs text-muted-foreground" data-section="still-working-hint">
          {t(STILL_WORKING_HINTS[phase])}
        </span>
      ) : null}
    </div>
  )
}

/** A small "thinking" indicator shown while an artifact is being produced. */
function ThinkingDots() {
  return (
    <div className="ml-14 flex items-center gap-1.5" aria-hidden data-section="thinking-dots">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="size-2 animate-bounce rounded-full bg-team-purple/60"
          style={{ animationDelay: `${i * 0.15}s` }}
        />
      ))}
      <ChevronDown className="ml-1 size-4 animate-pulse text-muted-foreground" />
    </div>
  )
}
