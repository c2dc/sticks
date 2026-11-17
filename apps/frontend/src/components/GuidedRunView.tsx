import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"
import { useTranslation } from "react-i18next"
import {
  CheckCircle2,
  ChevronDown,
  Loader2,
  Sparkles,
  XCircle,
} from "lucide-react"

import { OrchestratorAvatar, type OrchestratorMood } from "@/components/OrchestratorAvatar"
import { AgentTracePanel } from "@/components/AgentTracePanel"
import { FlowPhaseView } from "@/components/FlowPhaseView"
import { OperationalizationScreen } from "@/components/OperationalizationScreen"
import { Button } from "@/components/ui/button"
import { EmptyNote } from "@/components/PhaseSection"
import { campaignDisplayName } from "@/lib/campaign"
import { cn } from "@/lib/utils"
import {
  getAiRun,
  startAiRun,
  type AiEvaluation,
  type AiIteration,
  type AiRun,
  type AiRunDetail,
  type AiPhaseOutput,
} from "@/lib/ai"

/**
 * GuidedRunView — the conversational, orchestrator-led Run screen (Layer 2).
 *
 * Instead of a dense dashboard, the Run is narrated by an orchestrator robot
 * that reveals one phase at a time and asks the Researcher's permission to
 * proceed:
 *
 *   1. Operationalization   — right after the campaign is chosen, it lists the
 *                             commands available for each technique. The AI run
 *                             starts only once the Researcher reviews them.
 *   2. Attack flow          — it shows the attack flow it generated (with its
 *                             provenance) and asks "shall I proceed?".
 *   3. Environment          — then the isolated environment it designed.
 *   4. Execution            — it runs the offensive emulation against the targets
 *                             and reports that the real result was recorded.
 *   5. Evaluation           — finally, as its OWN phase (plan → act → evaluate),
 *                             the evaluator observes the execution result and
 *                             reports the verdict, distinct from the execution
 *                             step (Req. 2.5, 7.3).
 *
 * Under the hood this drives the SAME backend run as `AiRunView`: after the
 * Researcher reviews the operationalization, it starts a run, polls `getAiRun` (≤5s) and reads the per-phase Agent_Output artifacts of
 * the latest iteration. The "phases" are a presentational projection of those
 * phase outputs — the robot simply gates how many are revealed, so the
 * Researcher is conducted through the multi-agent pipeline rather than shown
 * everything at once.
 */

const POLL_INTERVAL_MS = 3000
const TERMINAL_STATUSES = new Set(["completed", "failed", "aborted"])

function isTerminal(run: AiRun | null | undefined): boolean {
  return run != null && TERMINAL_STATUSES.has(run.status)
}

/**
 * The ordered conductor phases the robot reveals, mapped to the semantic,
 * number-free backend `AgentPhase` values: `operationalization → flow →
 * environment → execution → evaluation`, without any numbered labels (Req. 2.2,
 * 5.7, 6.1).
 *
 * `execution` and `evaluation` are two distinct, ordered items following the
 * literature's plan → act → evaluate pattern: `execution` runs the offensive
 * emulation and `evaluation` observes the real result and reports the verdict,
 * revealed as its own turn after execution (Req. 2.1, 2.5, 7.3).
 */
const PHASE_ORDER = [
  "operationalization",
  "flow",
  "environment",
  "execution",
  "evaluation",
] as const
type Phase = (typeof PHASE_ORDER)[number]

/**
 * The phases whose artifact is derived from the terminal run state (the verdict)
 * rather than from a per-phase Agent_Output row: `execution` (the emulation ran)
 * and `evaluation` (the evaluator verdict).
 */
const TERMINAL_PHASES = new Set<Phase>(["execution", "evaluation"])

