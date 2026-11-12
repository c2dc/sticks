import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"

import type { AiStageOutput } from "@/lib/ai"

/**
 * AgentTracePanel — the three-part Input → Processing → Output view of a single
 * agent step within one iteration of an AI Run (Req. 1.1, 1.5, 6.1–6.4, task 9.4).
 *
 * Each agent step (Flow generation agent, Operationalization agent, Environment
 * agent, Execution, Evaluation) renders here with three distinctly-labeled
 * parts read from the extended `getAiRun` detail:
 *
 * - **Input** — the Agent_Input the agent received (`trace.agent_input`): prior
 *   artifacts, selected-campaign context, retrieved experience, attack/STIX.
 * - **Processing** — the Agent_Processing the agent performed (`trace.processing`):
 *   reasoning, tools invoked, repair attempts render as compact summaries; the
 *   long raw LLM prompt + raw LLM response are tucked behind a collapsible
 *   "show details" disclosure CLOSED by default (Req. 6.2, 6.3).
 * - **Output** — the Agent_Output artifact produced for the stage (`payload`),
 *   stamped with its AI-generated `origin` (Req. 6.4).
 *
 * Any processing element the backend returned as the literal `"not_available"`
 * sentinel renders an explicit "not available" marker rather than being dropped
 * (Req. 1.6, 6.4). Every user-facing string is routed through i18next `t()` under
 * the `aiRun.trace.*` keys (pt-BR source of truth, en parity).
 *
 * Component identifiers (`data-section`, `data-part`) are English per Req. 9.1/9.4.
 */

/** The backend's explicit "genuinely absent" marker for a processing element. */
const NOT_AVAILABLE_SENTINEL = "not_available"

/** Raw LLM processing keys shown only behind the collapsed "show details" disclosure. */
const RAW_PROCESSING_KEYS = ["llm_prompt", "llm_response"] as const

/** The semantic `AgentStage` value whose processing trace carries the environment specifics. */
const ENVIRONMENT_STAGE = "environment"

/**
 * The Environment agent's specialized processing keys (task 6.3). For the
 * environment stage these render through {@link EnvironmentStepDetails} — the
 * fixed-vs-agent-decided split, BOTH gate verdicts and the provisioner
 * sub-phases — instead of falling through to the generic JSON entry list.
 */
const ENVIRONMENT_PROCESSING_KEYS = [
  "fixed_infrastructure",
  "agent_decided",
  "gates",
  "substages",
] as const

