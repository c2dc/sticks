import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, Clock, Target, XCircle } from "lucide-react"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { cn } from "@/lib/utils"
import {
  getReport,
  type AbilityResultStatus,
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
 * - Overall success **verdict** (Req. 15.6) and the environment deprovisioning
 *   flag (Req. 12).
 * - **Success rate** (successful techniques / DAG total — Req. 15.1) and
 *   **procedural coverage** ∈ [0, 1] (Req. 15.3), formatted as percentages.
 * - **Total time** of execution (Req. 15.5) and per-technique time.
 * - **Failed nodes** of the DAG (Req. 10.6, 15.4).
 * - **Per-technique telemetry** (status, failed step, time, observed signals —
 *   Req. 15.2, 15.4, 15.7).
 *
 * It is read-only: loads the report on mount and handles loading/error like its
 * sibling screens. A case without execution returns zeroed metrics, shown
 * without error.
 */
export function ReportView({ campaignId }: { campaignId: string }) {
  const { t } = useTranslation("pipeline")
  const [report, setReport] = useState<ExecutionReport | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
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
  }, [campaignId, t])

  if (loadError) return <EmptyNote>{loadError}</EmptyNote>
  if (!report) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  const {
    metrics,
    environment_deprovisioned: environmentDeprovisioned,
    generated_at: generatedAt,
  } = report

  return (
    <div className="flex flex-col gap-4" data-section="relatorio">
      {/* Overall verdict + environment deprovisioning. */}
      <StageSection
        data-section="veredito"
        label={t("relatorio.titulo")}
        kind={metrics.success_verdict ? "blue" : "red"}
        description={`${t("relatorio.geradoEm")}: ${formatDate(generatedAt)}`}
      >
        <VerdictBadge success={metrics.success_verdict} />
        <DeprovisioningBadge deprovisioned={environmentDeprovisioned} />
      </StageSection>

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

      {/* Failed DAG nodes (Req. 10.6, 15.4). */}
      <StageSection
        data-section="nos-falhos"
        label={t("metricas.nosFalhos.titulo")}
        kind="red"
      >
        {metrics.failed_nodes.length === 0 ? (
          <EmptyNote>{t("metricas.nosFalhos.vazio")}</EmptyNote>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {metrics.failed_nodes.map((node, index) => (
              <li
                key={index}
                className="inline-flex items-center gap-1 rounded-md border border-team-red/30 bg-team-red/5 px-2 py-0.5 text-xs text-team-red"
              >
                <XCircle className="size-3" aria-hidden />
                <code className="font-mono">{node}</code>
              </li>
            ))}
          </ul>
        )}
      </StageSection>

      {/* Per-technique telemetry (Req. 15.2, 15.4, 15.7). */}
      <StageSection
        data-section="telemetria"
        label={t("relatorio.telemetria.titulo")}
        kind="neutral"
      >
        {metrics.per_technique.length === 0 ? (
          <EmptyNote>{t("relatorio.telemetria.vazio")}</EmptyNote>
        ) : (
          <ul className="flex flex-col gap-2">
            {metrics.per_technique.map((metric, index) => (
              <TechniqueCard key={index} metric={metric} />
            ))}
          </ul>
        )}
      </StageSection>
    </div>
  )
}

/** Overall success verdict (Req. 15.6). */
function VerdictBadge({ success }: { success: boolean }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1" role="status">
      <span
        className={cn(
          "inline-flex w-fit items-center gap-1.5 rounded-full px-2.5 py-1 text-sm font-medium",
          success
            ? "bg-team-blue/10 text-team-blue"
            : "bg-team-red/10 text-team-red",
        )}
        data-veredito={success ? "sucesso" : "falha"}
      >
        {success ? (
          <CheckCircle2 className="size-4" aria-hidden />
        ) : (
          <XCircle className="size-4" aria-hidden />
        )}
        {success
          ? t("relatorio.veredito.sucesso")
          : t("relatorio.veredito.falha")}
      </span>
    </div>
  )
}

/** Environment teardown flag (Req. 12). */
function DeprovisioningBadge({
  deprovisioned,
}: {
  deprovisioned: boolean
}) {
  const { t } = useTranslation("pipeline")
  return (
    <p className="text-xs text-muted-foreground" data-desprovisionado={deprovisioned}>
      {deprovisioned
        ? t("relatorio.ambiente.desprovisionado")
        : t("relatorio.ambiente.naoDesprovisionado")}
    </p>
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

/** Per-technique telemetry card: status, failing step, time, signals. */
function TechniqueCard({ metric }: { metric: TechniqueMetric }) {
  const { t } = useTranslation("pipeline")
  const title = metric.technique_id ?? metric.node_id ?? "—"
  return (
    <li className="flex flex-col gap-1 rounded-md border p-2" data-tecnica={title}>
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex min-w-0 items-center gap-1.5 truncate text-sm font-medium">
          <Target className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <code className="truncate font-mono">{title}</code>
        </span>
        <StatusBadge status={metric.status} />
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
        {metric.node_id && metric.technique_id ? (
          <span>
            {t("metricas.no")}: <code className="font-mono">{metric.node_id}</code>
          </span>
        ) : null}
        {metric.execution_time_s != null ? (
          <span className="inline-flex items-center gap-1">
            <Clock className="size-3" aria-hidden />
            {formatTime(metric.execution_time_s)}
          </span>
        ) : null}
        {metric.failed_step ? (
          <span className="text-team-red">
            {t("metricas.etapaFalha")}: {metric.failed_step}
          </span>
        ) : null}
      </div>
      {metric.telemetry.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("metricas.telemetria")}
          </span>
          <ul className="flex flex-wrap gap-1">
            {metric.telemetry.map((signal, index) => (
              <li
                key={index}
                className="rounded border bg-muted px-1.5 py-0.5 font-mono text-[11px]"
              >
                {signal}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </li>
  )
}

/** Per-technique status pill, reusing the Stage-3 status palette. */
function StatusBadge({ status }: { status: AbilityResultStatus }) {
  const { t } = useTranslation("pipeline")
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-2 py-0.5 text-xs",
        status === "sucesso" && "bg-team-blue/10 text-team-blue",
        status === "falha" && "bg-team-red/10 text-team-red",
        (status === "pendente" || status === "em_execucao") &&
          "bg-muted text-muted-foreground",
      )}
      data-status={status}
    >
      {t(`stage3.status.${status}`)}
    </span>
  )
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

/** Format an ISO timestamp for display; falls back to the raw string. */
function formatDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString()
}