export function GuidedRunView({
  campaignId,
  campaignName,
  version,
}: {
  /** The chosen campaign key (`campaign_key` of the STIX listing). */
  campaignId: string
  /** The campaign's readable name; falls back to a humanized key. */
  campaignName?: string
  /** The chosen STIX base version, used by the operationalization listing. */
  version: string
}) {
  const { t } = useTranslation("pipeline")
  const displayName = campaignDisplayName(campaignName, campaignId)

  const [detail, setDetail] = useState<AiRunDetail | null>(null)
  const [runId, setRunId] = useState<number | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)

  // How far the Researcher has approved the robot to reveal. Index into
  // PHASE_ORDER: 0 means "operationalization shown, awaiting the review that starts the run".
  const [approvedUpTo, setApprovedUpTo] = useState(0)

  const start = useCallback(async () => {
    setStarting(true)
    setStartError(null)
    setDetail(null)
    setApprovedUpTo(0)
    try {
      const run = await startAiRun(campaignId)
      setRunId(run.id)
      setDetail({ run, iterations: [] })
      // The operationalization was reviewed: reveal the flow phase.
      setApprovedUpTo(1)
    } catch {
      setStartError(t("aiRun.startError"))
    } finally {
      setStarting(false)
    }
  }, [campaignId, t])

  // Poll while the run is non-terminal; one final read on terminal.
  useEffect(() => {
    if (runId == null) return
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined

    const poll = async () => {
      const id = runId
      if (!active) return
      try {
        const next = await getAiRun(campaignId, id)
        if (!active) return
        setDetail(next)
        if (!isTerminal(next.run)) timer = setTimeout(poll, POLL_INTERVAL_MS)
      } catch {
        if (!active) return
        timer = setTimeout(poll, POLL_INTERVAL_MS)
      }
    }

    void poll()
    return () => {
      active = false
      if (timer) clearTimeout(timer)
    }
  }, [campaignId, runId])

  // The latest iteration drives the conversation (the newest plan the robot
  // produced). Its phase outputs are keyed by phase for phase lookup.
  const latest: AiIteration | undefined = detail?.iterations.at(-1)
  const outputsByPhase = useMemo(() => {
    const map = new Map<string, AiPhaseOutput>()
    for (const out of latest?.phase_outputs ?? []) map.set(out.phase, out)
    return map
  }, [latest])

  // Not started yet → the robot's welcome, the operationalization listing the
  // Researcher reviews, and the single "start" action that begins the AI run.
  if (!detail) {
    return (
      <GuidedShell>
        <OrchestratorBubble mood={starting ? "thinking" : "talking"} pulse>
          {starting ? t("guided.thinking") : t("guided.greeting")}
        </OrchestratorBubble>
        <div className="ml-14" data-section="guided-artifact" data-phase="operationalization">
          <OperationalizationScreen
            version={version}
            campaignKey={campaignId}
            campaignName={displayName}
          />
        </div>
        <div className="flex flex-col items-center gap-3">
          <Button size="lg" onClick={() => void start()} disabled={starting} className="px-8">
            {starting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Sparkles className="size-4" aria-hidden />}
            {starting ? t("guided.preparing") : t("guided.start")}
          </Button>
          {startError ? <EmptyNote>{startError}</EmptyNote> : null}
        </div>
      </GuidedShell>
    )
  }

  const run = detail.run
  const running = !isTerminal(run)
  const noPlanYet = !latest || latest.phase_outputs.length === 0

  // While the first plan is still being produced, the robot is "thinking".
  if (noPlanYet && running) {
    return (
      <GuidedShell>
        <RunHeader run={run} />
        <OrchestratorBubble mood="thinking" pulse>
          {t("guided.generatingFlow")}
        </OrchestratorBubble>
        <ThinkingDots />
      </GuidedShell>
    )
  }

  // Terminal verdict closes the conversation.
  const finished = isTerminal(run)

  return (
    <GuidedShell>
      <RunHeader run={run} />

      {/* Each revealed phase as a conversation turn. The robot speaks, shows the
          artifact, then asks to proceed; approving reveals the next phase. */}
      <div className="flex flex-col gap-6" data-section="guided-phases">
        {PHASE_ORDER.map((phase, index) => {
          if (index > approvedUpTo) return null
          const isTerminalPhase = TERMINAL_PHASES.has(phase)
          const output = isTerminalPhase ? undefined : outputsByPhase.get(phase)
          // A phase is "ready" once its artifact exists (or, for the terminal
          // execution/evaluation phases, once the run reaches a terminal state).
          // The operationalization listing is read-only and always ready.
          const ready =
            phase === "operationalization"
              ? true
              : isTerminalPhase
                ? finished
                : Boolean(output)
          const isCurrent = index === approvedUpTo

          return (
            <PhaseTurn
              key={phase}
              phase={phase}
              campaignId={campaignId}
              campaignName={displayName}
              version={version}
              output={output}
              evaluation={latest?.evaluation}
              ready={ready}
              running={running}
              finished={finished}
              run={run}
              isCurrent={isCurrent}
              isLastPhase={index === PHASE_ORDER.length - 1}
              onApprove={() => setApprovedUpTo((v) => Math.min(PHASE_ORDER.length - 1, v + 1))}
            />
          )
        })}
      </div>
    </GuidedShell>
  )
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

/**
 * One conversation turn for a phase: the robot's line + the artifact it
 * produced, followed by the "can I proceed?" prompt when it is the current
 * (frontier) phase and the artifact is ready.
 */
