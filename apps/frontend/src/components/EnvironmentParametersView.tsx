import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Boxes, ServerCog, ShieldAlert } from "lucide-react"

import { EmptyNote, StageSection } from "@/components/StageSection"
import {
  getStageOperationalization,
  type TargetHost,
  type TranslationSource,
  type EnvironmentParameters,
  type EnvironmentPlatform,
  type RequiredService,
} from "@/lib/stages"

/**
 * EnvironmentParametersView — panel of the Stage 4 (Operationalization)
 * environment parameters, shown beside `Stage2View` (Abilities) — Task 14.3,
 * Req. 4.1, 8.3, 8.4.
 *
 * Stage 4 produces two separate typed artifacts: the ordered Abilities
 * (rendered by `Stage2View`) and the `EnvironmentParameters` destined for
 * Stage 5. This screen handles the second artifact, showing:
 *
 * - The environment **platform** (container today; VM admitted without a
 *   redesign — Req. 17.1, 17.2) and the translation **origin** (explicit and
 *   extensible).
 * - The **target hosts** derived from the curated DAG nodes/commands: label,
 *   address, container, host type and vulnerabilities (Req. 8.3).
 * - The **services/vulnerabilities** to provision, inferred from the commands,
 *   with the originating technique (Req. 8.3).
 * - The **`network_context`** derived from the nodes (network-context notes).
 *
 * It is read-only: loads the Stage 4 payload on mount and handles the loading
 * and load-error states like its sibling screens (Stage2View /
 * EnvironmentStageView). Reuses `getStageOperationalization` and extracts only
 * the `parameters` field of the Stage4Response.
 */
