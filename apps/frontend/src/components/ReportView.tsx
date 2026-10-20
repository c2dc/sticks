import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { EmptyNote, StageSection } from "@/components/StageSection"
import {
  getReport,
  type DeprovisioningResult,
  type SufficiencyMetrics,
  type TechniqueMetric,
  type ExecutionReport,
} from "@/lib/stages"

/**
 * ReportView — consolidated Stage 6 report (Execution, measurement and report)
 * — Task 14.3, Req. 4.1, 10.6, 10.7, 15.7.
 *
 * Reads the `ExecutionReport` (`GET /api/campaigns/{campaign}/report`) and shows
 * the procedural-sufficiency measurement:
 *
 * - **Success rate** (successful techniques / DAG total — Req. 15.1) and
 *   **procedural coverage** ∈ [0, 1] (Req. 15.3), formatted as percentages.
 * - **Total time** of execution (Req. 4.1) and the **failed-node count**
 *   (Req. 5.2) — the nodes themselves are surfaced live in Processing, not here.
 *
 * After the metrics it renders the **deprovisioning report** last (Req. 7.2,
 * 7.3): whether the environment teardown succeeded and, on failure, the
 * `remaining_resources` left behind — defensive to an absent structured verdict.
 *
 * It is read-only and **gated** by `executionFinished` (Req. 3.1, 7.1, 8.2):
 * while the Stage 6 execution has not terminated the view neither fetches
 * `GET /report` nor renders any metrics/deprovisioning — it stays a neutral,
 * empty placeholder. The report is fetched only once `executionFinished`
 * becomes `true`, and then it handles loading/error like its sibling screens.
 * A case without execution returns zeroed metrics, shown without error.
 */
export function ReportView({
  campaignId,
  executionFinished,
}: {
  campaignId: string
  executionFinished: boolean
}) {
  const { t } = useTranslation("pipeline")
  const [report, setReport] = useState<ExecutionReport | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    // Gate the fetch on the execution being finished (Req. 3.1, 3.2, 7.1, 8.2):
    // do not touch `GET /report` while the run is still in progress.
    if (!executionFinished) {
      setReport(null)
      setLoadError(null)
      return
    }
    let active = true
    setReport(null)
    setLoadError(null)
    getReport(campaignId)
      .then((data) => {
        if (active) setReport(data)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [campaignId, executionFinished, t])

  // While the execution has not finished, render a neutral empty state — no
  // metrics, no deprovisioning, no error (Req. 3.1, 7.1, 8.2).
  if (!executionFinished) return null

  if (loadError) return <EmptyNote>{loadError}</EmptyNote>
  if (!report) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  // Consume the report as **final**: normalize the metrics so no technique is
  // ever rendered in a non-terminal state and the displayed counts respect the
  // invariants of Req. 3.4 — the sum of techniques per state equals the total,
  // and the failed-node count equals the number of techniques in `falha`. The
  // backend `ReportBuilder` already guarantees this; this guard keeps the view
  // consistent even if a stale/divergent payload ever reaches it (Req. 3.5).
  const metrics = toFinalMetrics(report.metrics)

  return (
    <div className="flex flex-col gap-4" data-section="report">
      {/* Procedural-sufficiency metrics (Req. 15.1, 15.3, 15.5). */}
      <StageSection
        data-section="metricas"
        label={t("metricas.titulo")}
        kind="purple"
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label={t("metricas.taxaSucesso")}
            value={formatPercent(metrics.success_rate)}
            detail={`${metrics.successful_techniques} / ${metrics.total_dag_techniques}`}
          />
          <MetricCard
            label={t("metricas.coberturaProcedural")}
            value={formatPercent(metrics.procedural_coverage)}
          />
          <MetricCard
            label={t("metricas.tempoTotal")}
            value={formatTime(metrics.total_time_s)}
          />
          <MetricCard
            label={t("metricas.nosFalhos.titulo")}
            value={String(metrics.failed_nodes.length)}
          />
        </div>
      </StageSection>

      {/* Deprovisioning report, rendered last (Req. 7.2, 7.3). */}
      <DeprovisioningSection deprovisioning={report.deprovisioning} />
    </div>
  )
}

/**
 * Environment-teardown report (Req. 7.2, 7.3), rendered after the metrics.
 *
 * The teardown is always attempted at the end of Stage 6. This block states
 * whether it succeeded (`deprovisioned === true`) or not, and on failure lists
 * the `remaining_resources` that were left behind. It is **defensive** when the
 * structured `deprovisioning` verdict is absent (the backend omits it until the
 * run finishes): in that case it shows the neutral "not deprovisioned" state
 * with no remaining resources and without surfacing an error.
 */
function DeprovisioningSection({
  deprovisioning,
}: {
  deprovisioning?: DeprovisioningResult
}) {
  const { t } = useTranslation("pipeline")
  const deprovisioned = deprovisioning?.deprovisioned === true
  const remaining = deprovisioning?.remaining_resources ?? []

  return (
    <StageSection
      data-section="desprovisionamento"
      label={t("relatorio.titulo")}
      kind={deprovisioned ? "blue" : "red"}
    >
      <span className="text-sm font-medium">
        {deprovisioned
          ? t("relatorio.ambiente.desprovisionado")
          : t("relatorio.ambiente.naoDesprovisionado")}
      </span>
      {!deprovisioned && remaining.length > 0 ? (
        <span className="text-xs text-muted-foreground">
          {t("relatorio.ambiente.recursosRemanescentes", {
            recursos: remaining.join(", "),
          })}
        </span>
      ) : null}
    </StageSection>
  )
}

/** One headline metric: a big value, a label and an optional detail line. */
function MetricCard({
  label,
  value,
  detail,
}: {
  label: string
  value: string
  detail?: string
}) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md border p-3">
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="text-2xl font-semibold tabular-nums">{value}</span>
      {detail ? (
        <span className="text-xs text-muted-foreground">{detail}</span>
      ) : null}
    </div>
  )
}

