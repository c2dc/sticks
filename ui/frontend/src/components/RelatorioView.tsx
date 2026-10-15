import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, Clock, Target, XCircle } from "lucide-react"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { cn } from "@/lib/utils"
import {
  getRelatorio,
  type AbilityResultStatus,
  type MetricaTecnica,
  type RelatorioExecucao,
} from "@/lib/estagios"

/**
 * RelatorioView — relatório consolidado do Estágio 6 (Execução, medição e
 * relatório) — Task 14.3, Req. 4.1, 10.6, 10.7, 15.7.
 *
 * Lê o `RelatorioExecucao` (`GET /api/casos/{caso}/relatorio`) e exibe a medição
 * de suficiência procedural:
 *
 * - **Veredito** geral de sucesso (Req. 15.6) e flag de desprovisionamento do
 *   ambiente (Req. 12).
 * - **Taxa de sucesso** (técnicas com sucesso / total do DAG — Req. 15.1) e
 *   **cobertura procedural** ∈ [0, 1] (Req. 15.3), formatadas como percentuais.
 * - **Tempo total** de execução (Req. 15.5) e tempo por técnica.
 * - **Nós falhos** do DAG (Req. 10.6, 15.4).
 * - **Telemetria por técnica** (status, etapa da falha, tempo, sinais
 *   observados — Req. 15.2, 15.4, 15.7).
 *
 * É somente-leitura: carrega o relatório na montagem e trata carregamento/erro
 * como as telas irmãs. Um caso sem execução devolve métricas zeradas, exibidas
 * sem erro.
 */
export function RelatorioView({ casoId }: { casoId: string }) {
  const { t } = useTranslation("pipeline")
  const [relatorio, setRelatorio] = useState<RelatorioExecucao | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setRelatorio(null)
    setLoadError(null)
    getRelatorio(casoId)
      .then((data) => {
        if (active) setRelatorio(data)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [casoId, t])

  if (loadError) return <EmptyNote>{loadError}</EmptyNote>
  if (!relatorio) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  const { metricas, ambiente_desprovisionado, gerado_em } = relatorio

  return (
    <div className="flex flex-col gap-4" data-section="relatorio">
      {/* Veredito geral + desprovisionamento do ambiente. */}
      <StageSection
        data-section="veredito"
        label={t("relatorio.titulo")}
        kind={metricas.veredito_sucesso ? "blue" : "red"}
        description={`${t("relatorio.geradoEm")}: ${formatarData(gerado_em)}`}
      >
        <VereditoBadge sucesso={metricas.veredito_sucesso} />
        <DesprovisionamentoBadge desprovisionado={ambiente_desprovisionado} />
      </StageSection>

      {/* Métricas de suficiência procedural (Req. 15.1, 15.3, 15.5). */}
      <StageSection
        data-section="metricas"
        label={t("metricas.titulo")}
        kind="purple"
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <MetricaCard
            label={t("metricas.taxaSucesso")}
            value={formatarPercentual(metricas.taxa_sucesso)}
            detalhe={`${metricas.tecnicas_sucesso} / ${metricas.total_tecnicas_dag}`}
          />
          <MetricaCard
            label={t("metricas.coberturaProcedural")}
            value={formatarPercentual(metricas.cobertura_procedural)}
          />
          <MetricaCard
            label={t("metricas.tempoTotal")}
            value={formatarTempo(metricas.tempo_total_s)}
          />
          <MetricaCard
            label={t("metricas.nosFalhos.titulo")}
            value={String(metricas.nos_falhos.length)}
          />
        </div>
      </StageSection>

      {/* Nós falhos do DAG (Req. 10.6, 15.4). */}
      <StageSection
        data-section="nos-falhos"
        label={t("metricas.nosFalhos.titulo")}
        kind="red"
      >
        {metricas.nos_falhos.length === 0 ? (
          <EmptyNote>{t("metricas.nosFalhos.vazio")}</EmptyNote>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {metricas.nos_falhos.map((no, index) => (
              <li
                key={index}
                className="inline-flex items-center gap-1 rounded-md border border-team-red/30 bg-team-red/5 px-2 py-0.5 text-xs text-team-red"
              >
                <XCircle className="size-3" aria-hidden />
                <code className="font-mono">{no}</code>
              </li>
            ))}
          </ul>
        )}
      </StageSection>

      {/* Telemetria por técnica (Req. 15.2, 15.4, 15.7). */}
      <StageSection
        data-section="telemetria"
        label={t("relatorio.telemetria.titulo")}
        kind="neutral"
      >
        {metricas.por_tecnica.length === 0 ? (
          <EmptyNote>{t("relatorio.telemetria.vazio")}</EmptyNote>
        ) : (
          <ul className="flex flex-col gap-2">
            {metricas.por_tecnica.map((metrica, index) => (
              <TecnicaCard key={index} metrica={metrica} />
            ))}
          </ul>
        )}
      </StageSection>
    </div>
  )
}

/** Overall success verdict (Req. 15.6). */
function VereditoBadge({ sucesso }: { sucesso: boolean }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1" role="status">
      <span
        className={cn(
          "inline-flex w-fit items-center gap-1.5 rounded-full px-2.5 py-1 text-sm font-medium",
          sucesso
            ? "bg-team-blue/10 text-team-blue"
            : "bg-team-red/10 text-team-red",
        )}
        data-veredito={sucesso ? "sucesso" : "falha"}
      >
        {sucesso ? (
          <CheckCircle2 className="size-4" aria-hidden />
        ) : (
          <XCircle className="size-4" aria-hidden />
        )}
        {sucesso
          ? t("relatorio.veredito.sucesso")
          : t("relatorio.veredito.falha")}
      </span>
    </div>
  )
}

