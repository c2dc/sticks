import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react"

import { Button } from "@/components/ui/button"
import { EmptyNote, StageSection } from "@/components/StageSection"
import {
  getAiRun,
  startAiRun,
  type AiEvaluation,
  type AiIteration,
  type AiRun,
  type AiRunDetail,
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
 * reaches a terminal status (`concluido` / `falhou` / `abortado`), after one
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
const TERMINAL_STATUSES = new Set(["concluido", "falhou", "abortado"])

function isTerminal(run: AiRun | null | undefined): boolean {
  return run != null && TERMINAL_STATUSES.has(run.status)
}

export function AiRunView({ campaignId }: { campaignId: string }) {
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
        label={t("aiRun.titulo")}
        kind="purple"
        description={t("aiRun.descricao")}
      >
        <div className="flex items-center gap-3">
          <Button type="button" onClick={() => void start()} disabled={starting}>
            {starting ? (
              <Loader2 className="size-4 animate-spin" aria-hidden />
            ) : null}
            {starting ? t("aiRun.iniciando") : t("aiRun.iniciar")}
          </Button>
        </div>
        {startError ? <EmptyNote>{startError}</EmptyNote> : null}
      </StageSection>

      {detail ? (
        <>
          <RunSummary run={detail.run} />
          {loadError ? <EmptyNote>{loadError}</EmptyNote> : null}
          <IterationsSection iterations={detail.iterations} />
        </>
      ) : !startError ? (
        <EmptyNote>{t("aiRun.semExecucao")}</EmptyNote>
      ) : null}
    </div>
  )
}

/** Run summary: status, iterations used / max and the overall final verdict. */
function RunSummary({ run }: { run: AiRun }) {
  const { t } = useTranslation("pipeline")
  const statusLabel = t(`aiRun.status.${run.status}`, { defaultValue: run.status })

  return (
    <StageSection data-section="ai-run-summary" label={t("aiRun.run.titulo")} kind="neutral">
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryItem label={t("aiRun.run.status")} value={statusLabel} />
        <SummaryItem
          label={t("aiRun.run.iteracoesUsadas")}
          value={String(run.iterations_used)}
        />
        <SummaryItem
          label={t("aiRun.run.maxIteracoes")}
          value={String(run.max_iterations)}
        />
        <SummaryItem
          label={t("aiRun.run.veredito")}
          value={<VerdictBadge success={run.final_verdict_success} />}
        />
      </dl>
    </StageSection>
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

/** The list of iterations (index + refined stage) each with its evaluator verdict. */
function IterationsSection({ iterations }: { iterations: AiIteration[] }) {
  const { t } = useTranslation("pipeline")

  return (
    <StageSection data-section="ai-run-iterations" label={t("aiRun.iteracoes.titulo")} kind="blue">
      {iterations.length === 0 ? (
        <EmptyNote>{t("aiRun.iteracoes.vazio")}</EmptyNote>
      ) : (
        <ol className="flex flex-col gap-3">
          {iterations.map((iteration) => (
            <IterationCard key={iteration.id} iteration={iteration} />
          ))}
        </ol>
      )}
    </StageSection>
  )
}

/** One iteration: index, refined stage and its evaluator verdict (when present). */
function IterationCard({ iteration }: { iteration: AiIteration }) {
  const { t } = useTranslation("pipeline")
  const refined = iteration.refined_stage

  return (
    <li
      className="flex flex-col gap-2 rounded-md border p-3"
      data-section="ai-run-iteration"
      data-index={iteration.index}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold">
          {t("aiRun.iteracoes.indice", { indice: iteration.index + 1 })}
        </span>
        <span className="text-xs text-muted-foreground">
          {t("aiRun.iteracoes.estagioRefinado")}:{" "}
          {refined ? refined : t("aiRun.iteracoes.semEstagioRefinado")}
        </span>
      </div>
      {iteration.notes ? (
        <p className="text-xs text-muted-foreground">{iteration.notes}</p>
      ) : null}
      <EvaluationBlock evaluation={iteration.evaluation} />
    </li>
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
        {t("aiRun.avaliacao.ausente")}
      </p>
    )
  }

  const decisionLabel = t(`aiRun.decisao.${evaluation.decision}`, {
    defaultValue: evaluation.decision,
  })

  return (
    <div
      className="flex flex-col gap-2 rounded-md border bg-muted/30 p-2"
      data-section="ai-run-evaluation"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-team-purple">
          {t("aiRun.avaliacao.titulo")}
        </span>
        <VerdictBadge success={evaluation.verdict_success} />
      </div>
      <dl className="grid gap-2 sm:grid-cols-3">
        <SummaryItem label={t("aiRun.avaliacao.decisao")} value={decisionLabel} />
        <SummaryItem
          label={t("aiRun.avaliacao.taxaSucesso")}
          value={formatPercent(evaluation.success_rate)}
        />
        <SummaryItem
          label={t("aiRun.avaliacao.coberturaProcedural")}
          value={formatPercent(evaluation.procedural_coverage)}
        />
      </dl>
      {evaluation.rationale ? (
        <div className="flex flex-col gap-0.5">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("aiRun.avaliacao.justificativa")}
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
        {t("aiRun.veredito.pendente")}
      </span>
    )
  }
  if (success) {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm font-medium text-team-blue">
        <CheckCircle2 className="size-4" aria-hidden />
        {t("aiRun.veredito.sucesso")}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm font-medium text-team-red">
      <XCircle className="size-4" aria-hidden />
      {t("aiRun.veredito.falha")}
    </span>
  )
}

/** Format a ratio in [0, 1] as an integer percentage; non-finite → 0%. */
function formatPercent(value: number): string {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
  return `${Math.round(clamped * 100)}%`
}