export function EnvironmentParametersView({ campaignId }: { campaignId: string }) {
  const { t } = useTranslation("pipeline")
  const [parameters, setParameters] = useState<EnvironmentParameters | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setParameters(null)
    setLoadError(null)
    getStageOperationalization(campaignId)
      .then((data) => {
        if (active) setParameters(data.parameters)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [campaignId, t])

  if (loadError) return <EmptyNote>{loadError}</EmptyNote>
  if (!parameters) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  const {
    platform,
    origin,
    hosts,
    services,
    network_context,
  } = parameters

  return (
    <div className="flex flex-col gap-4" data-section="environment-parameters">
      {/* Platform + translation origin. */}
      <StageSection
        data-section="plataforma"
        label={t("parametrosAmbiente.titulo")}
        kind="purple"
        description={t("parametrosAmbiente.descricao")}
      >
        <div className="flex flex-wrap items-center gap-2">
          <PlatformBadge platform={platform} />
          <OriginBadge origin={origin} />
        </div>
      </StageSection>

      {/* Target hosts derived from the curated DAG nodes (Req. 8.3). */}
      <StageSection
        data-section="hosts"
        label={t("parametrosAmbiente.hosts.titulo")}
        kind="blue"
      >
        {hosts.length === 0 ? (
          <EmptyNote>{t("parametrosAmbiente.hosts.vazio")}</EmptyNote>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2">
            {hosts.map((host, index) => (
              <TargetHostCard key={`${host.label ?? "host"}-${index}`} host={host} />
            ))}
          </ul>
        )}
      </StageSection>

      {/* Services/vulnerabilities inferred from the commands (Req. 8.3). */}
      <StageSection
        data-section="servicos"
        label={t("parametrosAmbiente.servicos.titulo")}
        kind="red"
      >
        {services.length === 0 ? (
          <EmptyNote>{t("parametrosAmbiente.servicos.vazio")}</EmptyNote>
        ) : (
          <ul className="flex flex-col gap-2">
            {services.map((service, index) => (
              <ServiceCard key={index} service={service} />
            ))}
          </ul>
        )}
      </StageSection>

      {/* network_context derived from the nodes (network-context notes). */}
      <StageSection
        data-section="network-context"
        label={t("parametrosAmbiente.networkContext.titulo")}
        kind="neutral"
      >
        {network_context.length === 0 ? (
          <EmptyNote>{t("parametrosAmbiente.networkContext.vazio")}</EmptyNote>
        ) : (
          <ul className="flex flex-col gap-1">
            {network_context.map((note, index) => (
              <li
                key={index}
                className="rounded-md border bg-muted/40 px-2 py-1 text-xs"
              >
                {note}
              </li>
            ))}
          </ul>
        )}
      </StageSection>
    </div>
  )
}

/**
 * The platform indicator — container today, VM admitted without a redesign
 * (Req. 17.1, 17.2). Falls back to the raw value for a future, not-yet-
 * translated platform so a new value shows without a model change.
 */
function PlatformBadge({ platform }: { platform: EnvironmentPlatform }) {
  const { t } = useTranslation("pipeline")
  const key = `parametrosAmbiente.plataforma.${platform}`
  const translated = t(key)
  const label = translated === key ? platform : translated

  return (
    <span
      className="inline-flex w-fit items-center gap-1.5 rounded-full border border-team-purple/30 bg-team-purple/5 px-2.5 py-0.5 text-xs font-medium text-team-purple"
      data-plataforma={platform}
    >
      <Boxes className="size-3.5" aria-hidden />
      {label}
    </span>
  )
}

/**
 * The translation-source indicator (Req. 16.2, 16.4). Explicit, extensible —
 * falls back to the raw value for future origins so a new AI-assisted source
 * shows without a model change.
 */
function OriginBadge({ origin }: { origin: TranslationSource }) {
  const { t } = useTranslation("pipeline")
  const key = `stage2.translationSource.${origin}`
  const translated = t(key)
  const label = translated === key ? origin : translated

  return (
    <span className="inline-flex w-fit items-center gap-1.5 rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium text-foreground" data-origem={origin}>
      <span className="text-muted-foreground">
        {t("stage2.translationSource.label")}:
      </span>
      {label}
    </span>
  )
}

/** One target-host card: label / address / container / type / vulns. */
function TargetHostCard({ host }: { host: TargetHost }) {
  const { t } = useTranslation("pipeline")
  return (
    <li className="flex flex-col gap-1 rounded-md border p-2" data-host={host.label}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium" title={host.label}>
          {host.label}
        </span>
        {host.host_type ? (
          <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
            {host.host_type}
          </span>
        ) : null}
      </div>
      {host.address ? (
        <p className="truncate text-xs text-muted-foreground" title={host.address}>
          <span className="text-muted-foreground">
            {t("parametrosAmbiente.hosts.endereco")}:{" "}
          </span>
          <code className="font-mono">{host.address}</code>
        </p>
      ) : null}
      {host.container ? (
        <p className="truncate text-xs text-muted-foreground" title={host.container}>
          <span className="text-muted-foreground">
            {t("parametrosAmbiente.hosts.container")}:{" "}
          </span>
          <code className="font-mono">{host.container}</code>
        </p>
      ) : null}
      {host.vulnerabilities.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("parametrosAmbiente.hosts.vulnerabilidades")}
          </span>
          <ul className="flex flex-wrap gap-1">
            {host.vulnerabilities.map((vuln, index) => (
              <li
                key={index}
                className="inline-flex items-center gap-1 rounded border border-team-red/30 bg-team-red/5 px-1.5 py-0.5 text-[11px] text-team-red"
              >
                <ShieldAlert className="size-3" aria-hidden />
                {vuln}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </li>
  )
}

/** One required service/vulnerability with its originating technique. */
function ServiceCard({ service }: { service: RequiredService }) {
  const { t } = useTranslation("pipeline")
  return (
    <li className="flex items-start gap-2 rounded-md border p-2">
      <ServerCog className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
      <div className="flex min-w-0 flex-col">
        <span className="text-sm">{service.description}</span>
        {service.origin_technique ? (
          <span className="text-xs text-muted-foreground">
            {t("parametrosAmbiente.servicos.tecnicaOrigem")}:{" "}
            <code className="font-mono">{service.origin_technique}</code>
          </span>
        ) : null}
      </div>
    </li>
  )
}