/** Environment teardown flag (Req. 12). */
function DesprovisionamentoBadge({
  desprovisionado,
}: {
  desprovisionado: boolean
}) {
  const { t } = useTranslation("pipeline")
  return (
    <p className="text-xs text-muted-foreground" data-desprovisionado={desprovisionado}>
      {desprovisionado
        ? t("relatorio.ambiente.desprovisionado")
        : t("relatorio.ambiente.naoDesprovisionado")}
    </p>
  )
}

/** One headline metric: a big value, a label and an optional detail line. */
function MetricaCard({
  label,
  value,
  detalhe,
}: {
  label: string
  value: string
  detalhe?: string
}) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md border p-3">
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="text-2xl font-semibold tabular-nums">{value}</span>
      {detalhe ? (
        <span className="text-xs text-muted-foreground">{detalhe}</span>
      ) : null}
    </div>
  )
}

/** Per-technique telemetry card: status, failing step, time, signals. */
function TecnicaCard({ metrica }: { metrica: MetricaTecnica }) {
  const { t } = useTranslation("pipeline")
  const titulo = metrica.technique_id ?? metrica.node_id ?? "—"
  return (
    <li className="flex flex-col gap-1 rounded-md border p-2" data-tecnica={titulo}>
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex min-w-0 items-center gap-1.5 truncate text-sm font-medium">
          <Target className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <code className="truncate font-mono">{titulo}</code>
        </span>
        <StatusBadge status={metrica.status} />
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
        {metrica.node_id && metrica.technique_id ? (
          <span>
            {t("metricas.no")}: <code className="font-mono">{metrica.node_id}</code>
          </span>
        ) : null}
        {metrica.tempo_execucao_s != null ? (
          <span className="inline-flex items-center gap-1">
            <Clock className="size-3" aria-hidden />
            {formatarTempo(metrica.tempo_execucao_s)}
          </span>
        ) : null}
        {metrica.etapa_falha ? (
          <span className="text-team-red">
            {t("metricas.etapaFalha")}: {metrica.etapa_falha}
          </span>
        ) : null}
      </div>
      {metrica.telemetria.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("metricas.telemetria")}
          </span>
          <ul className="flex flex-wrap gap-1">
            {metrica.telemetria.map((sinal, index) => (
              <li
                key={index}
                className="rounded border bg-muted px-1.5 py-0.5 font-mono text-[11px]"
              >
                {sinal}
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
function formatarPercentual(valor: number): string {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(valor) ? valor : 0))
  return `${Math.round(clamped * 100)}%`
}

/** Format a duration in seconds; `null`/`undefined` becomes an em dash. */
function formatarTempo(segundos: number | null | undefined): string {
  if (segundos == null || !Number.isFinite(segundos)) return "—"
  return `${Math.round(segundos * 10) / 10}s`
}

/** Format an ISO timestamp for display; falls back to the raw string. */
function formatarData(iso: string): string {
  const data = new Date(iso)
  if (Number.isNaN(data.getTime())) return iso
  return data.toLocaleString()
}