export function AgentTracePanel({
  stageName,
  stageOutput,
}: {
  /** Meaningful English stage name already resolved by the caller (e.g. "Flow generation agent"). */
  stageName: string
  /** The `agent_stage_outputs` row with its 1:1 Audit_Trace for this agent step. */
  stageOutput: AiStageOutput
}) {
  const { t } = useTranslation("pipeline")
  const { origin, payload, trace } = stageOutput
  const input = trace?.agent_input ?? {}
  const processing = trace?.processing ?? {}
  const isEnvironment = stageOutput.stage === ENVIRONMENT_STAGE

  // Split the processing trace: the long raw LLM prompt/response go behind the
  // collapsed disclosure; everything else renders as a compact summary. For the
  // Environment agent the specialized `fixed_infrastructure` / `agent_decided` /
  // `gates` / `substages` keys are lifted out of the generic summary and rendered
  // by `EnvironmentStepDetails` instead (task 6.3, task 9.5).
  const summaryProcessing = Object.entries(processing).filter(([key]) => {
    if (RAW_PROCESSING_KEYS.includes(key as (typeof RAW_PROCESSING_KEYS)[number])) return false
    if (
      isEnvironment &&
      ENVIRONMENT_PROCESSING_KEYS.includes(key as (typeof ENVIRONMENT_PROCESSING_KEYS)[number])
    ) {
      return false
    }
    return true
  })
  const rawProcessing = RAW_PROCESSING_KEYS.map((key) => [key, processing[key]] as const).filter(
    ([, value]) => value !== undefined,
  )

  return (
    <div
      className="flex flex-col gap-3 rounded-md border p-3"
      data-section="agent-trace-panel"
      data-stage={stageOutput.stage}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold">{stageName}</span>
        <span
          className="text-xs font-medium uppercase tracking-wide text-muted-foreground"
          data-part="origin"
        >
          {t(`aiRun.trace.origin.${origin}`, { defaultValue: origin })}
        </span>
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        {/* Input */}
        <TracePart label={t("aiRun.trace.input")} part="input">
          {Object.keys(input).length === 0 ? (
            <NotAvailable label={t("aiRun.trace.notAvailable")} />
          ) : (
            <TraceEntries entries={Object.entries(input)} notAvailableLabel={t("aiRun.trace.notAvailable")} />
          )}
        </TracePart>

        {/* Processing */}
        <TracePart label={t("aiRun.trace.processing")} part="processing">
          {summaryProcessing.length === 0 && !isEnvironment ? (
            <NotAvailable label={t("aiRun.trace.notAvailable")} />
          ) : null}
          {summaryProcessing.length > 0 ? (
            <TraceEntries entries={summaryProcessing} notAvailableLabel={t("aiRun.trace.notAvailable")} />
          ) : null}
          {isEnvironment ? <EnvironmentStepDetails processing={processing} /> : null}
          {rawProcessing.length > 0 ? (
            <details className="mt-1 rounded-md border bg-muted/30 p-2" data-part="raw-details">
              <summary className="cursor-pointer select-none text-xs font-medium text-muted-foreground">
                {t("aiRun.trace.showDetails")}
              </summary>
              <div className="mt-2 flex flex-col gap-2">
                {rawProcessing.map(([key, value]) => (
                  <div key={key} className="flex flex-col gap-0.5" data-raw={key}>
                    <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                      {t(`aiRun.trace.raw.${key}`, { defaultValue: key })}
                    </span>
                    {isNotAvailable(value) ? (
                      <NotAvailable label={t("aiRun.trace.notAvailable")} />
                    ) : (
                      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 text-xs text-muted-foreground">
                        {formatValue(value)}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            </details>
          ) : null}
        </TracePart>

        {/* Output */}
        <TracePart label={t("aiRun.trace.output")} part="output">
          {payload && Object.keys(payload).length > 0 ? (
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/30 p-2 text-xs text-muted-foreground">
              {formatValue(payload)}
            </pre>
          ) : (
            <NotAvailable label={t("aiRun.trace.notAvailable")} />
          )}
        </TracePart>
      </div>
    </div>
  )
}

/**
 * EnvironmentStepDetails — the Environment agent step's specialized rendering of
 * the design §E trace (task 6.3, task 9.5, Req. 1.1, 1.5). Delegated to by
 * {@link AgentTracePanel} when `stage === "environment"`, it surfaces the
 * four environment processing keys as structured UI instead of raw JSON:
 *
 * - **fixed_infrastructure** — the fixed-vs-agent-decided split's SYSTEM half:
 *   the execution engine, the isolable network structure and the resource limits
 *   the system imposes (not the agent's to decide).
 * - **agent_decided** — the AGENT half: the attacker node and the targets +
 *   vulnerabilities the agent designs within the fixed isolable structure.
 * - **gates** — BOTH gate verdicts: the code-level `ResourceGate` (approved +
 *   reasons) and the fail-closed `isolation` gate (approved + per-container
 *   verdicts).
 * - **substages** — the provisioner sub-phases (build online → close egress →
 *   verify egress lost → apply offline configuration) + per-host egress verdicts.
 *
 * Every string is routed through i18next under the `aiRun.environment.*` keys;
 * component identifiers (`data-section`) are English (Req. 9.1/9.4). Any absent
 * block renders nothing rather than an empty shell.
 */
function EnvironmentStepDetails({ processing }: { processing: Record<string, unknown> }) {
  const fixed = asRecord(processing.fixed_infrastructure)
  const decided = asRecord(processing.agent_decided)
  const gates = asRecord(processing.gates)
  const substages = asRecord(processing.substages)

  return (
    <div className="mt-1 flex flex-col gap-2" data-section="environment-step-details">
      {fixed ? <FixedInfrastructureBlock fixed={fixed} /> : null}
      {decided ? <AgentDecidedBlock decided={decided} /> : null}
      {gates ? <GateVerdictsBlock gates={gates} /> : null}
      {substages ? <SubStagesBlock substages={substages} /> : null}
    </div>
  )
}

/** A labeled, bordered block within the environment details. */
function EnvBlock({
  section,
  title,
  description,
  children,
}: {
  section: string
  title: string
  description?: string
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-1.5 rounded-md border bg-muted/20 p-2" data-section={section}>
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </span>
      {description ? <span className="text-xs text-muted-foreground">{description}</span> : null}
      {children}
    </div>
  )
}

/** One label → value line in an environment block. */
function EnvField({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="text-xs text-muted-foreground">{value}</span>
    </div>
  )
}

/** The SYSTEM half of the fixed-vs-agent-decided split (design §E). */
function FixedInfrastructureBlock({ fixed }: { fixed: Record<string, unknown> }) {
  const { t } = useTranslation("pipeline")
  const network = asRecord(fixed.isolable_network_structure)
  const limits = asRecord(fixed.resource_limits)
  const context = network ? toStringArray(network.network_context) : []

  return (
    <EnvBlock
      section="environment-fixed-infrastructure"
      title={t("aiRun.environment.fixed.title")}
      description={t("aiRun.environment.fixed.description")}
    >
      <EnvField
        label={t("aiRun.environment.fixed.executionEngine")}
        value={formatScalar(fixed.execution_engine)}
      />
      {network ? (
        <div className="flex flex-col gap-1">
          <EnvField
            label={t("aiRun.environment.fixed.networkStructure")}
            value={
              network.internal === true
                ? t("aiRun.environment.fixed.internal")
                : formatScalar(network.internal)
            }
          />
          <EnvField
            label={t("aiRun.environment.fixed.platform")}
            value={formatScalar(network.platform)}
          />
          <EnvField
            label={t("aiRun.environment.fixed.networkContext")}
            value={
              context.length > 0 ? (
                <ul className="list-disc pl-4">
                  {context.map((note, i) => (
                    <li key={i}>{note}</li>
                  ))}
                </ul>
              ) : (
                t("aiRun.environment.fixed.noNetworkContext")
              )
            }
          />
        </div>
      ) : null}
      {limits ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("aiRun.environment.fixed.limits")}
          </span>
          <EnvField
            label={t("aiRun.environment.fixed.maxContainers")}
            value={formatScalar(limits.max_containers)}
          />
          <EnvField
            label={t("aiRun.environment.fixed.maxRam")}
            value={formatScalar(limits.max_ram_gb)}
          />
        </div>
      ) : null}
    </EnvBlock>
  )
}

/** The AGENT half of the fixed-vs-agent-decided split (design §E). */
function AgentDecidedBlock({ decided }: { decided: Record<string, unknown> }) {
  const { t } = useTranslation("pipeline")
  const targets = Array.isArray(decided.targets) ? (decided.targets as unknown[]) : []

  return (
    <EnvBlock
      section="environment-agent-decided"
      title={t("aiRun.environment.agent.title")}
      description={t("aiRun.environment.agent.description")}
    >
      <EnvField
        label={t("aiRun.environment.agent.attackerNode")}
        value={
          isNotAvailable(decided.attacker_node) ? (
            <NotAvailable label={t("aiRun.trace.notAvailable")} />
          ) : (
            formatScalar(decided.attacker_node)
          )
        }
      />
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {t("aiRun.environment.agent.targets")}
      </span>
      {targets.length === 0 ? (
        <span className="text-xs text-muted-foreground">
          {t("aiRun.environment.agent.noTargets")}
        </span>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {targets.map((target, i) => {
            const record = asRecord(target)
            const vulns = record ? toStringArray(record.vulnerabilities) : []
            return (
              <li
                key={i}
                className="rounded border border-dashed p-1.5"
                data-section="environment-target"
              >
                <EnvField
                  label={t("aiRun.environment.agent.system")}
                  value={formatScalar(record?.system)}
                />
                <EnvField
                  label={t("aiRun.environment.agent.vulnerabilities")}
                  value={
                    vulns.length > 0 ? (
                      <ul className="list-disc pl-4">
                        {vulns.map((v, j) => (
                          <li key={j}>{v}</li>
                        ))}
                      </ul>
                    ) : (
                      t("aiRun.environment.agent.noVulnerabilities")
                    )
                  }
                />
              </li>
            )
          })}
        </ul>
      )}
    </EnvBlock>
  )
}

/** BOTH gate verdicts (design §E `gates`): ResourceGate + isolation. */
function GateVerdictsBlock({ gates }: { gates: Record<string, unknown> }) {
  const { t } = useTranslation("pipeline")
  const resourceGate = asRecord(gates.resource_gate)
  const isolation = asRecord(gates.isolation)

  return (
    <EnvBlock section="environment-gates" title={t("aiRun.environment.gates.title")}>
      {resourceGate ? (
        <div className="flex flex-col gap-1" data-section="environment-gate-resource">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("aiRun.environment.gates.resourceGate.title")}
            </span>
            <GateBadge approved={resourceGate.approved === true} />
          </div>
          <EnvField
            label={t("aiRun.environment.gates.resourceGate.reasons")}
            value={<ReasonList reasons={toStringArray(resourceGate.reasons)} emptyKey="aiRun.environment.gates.resourceGate.noReasons" />}
          />
        </div>
      ) : null}
      {isolation ? (
        <div className="flex flex-col gap-1" data-section="environment-gate-isolation">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("aiRun.environment.gates.isolation.title")}
            </span>
            <GateBadge approved={isolation.approved === true} />
          </div>
          <IsolationVerdicts verdicts={isolation.verdicts} />
        </div>
      ) : null}
    </EnvBlock>
  )
}

/** The isolation gate's per-container verdicts, or a fail-closed empty note. */
function IsolationVerdicts({ verdicts }: { verdicts: unknown }) {
  const { t } = useTranslation("pipeline")
  const rows = Array.isArray(verdicts) ? (verdicts as unknown[]) : []

  if (rows.length === 0) {
    return (
      <span className="text-xs text-muted-foreground">
        {t("aiRun.environment.gates.isolation.noVerdicts")}
      </span>
    )
  }

  return (
    <ul className="flex flex-col gap-1.5">
      {rows.map((verdict, i) => {
        const record = asRecord(verdict)
        const isolated = record?.isolated === true
        const reasons = record ? toStringArray(record.reasons) : []
        const externalNetworks = record ? toStringArray(record.external_networks) : []
        const externalDns = record ? toStringArray(record.external_dns) : []
        return (
          <li
            key={i}
            className="rounded border border-dashed p-1.5"
            data-section="environment-isolation-verdict"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium">{formatScalar(record?.container)}</span>
              <span
                className={
                  isolated
                    ? "text-xs font-medium text-team-blue"
                    : "text-xs font-medium text-team-red"
                }
              >
                {isolated
                  ? t("aiRun.environment.gates.isolation.isolated")
                  : t("aiRun.environment.gates.isolation.notIsolated")}
              </span>
            </div>
            {reasons.length > 0 ? (
              <EnvField
                label={t("aiRun.environment.gates.isolation.reasons")}
                value={<ReasonList reasons={reasons} />}
              />
            ) : null}
            {externalNetworks.length > 0 ? (
              <EnvField
                label={t("aiRun.environment.gates.isolation.externalNetworks")}
                value={<ReasonList reasons={externalNetworks} />}
              />
            ) : null}
            {externalDns.length > 0 ? (
              <EnvField
                label={t("aiRun.environment.gates.isolation.externalDns")}
                value={<ReasonList reasons={externalDns} />}
              />
            ) : null}
          </li>
        )
      })}
    </ul>
  )
}

/** The provisioner sub-phases (design §E `substages`) + per-host egress verdicts. */
function SubStagesBlock({ substages }: { substages: Record<string, unknown> }) {
  const { t } = useTranslation("pipeline")
  const egressVerdicts = Array.isArray(substages.egress_verdicts)
    ? (substages.egress_verdicts as unknown[])
    : []

  return (
    <EnvBlock section="environment-substages" title={t("aiRun.environment.substages.title")}>
      <dl className="grid gap-1.5 sm:grid-cols-2">
        <SubStageFlag label={t("aiRun.environment.substages.buildOnline")} ok={substages.build_online_ok === true} />
        <SubStageFlag
          label={t("aiRun.environment.substages.egressClosed")}
          ok={substages.egress_closed === true}
        />
        <SubStageFlag
          label={t("aiRun.environment.substages.egressLostVerified")}
          ok={substages.egress_lost_verified === true}
        />
        <SubStageFlag
          label={t("aiRun.environment.substages.offlineApplied")}
          ok={substages.offline_applied === true}
        />
      </dl>
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {t("aiRun.environment.substages.egressVerdicts")}
      </span>
      {egressVerdicts.length === 0 ? (
        <span className="text-xs text-muted-foreground">
          {t("aiRun.environment.substages.noEgressVerdicts")}
        </span>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {egressVerdicts.map((verdict, i) => {
            const record = asRecord(verdict)
            return (
              <li
                key={i}
                className="rounded border border-dashed p-1.5"
                data-section="environment-egress-verdict"
              >
                <span className="text-xs font-medium">{formatScalar(record?.container)}</span>
                <EnvField
                  label={t("aiRun.environment.substages.egressLost")}
                  value={boolLabel(record?.egress_lost === true, t)}
                />
                <EnvField
                  label={t("aiRun.environment.substages.conclusive")}
                  value={boolLabel(record?.conclusive === true, t)}
                />
                {record?.reason ? (
                  <EnvField
                    label={t("aiRun.environment.substages.reason")}
                    value={formatScalar(record.reason)}
                  />
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </EnvBlock>
  )
}

/** A yes/no sub-stage flag line. */
function SubStageFlag({ label, ok }: { label: string; ok: boolean }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex items-center justify-between gap-2 rounded border p-1.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={ok ? "text-xs font-medium text-team-blue" : "text-xs font-medium text-team-red"}>
        {boolLabel(ok, t)}
      </span>
    </div>
  )
}

/** An approved/rejected gate badge. */
function GateBadge({ approved }: { approved: boolean }) {
  const { t } = useTranslation("pipeline")
  return (
    <span
      className={
        approved
          ? "inline-flex items-center rounded border px-1.5 py-0.5 text-xs font-medium text-team-blue"
          : "inline-flex items-center rounded border px-1.5 py-0.5 text-xs font-medium text-team-red"
      }
    >
      {approved ? t("aiRun.environment.gates.approved") : t("aiRun.environment.gates.rejected")}
    </span>
  )
}

/** A compact bulleted reason list, or an optional empty note. */
function ReasonList({ reasons, emptyKey }: { reasons: string[]; emptyKey?: string }) {
  const { t } = useTranslation("pipeline")
  if (reasons.length === 0) {
    return emptyKey ? <span>{t(emptyKey)}</span> : null
  }
  return (
    <ul className="list-disc pl-4">
      {reasons.map((reason, i) => (
        <li key={i}>{reason}</li>
      ))}
    </ul>
  )
}

/** Resolve a boolean to the localized yes/no label. */
function boolLabel(value: boolean, t: (key: string) => string): string {
  return value ? t("aiRun.environment.substages.yes") : t("aiRun.environment.substages.no")
}

/** A nested object (plain record), or `null` for anything else / the sentinel. */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (value === null || value === undefined || value === NOT_AVAILABLE_SENTINEL) return null
  if (typeof value !== "object" || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/** Coerce a value into a string array, dropping non-string / sentinel entries. */
function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((v): v is string => typeof v === "string")
}

/** Render a scalar for the environment blocks: strings verbatim, else compact text. */
function formatScalar(value: unknown): string {
  if (value === null || value === undefined) return ""
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return formatValue(value)
}

/** One labeled part (Input / Processing / Output) of the trace panel. */
function TracePart({
  label,
  part,
  children,
}: {
  label: string
  part: "input" | "processing" | "output"
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-1.5" data-part={part}>
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <div className="flex flex-col gap-1.5">{children}</div>
    </div>
  )
}

/** A compact list of key → value trace entries, with the "not available" sentinel honored. */
function TraceEntries({
  entries,
  notAvailableLabel,
}: {
  entries: (readonly [string, unknown])[]
  notAvailableLabel: string
}) {
  const { t } = useTranslation("pipeline")
  return (
    <dl className="flex flex-col gap-1.5">
      {entries.map(([key, value]) => (
        <div key={key} className="flex flex-col gap-0.5" data-entry={key}>
          <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t(`aiRun.trace.field.${key}`, { defaultValue: key })}
          </dt>
          <dd className="text-xs text-muted-foreground">
            {isNotAvailable(value) ? (
              <NotAvailable label={notAvailableLabel} />
            ) : (
              <span className="whitespace-pre-wrap break-words">{formatValue(value)}</span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  )
}

/** The explicit "not available" marker rendered for a sentinel/absent element (Req. 1.6). */
function NotAvailable({ label }: { label: string }) {
  return (
    <span
      className="inline-flex items-center rounded border border-dashed px-1.5 py-0.5 text-xs text-muted-foreground"
      data-section="agent-trace-not-available"
    >
      {label}
    </span>
  )
}

/** Whether a trace value is the backend's explicit "genuinely absent" sentinel. */
function isNotAvailable(value: unknown): boolean {
  return value === NOT_AVAILABLE_SENTINEL || value === null || value === undefined
}

/** Render a trace value as compact text: strings verbatim, structures as pretty JSON. */
function formatValue(value: unknown): string {
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
