import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, ShieldAlert, ShieldQuestion } from "lucide-react"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { MermaidDiagram } from "@/components/MermaidDiagram"
import { PrereqChecklist } from "@/components/PrereqChecklist"
import { ProvisioningConsole } from "@/components/ProvisioningConsole"
import { cn } from "@/lib/utils"
import type { StageState } from "@/lib/campaigns"
import {
  getStageEnvironment,
  provisionEnvironment,
  type ProvisionedEnvironment,
  type IsolationSummary,
  type ProvisioningResponse,
  type HealthSummary,
  type TopologyHost,
} from "@/lib/stages"

/** Empty health summary (not approved) used as the checklist fallback. */
const EMPTY_HEALTH: HealthSummary = { approved: false, verdicts: [] }
/** Empty isolation summary (not approved) used as the checklist fallback. */
const EMPTY_ISOLATION: IsolationSummary = { approved: false, verdicts: [] }

/**
 * EnvironmentStageView — Environment Provisioning (Stage 4, Req. 5.4–5.6).
 *
 * Dedicated Stage 4 screen. Shows, side by side:
 *
 * - The **contained lab topology**: Mermaid diagram, internal networks
 *   (name + subnet), hosts (name / container / role / IPs) and the environment
 *   file (docker-compose) when present.
 * - The **isolation state** in three situations:
 *   - `isolated === true` → a clear "isolated environment / ready" indicator
 *     (team-blue). Stage 4 auto-completes and Stage 5 is unlocked — the case of
 *     the auto-completed curated environments.
 *   - `isolated === false && !isolationUnavailable` → shows the `reason` as why
 *     the environment is not isolated, with a block/warning indicator
 *     (team-red), making clear Stage 5 is blocked.
 *   - `isolationUnavailable === true` → shows that isolation **could not be
 *     verified** (fail-closed) with the `reason`, a warning indicator, Stage 5
 *     blocked — the topology is still shown.
 *
 * Beyond the topology + isolation (inherited from Stage 4), the Stage 5 screen
 * mounts the **live log terminal** ({@link ProvisioningConsole}) and the
 * **three-prerequisite checklist** ({@link PrereqChecklist}, with the Stage 6
 * blocked indicator).
 *
 * On opening the screen, it **automatically** triggers the idempotent
 * provisioning (`provisionEnvironment` — ensures/starts; reuses a ready
 * environment, Req. 1.1, 1.6) with no manual action and, in parallel, reads the
 * read-only payload (`getStageEnvironment`) for the topology + checklist. The
 * `ProvisioningConsole` streams the live logs over the channel, independently.
 *
 * When provisioning completes successfully, it calls `onProvisioningComplete`
 * so the wizard re-reads the backend state and unlocks "Next" (Req. 6.4, 7.3) —
 * Stage 5 only becomes `concluido` after the three prerequisites pass.
 */
