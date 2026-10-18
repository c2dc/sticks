import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { StageStateBadge } from "@/components/StageStateBadge"
import {
  getStage1,
  type CampaignStixInput,
  type Stage1Result,
  type Stage1Technique,
} from "@/lib/stages"

/**
 * Stage1View — Structural Modeling (Task 12.2, Req. 2).
 *
 * Renders the three labelled sections of Stage 1:
 * - Input: the source STIX intelligence of the selected case (Req. 2.1).
 *   The DAG is NOT an input here — it is produced in Stage 2 (DAG Generation).
 * - Processing: the modeling state (nao_iniciado / em_andamento / concluido /
 *   erro), surfacing the failure cause and step when it errors (Req. 2.6).
 * - Output: the extracted techniques, relationships, indicators, infrastructure,
 *   malware and metadata (Req. 2.3). When the extraction produced no structural
 *   elements (`output.is_empty`), it shows the "no structural elements extracted"
 *   indication instead (Req. 2.5).
 */
export function Stage1View({
  campaignId,
  version,
}: {
  campaignId: string
  /**
   * STIX base version selected in the flow (Req. 3.1, 3.2). Propagated to the
   * read `getStage1(campaignId, version)` so the Input slices the `campaign`
   * object of the correct base. Optional/backward-compatible: when absent, the
   * backend applies the fallback (`versao_ausente`) without error.
   */
  version?: string
}) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<Stage1Result | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setResult(null)
    setLoadError(null)
    getStage1(campaignId, version)
      .then((data) => {
        if (active) setResult(data)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [campaignId, version, t])

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

  const { input, output, state } = result

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Input — source STIX intelligence (Req. 2.1). The DAG is produced
          in Stage 2, so it is not shown as an input here. */}
      <StageSection
        data-section="entrada"
        label={t("sections.entrada")}
        kind="blue"
      >
        {/* The Structural Modeling Input is the identity of the STIX campaign of
            the selected base (slice of the `campaign` object). The DAG and any
            Caldera path/file are NOT shown here (Property 3). */}
        <CampaignStixBlock campaign={input.campaign_stix} />
      </StageSection>

      {/* Processing — modeling state + failure cause/step (Req. 2.6). */}
      <StageSection
        data-section="processamento"
        label={t("sections.processamento")}
        kind="purple"
      >
        <div className="flex items-center gap-2">
          <StageStateBadge state={state} />
        </div>
        {state === "erro" ? (
          <div className="flex flex-col gap-1 text-team-red">
            <p className="text-sm">{result.error_message || t("error.unknown")}</p>
            {result.failed_step ? (
              <p className="text-xs text-muted-foreground">
                {t("stage1.processamento.failedStep", { step: result.failed_step })}
              </p>
            ) : null}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            {t("stage1.processamento.description")}
          </p>
        )}
      </StageSection>

      {/* Output — extracted structural elements, or absence indication (2.3/2.5).
          `output` is null until the stage actually runs (read-only GET): show a
          "not run yet" note instead of fabricating a completed output. */}
      <StageSection
        data-section="saida"
        label={t("sections.saida")}
        kind="neutral"
      >
        {!output ? (
          <EmptyNote>{t("stage1.saida.notRun")}</EmptyNote>
        ) : output.is_empty ? (
          <EmptyNote>{output.note || t("stage1.saida.empty")}</EmptyNote>
        ) : (
          <div className="flex flex-col gap-3">
            <TechniquesList techniques={output.techniques} />
            <CountRow
              label={t("stage1.saida.relationships")}
              count={output.relationships.length}
            />
            <CountRow
              label={t("stage1.saida.indicators")}
              count={output.indicators.length}
            />
            <CountRow
              label={t("stage1.saida.infrastructure")}
              count={output.infrastructure.length}
            />
            <CountRow label={t("stage1.saida.malware")} count={output.malware.length} />
            <MetadataBlock metadata={output.metadata} />
          </div>
        )}
      </StageSection>
    </div>
  )
}

/**
 * Stage 1 Input: identity of the STIX campaign of the selected base (slice of
 * the `campaign` object — Req. 1.1, 2.1).
 *
 * - Found (`found === true`): renders name, STIX id, external_id, readable
 *   description (scroll/height limit), aliases (chips), dates and base version.
 *   Absent fields (null/empty) are OMITTED — it never renders empty/confusing
 *   rows (Req. 2.3, 2.4).
 * - Not found (falsy or `found === false`): shows a clear alert (`role="alert"`)
 *   and the available curated identifier (name + external_id), keeping the
 *   screen functional (Req. 2.1, 4.2).
 *
 * It never shows Caldera paths/files (Property 3).
 */
export function CampaignStixBlock({
  campaign,
}: {
  campaign?: CampaignStixInput | null
}) {
  const { t } = useTranslation("pipeline")

  if (!campaign || campaign.found !== true) {
    const curated = [campaign?.name, campaign?.external_id]
      .filter((v): v is string => Boolean(v))
      .join(" · ")
    return (
      <div
        className="flex flex-col gap-1 rounded-md border border-team-red/30 bg-team-red/5 p-2"
        role="alert"
        data-testid="campaign-stix-not-found"
      >
        <p className="text-sm text-team-red">
          {t("stage1.entrada.naoEncontrada")}
        </p>
        {curated ? (
          <p className="break-all text-xs text-muted-foreground">{curated}</p>
        ) : null}
      </div>
    )
  }

  return (
    <div
      className="flex flex-col gap-2"
      data-testid="campaign-stix"
      data-section="campaign-stix"
    >
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("stage1.entrada.campanhaStix")}
      </p>
      <dl className="flex flex-col gap-2">
        <Field label={t("stage1.entrada.nome")} value={campaign.name} />
        <Field label={t("stage1.entrada.stixId")} value={campaign.stix_id} />
        <Field
          label={t("stage1.entrada.externalId")}
          value={campaign.external_id}
        />
        {campaign.description ? (
          <div className="flex flex-col">
            <dt className="text-xs font-medium text-muted-foreground">
              {t("stage1.entrada.descricao")}
            </dt>
            <dd className="max-h-40 overflow-auto whitespace-pre-wrap text-sm">
              {campaign.description}
            </dd>
          </div>
        ) : null}
        {campaign.aliases.length > 0 ? (
          <div className="flex flex-col gap-1">
            <dt className="text-xs font-medium text-muted-foreground">
              {t("stage1.entrada.aliases")}
            </dt>
            <dd>
              <ul className="flex flex-wrap gap-1.5">
                {campaign.aliases.map((alias, index) => (
                  <li
                    key={`${alias}-${index}`}
                    className="rounded-md border border-team-blue/30 bg-team-blue/5 px-2 py-0.5 text-xs"
                  >
                    {alias}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}
        <Field
          label={t("stage1.entrada.firstSeen")}
          value={campaign.first_seen}
        />
        <Field label={t("stage1.entrada.lastSeen")} value={campaign.last_seen} />
        <Field
          label={t("stage1.entrada.versaoBase")}
          value={campaign.base_version}
        />
      </dl>
    </div>
  )
}

/**
 * Label/value row of the Input. It omits itself entirely when `value` is absent
 * (null/undefined/empty) so it never renders empty/confusing rows (Req. 2.4).
 */
function Field({
  label,
  value,
}: {
  label: string
  value?: string | null
}) {
  if (!value) return null
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