/**
 * Normalize the sufficiency metrics into a **final** report (Req. 3.4).
 *
 * A final report admits only terminal per-technique states and must satisfy the
 * consistency invariants of Req. 3.4. The backend `ReportBuilder` already builds
 * such a report, but this view defends against any stale or divergent payload
 * (Req. 3.5) by recomputing the metrics from the per-technique entries:
 *
 * - **(d)** every technique is forced to a terminal state — `sucesso` stays
 *   `sucesso` and anything else (`falha`, `pendente`, `em_execucao`) becomes
 *   `falha` (a technique that never completed did not succeed), so a non-terminal
 *   status is never rendered;
 * - **(b)** `successful_techniques + (total − successful) === total_dag_techniques`
 *   because every technique lands in exactly one terminal bucket, and the total
 *   is pinned to the number of per-technique entries;
 * - **(c)** `failed_nodes` is rebuilt from exactly the techniques normalized to
 *   `falha`, so its length equals the failure count;
 * - **(a)** `success_rate` is recomputed from the counts and `success_verdict` is
 *   derived deterministically from it (true iff there is at least one technique
 *   and every technique succeeded).
 *
 * `procedural_coverage` and `total_time_s` are carried through unchanged — they
 * are not part of the Req. 3.4 invariants.
 */
function toFinalMetrics(metrics: SufficiencyMetrics): SufficiencyMetrics {
  const perTechnique: TechniqueMetric[] = []
  const failedNodes: string[] = []
  let successful = 0

  for (const metric of metrics.per_technique) {
    if (metric.status === "sucesso") {
      successful += 1
      perTechnique.push({ ...metric, status: "sucesso" })
    } else {
      // Normalize non-success (incl. non-terminal) to `falha` so a final report
      // never carries a pending/running technique (d) and the failed-node count
      // tracks the failures exactly (c).
      perTechnique.push({ ...metric, status: "falha" })
      failedNodes.push(metric.node_id ?? metric.technique_id ?? "")
    }
  }

  const total = perTechnique.length
  const successRate = total > 0 ? successful / total : 0

  return {
    ...metrics,
    total_dag_techniques: total,
    successful_techniques: successful,
    success_rate: successRate,
    per_technique: perTechnique,
    failed_nodes: failedNodes,
    // Deterministic verdict from the computed success rate (a): a run succeeds
    // iff it has at least one technique and every technique succeeded.
    success_verdict: total > 0 && successRate >= 1,
  }
}

/**
 * Format a ratio in [0, 1] as an integer-ish percentage. Values outside the
 * range are clamped so a malformed metric never renders a nonsensical figure.
 */
function formatPercent(value: number): string {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
  return `${Math.round(clamped * 100)}%`
}

/** Format a duration in seconds; `null`/`undefined` becomes an em dash. */
function formatTime(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—"
  return `${Math.round(seconds * 10) / 10}s`
}