function PhaseTurn({
  phase,
  campaignId,
  campaignName,
  version,
  output,
  evaluation,
  ready,
  running,
  finished,
  run,
  isCurrent,
  isLastPhase,
  onApprove,
}: {
  phase: Phase
  campaignId: string
  campaignName: string
  version: string
  output?: AiPhaseOutput
  evaluation?: AiEvaluation | null
  ready: boolean
  running: boolean
  finished: boolean
  run: AiRun
  isCurrent: boolean
  isLastPhase: boolean
  onApprove: () => void
}) {
  const { t } = useTranslation("pipeline")

  // The robot's opening line for the phase.
  const line = t(`guided.speech.${phase}`, { campaign: campaignName })
  // Only the `evaluation` phase carries the verdict, so its mood reflects the
  // outcome; `execution` reports the run, not the judgement.
  const mood: OrchestratorMood =
    phase === "evaluation" && finished
      ? run.final_verdict_success
        ? "happy"
        : "alert"
      : "talking"

  return (
    <div className="flex flex-col gap-4 animate-in fade-in slide-in-from-bottom-2 fill-mode-both">
      <OrchestratorBubble mood={ready ? mood : "thinking"}>
        {ready ? line : t("guided.working")}
      </OrchestratorBubble>

      {/* The artifact the robot produced for this phase. `operationalization`
          lists the commands per technique; `flow` shows the attack flow with its
          provenance; `execution` reports that the emulation ran; `evaluation`
          carries the evaluator verdict; the other phases render their
          Agent_Output artifact. */}
      {ready ? (
        phase === "operationalization" ? (
          <div className="ml-14" data-section="guided-artifact" data-phase={phase}>
            <OperationalizationScreen
              version={version}
              campaignKey={campaignId}
              campaignName={campaignName}
            />
          </div>
        ) : phase === "flow" ? (
          <div className="ml-14 flex flex-col gap-3" data-section="guided-artifact" data-phase={phase}>
            <FlowPhaseView campaign={campaignId} />
            {output ? <PhaseArtifact phase={phase} output={output} bare /> : null}
          </div>
        ) : phase === "execution" ? (
          <ExecutionReport run={run} />
        ) : phase === "evaluation" ? (
          <EvaluationVerdict run={run} evaluation={evaluation} />
        ) : output ? (
          <PhaseArtifact phase={phase} output={output} />
        ) : null
      ) : (
        <ThinkingDots />
      )}

      {/* "Shall I proceed?" — only on the current frontier phase, once ready, and
          never on the final (execution) phase. */}
      {isCurrent && ready && !isLastPhase && phase !== "operationalization" ? (
        <div
          className="flex flex-col items-center gap-2 rounded-xl border border-team-purple/30 bg-team-purple/5 p-4"
          data-section="guided-proceed"
        >
          <p className="text-center text-sm font-medium">{t("guided.proceedQuestion")}</p>
          <Button onClick={onApprove} className="px-6">
            <CheckCircle2 className="size-4" aria-hidden />
            {t("guided.proceed")}
          </Button>
          {running ? (
            <p className="text-xs text-muted-foreground">{t("guided.stillWorking")}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/**
 * The artifact body for a flow / environment phase: the agent trace panel. With
 * `bare` it renders just the panel, for use inside another artifact wrapper.
 */
function PhaseArtifact({
  phase,
  output,
  bare = false,
}: {
  phase: Phase
  output: AiPhaseOutput
  bare?: boolean
}) {
  const { t } = useTranslation("pipeline")
  const panel = (
    <AgentTracePanel
      phaseName={t(`aiRun.phaseName.${output.phase}`, { defaultValue: output.phase })}
      phaseOutput={output}
    />
  )
  if (bare) return panel
  return (
    <div className="ml-14" data-section="guided-artifact" data-phase={phase}>
      {panel}
    </div>
  )
}

/**
 * The `execution` phase artifact: a neutral report that the offensive emulation
 * ran against the targets and the real result was recorded. It carries NO
 * verdict — the judgement belongs to the distinct `evaluation` phase that
 * follows (Req. 2.5, 7.3).
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
 * The `evaluation` phase artifact: the evaluator's verdict rendered as the
 * closing conversation turn, observing the real execution result. Shows the
 * final pass/fail verdict, the evaluator's rationale for the latest iteration
 * when present, and the closing summary (Req. 2.2, 2.5, 7.3).
 */
function EvaluationVerdict({
  run,
  evaluation,
}: {
  run: AiRun
  evaluation?: AiEvaluation | null
}) {
  const { t } = useTranslation("pipeline")
  const success = run.final_verdict_success
  const failed = run.status === "failed" || run.status === "aborted"

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
      {evaluation?.rationale ? (
        <div className="flex flex-col gap-0.5">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("guided.evaluationRationale")}
          </span>
          <p className="whitespace-pre-wrap text-sm text-muted-foreground">
            {evaluation.rationale}
          </p>
        </div>
      ) : null}
      {run.error ? <p className="text-sm text-team-red">{run.error}</p> : null}
      <p className="text-sm text-muted-foreground">
        {t("guided.finalSummary", {
          iteracoes: run.iterations_used,
        })}
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
