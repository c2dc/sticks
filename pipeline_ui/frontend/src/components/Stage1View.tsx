import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { StageStateBadge } from "@/components/StageStateBadge"
import {
  getEstagio1,
  type Stage1Result,
  type Stage1Technique,
} from "@/lib/estagios"

/**
 * Stage1View — Structural Modeling (Task 12.2, Req. 2).
 *
 * Renders the three labelled sections of Stage 1:
 * - Entrada: the source STIX / DAG reference of the selected case (Req. 2.1).
 * - Processamento: the modeling state (nao_iniciado / em_andamento / concluido /
 *   erro), surfacing the failure cause and step when it errors (Req. 2.6).
 * - Saída: the extracted techniques, relationships, indicators, infrastructure,
 *   malware and metadata (Req. 2.3). When the extraction produced no structural
 *   elements (`saida.is_empty`), it shows the "no structural elements extracted"
 *   indication instead (Req. 2.5).
 */
export function Stage1View({ casoId }: { casoId: string }) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<Stage1Result | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setResult(null)
    setLoadError(null)
    getEstagio1(casoId)
      .then((data) => {
        if (active) setResult(data)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [casoId, t])

  if (loadError) {
    return <EmptyNote>{loadError}</EmptyNote>
  }
  if (!result) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  const { entrada, saida, estado } = result

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Entrada — source STIX / DAG reference (Req. 2.1). */}
      <StageSection
        data-section="entrada"
        label={t("sections.entrada")}
        kind="blue"
        description={entrada.campaign_name}
      >
        <dl className="flex flex-col gap-2">
          <Field label={t("stage1.entrada.stix")} value={entrada.stix_reference} />
          <Field label={t("stage1.entrada.dag")} value={entrada.dag_file} />
        </dl>
      </StageSection>

      {/* Processamento — modeling state + failure cause/step (Req. 2.6). */}
      <StageSection
        data-section="processamento"
        label={t("sections.processamento")}
        kind="purple"
      >
        <div className="flex items-center gap-2">
          <StageStateBadge estado={estado} />
        </div>
        {estado === "erro" ? (
          <div className="flex flex-col gap-1 text-team-red">
            <p className="text-sm">{result.mensagem_erro || t("error.unknown")}</p>
            {result.etapa_falha ? (
              <p className="text-xs text-muted-foreground">
                {t("stage1.processamento.failedStep", { step: result.etapa_falha })}
              </p>
            ) : null}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            {t("stage1.processamento.description")}
          </p>
        )}
      </StageSection>

      {/* Saída — extracted structural elements, or absence indication (2.3/2.5). */}
      <StageSection
        data-section="saida"
        label={t("sections.saida")}
        kind="neutral"
      >
        {saida.is_empty ? (
          <EmptyNote>{saida.note || t("stage1.saida.empty")}</EmptyNote>
        ) : (
          <div className="flex flex-col gap-3">
            <TechniquesList techniques={saida.techniques} />
            <CountRow
              label={t("stage1.saida.relationships")}
              count={saida.relationships.length}
            />
            <CountRow
              label={t("stage1.saida.indicators")}
              count={saida.indicators.length}
            />
            <CountRow
              label={t("stage1.saida.infrastructure")}
              count={saida.infrastructure.length}
            />
            <CountRow label={t("stage1.saida.malware")} count={saida.malware.length} />
            <MetadataBlock metadata={saida.metadata} />
          </div>
        )}
      </StageSection>
    </div>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="break-all text-sm">{value}</dd>
    </div>
  )
}

function TechniquesList({ techniques }: { techniques: Stage1Technique[] }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("stage1.saida.techniques", { count: techniques.length })}
      </p>
      {techniques.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("stage1.saida.noTechniques")}</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {techniques.map((tech, index) => (
            <li
              key={tech.technique_id ?? index}
              className="rounded-md border border-team-blue/30 bg-team-blue/5 px-2 py-0.5 text-xs"
              title={tech.description ?? undefined}
            >
              <span className="font-medium">{tech.technique_id ?? "—"}</span>
              {tech.name ? <span className="text-muted-foreground"> · {tech.name}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function CountRow({ label, count }: { label: string; count: number }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">{count}</span>
    </div>
  )
}

function MetadataBlock({ metadata }: { metadata: Record<string, unknown> }) {
  const { t } = useTranslation("pipeline")
  const entries = Object.entries(metadata ?? {})
  if (entries.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("stage1.saida.metadata")}
      </p>
      <dl className="flex flex-col gap-0.5">
        {entries.map(([key, value]) => (
          <div key={key} className="flex justify-between gap-3 text-xs">
            <dt className="text-muted-foreground">{key}</dt>
            <dd className="break-all text-right">{String(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