export function EnvironmentStageView({
  campaignId,
  onProvisioningComplete,
}: {
  campaignId: string
  /**
   * Called when provisioning completes successfully (the three prerequisites
   * pass). The wizard uses this to re-read the backend state and unlock "Next"
   * (Req. 6.4, 7.3).
   */
  onProvisioningComplete?: () => void
}) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<ProvisioningResponse | null>(null)
  const [provisioned, setProvisioned] = useState<ProvisionedEnvironment | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setResult(null)
    setProvisioned(null)
    setLoadError(null)

    // Trigger the idempotent provisioning (ensure/start) on opening the screen —
    // no manual button (Req. 1.1). Reusing a ready environment is safe on
    // remount (Req. 1.6). The Output feeds the checklist (health/isolation/
    // abilities) and, on completion, unlocks "Next" via the callback.
    provisionEnvironment(campaignId)
      .then((environment) => {
        if (!active) return
        setProvisioned(environment)
        const completed =
          !environment.failed_step &&
          environment.health.approved &&
          environment.isolation.approved &&
          environment.loaded_abilities
        if (completed) onProvisioningComplete?.()
        // Re-read the read-only payload after provisioning to reflect the
        // latest topology + states in the checklist.
        return getStageEnvironment(campaignId).then((data) => {
          if (active) setResult(data)
        })
      })
      .catch(() => {
        // Provisioning failed/unavailable: still show the current topology and
        // read-only state (fail-closed on screen).
        if (!active) return
        getStageEnvironment(campaignId)
          .then((data) => {
            if (active) setResult(data)
          })
          .catch(() => {
            if (active) setLoadError(t("stageView.loadError"))
          })
      })

    return () => {
      active = false
    }
  }, [campaignId, t, onProvisioningComplete])

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

  const {
    hosts,
    networks,
    mermaid,
    compose_file,
    isolated,
    isolation_unavailable: isolationUnavailable,
    reason,
  } = result

  // Stage 5 state in the read-only payload's `states` (the checklist source).
  const stage5 = result.states.stages.find((e) => e.stage === 5)
  const checklistState = (stage5?.state ?? "nao_iniciado") as StageState
  // Prefer the fresh `provision` result for health/isolation/abilities (the GET
  // payload has no `loaded_abilities`); fall back to the GET when absent.
  const checklistHealth = provisioned?.health ?? result.health ?? EMPTY_HEALTH
  const checklistIsolation = provisioned?.isolation ?? EMPTY_ISOLATION
  const abilitiesLoaded = provisioned?.loaded_abilities ?? false
  const failedStep = provisioned?.failed_step ?? stage5?.failed_step ?? null
  const errorMessage = provisioned?.error_message ?? stage5?.error_message ?? null

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Contained lab topology — diagram + internal networks. */}
      <StageSection
        data-section="topologia"
        label={t("stage4.topologia.titulo")}
        kind="blue"
        description={t("stage4.topologia.descricao")}
        className="md:col-span-2"
      >
        <MermaidDiagram definition={mermaid} className="w-full overflow-auto" />

        <div className="flex flex-col gap-1">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t("stage4.topologia.redes")}
          </p>
          {networks.length === 0 ? (
            <p className="text-xs text-muted-foreground">{t("stage4.topologia.semRedes")}</p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {networks.map((network) => (
                <li key={network.name} className="flex justify-between gap-2 text-xs">
                  <span className="text-muted-foreground">{network.name}</span>
                  <span className="font-mono">{network.subnet}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {compose_file ? (
          <p
            className="truncate text-[11px] text-muted-foreground"
            title={compose_file}
          >
            {t("stage4.topologia.composeFile")}: <code>{compose_file}</code>
          </p>
        ) : null}
      </StageSection>

      {/* Isolation state — ready / blocked / not verifiable. */}
      <StageSection
        data-section="isolamento"
        label={t("stage4.isolamento.titulo")}
        kind={isolated ? "blue" : "red"}
      >
        <IsolationIndicator
          isolated={isolated}
          unavailable={isolationUnavailable}
          reason={reason}
        />
      </StageSection>

      {/* Environment hosts — name / container / role / IPs. */}
      <StageSection
        data-section="hosts"
        label={t("stage4.topologia.hosts")}
        kind="neutral"
        className="md:col-span-3"
      >
        {hosts.length === 0 ? (
          <EmptyNote>{t("stage4.topologia.semHosts")}</EmptyNote>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {hosts.map((host) => (
              <HostCard key={host.name} host={host} />
            ))}
          </ul>
        )}
      </StageSection>

      {/* Live provisioning log terminal (Req. 2.x). */}
      <ProvisioningConsole campaignId={campaignId} className="md:col-span-2" />

      {/* Three-prerequisite checklist + Stage 6 blocked indicator
          (Req. 5.6, 7.2, 7.3). */}
      <PrereqChecklist
        state={checklistState}
        health={checklistHealth}
        isolation={checklistIsolation}
        abilitiesLoaded={abilitiesLoaded}
        failedStep={failedStep}
        errorMessage={errorMessage}
      />
    </div>
  )
}

/**
 * The isolation state indicator — exactly one of the three mutually-exclusive
 * situations (Req. 5.4–5.6). When not isolated or not verifiable, it also makes
 * clear that Stage 5 is blocked.
 */
function IsolationIndicator({
  isolated,
  unavailable,
  reason,
}: {
  isolated: boolean
  unavailable: boolean
  reason: string | null
}) {
  const { t } = useTranslation("pipeline")

  if (isolated) {
    return (
      <div className="flex flex-col gap-2" role="status">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-team-blue/10 px-2.5 py-1 text-sm font-medium text-team-blue">
          <CheckCircle2 className="size-4" aria-hidden />
          {t("stage4.isolamento.isolado")}
        </span>
        <p className="text-xs text-muted-foreground">
          {t("stage4.isolamento.isoladoDescricao")}
        </p>
      </div>
    )
  }

  // Both remaining states are blocking: non-isolated or unverifiable.
  const Icon = unavailable ? ShieldQuestion : ShieldAlert
  const title = unavailable
    ? t("stage4.isolamento.indisponivel")
    : t("stage4.isolamento.naoIsolado")

  return (
    <div className="flex flex-col gap-2">
      <span className="inline-flex items-center gap-1.5 rounded-full bg-team-red/10 px-2.5 py-1 text-sm font-medium text-team-red">
        <Icon className="size-4" aria-hidden />
        {title}
      </span>
      {unavailable ? (
        <p className="text-xs text-muted-foreground">
          {t("stage4.isolamento.indisponivelDescricao")}
        </p>
      ) : null}
      {reason ? <p className="text-sm text-team-red">{reason}</p> : null}
      <p className="text-xs text-muted-foreground">{t("stage4.isolamento.bloqueado")}</p>
    </div>
  )
}

/** One lab host card: name / container / role / IPs. */
function HostCard({ host }: { host: TopologyHost }) {
  const { t } = useTranslation("pipeline")
  const roleLabel =
    host.role === "attacker"
      ? t("stage4.topologia.papel.attacker")
      : host.role === "target"
        ? t("stage4.topologia.papel.target")
        : host.role

  return (
    <li className="flex flex-col gap-1 rounded-md border p-2">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium" title={host.name}>
          {host.name}
        </span>
        <span
          className={cn(
            "shrink-0 rounded-full px-2 py-0.5 text-[11px]",
            host.role === "attacker" && "bg-team-red/10 text-team-red",
            host.role === "target" && "bg-team-blue/10 text-team-blue",
            host.role !== "attacker" &&
              host.role !== "target" &&
              "bg-muted text-muted-foreground",
          )}
        >
          {roleLabel}
        </span>
      </div>
      <p className="truncate text-xs text-muted-foreground" title={host.container}>
        <code>{host.container}</code>
      </p>
      {host.ips.length > 0 ? (
        <ul className="flex flex-wrap gap-1">
          {host.ips.map((ip) => (
            <li
              key={ip}
              className="rounded border bg-muted px-1.5 py-0.5 font-mono text-[11px]"
            >
              {ip}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  )
}
