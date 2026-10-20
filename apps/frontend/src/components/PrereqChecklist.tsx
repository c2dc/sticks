import type { ComponentType } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, Circle, ShieldAlert, XCircle } from "lucide-react"

import { StageSection } from "@/components/StageSection"
import { cn } from "@/lib/utils"
import type { StageState } from "@/lib/campaigns"
import type {
  IsolationSummary,
  HealthSummary,
  ContainerVerdict,
  HealthVerdict,
  SubStageSummary,
  EgressVerdictView,
} from "@/lib/stages"

/**
 * PrereqChecklist — Stage 5 prerequisites confirmation (Req. 5.6, 6.3, 6.4,
 * 7.2–7.4, 8.2).
 *
 * Lists the execution prerequisites that must be satisfied and verified before
 * unlocking Stage 6 (Req. 5.6, 6.3). Besides the three inherited items, the
 * stage5-target-provisioning sub-stages add two more (Req. 6.3):
 *
 * 1. **Healthy containers** — derived from `health: HealthSummary`; on failure
 *    the per-container `HealthVerdict`s carry the `reason` of the failing
 *    container. Steps `subir_conteineres`/`aguardar_saude`.
 * 2. **Online prerequisites obtained** (NEW, sub-stage 5.1) — derived from
 *    `substages.build_online_ok`; steps `build_online`/`encerrar_egresso`.
 * 3. **Egress lost (verified)** (NEW, sub-stage 5.2) — derived from
 *    `substages.egress_lost_verified`; steps `reisolar`/`verificar_egresso`/
 *    `setup_offline`.
 * 4. **Abilities loaded into Caldera** — derived from `abilitiesLoaded`; step
 *    `carregar_abilities`.
 * 5. **Isolation approved** — derived from `isolation: IsolationSummary`; on
 *    failure the `ContainerVerdict`s name the non-isolated container and the
 *    `reasons` for the failure (Req. 7.4). Step `verificar_isolamento`.
 *
 * Each item has exactly one of three states — `pendente` / `ok` / `falha`
 * (Req. 5.6). The per-item derivation (see {@link derivePrereqStates}) combines
 * the failing step (`failedStep`) with the health/substages/abilities/isolation
 * data and the aggregate stage state:
 *
 * - When `state === "concluido"`, all items are `ok`.
 * - When `failedStep` identifies an item's step, that item is `falha` (with the
 *   cause), the **preceding** items that passed are `ok` and the **following**
 *   ones are `pendente`.
 * - Otherwise, it uses the data: `health.approved`, `substages.build_online_ok`,
 *   `substages.egress_lost_verified`, `abilitiesLoaded`, `isolation.approved` →
 *   `ok`/`falha`/`pendente`.
 * - When nothing ran yet (`state === "nao_iniciado"` with no data), all items
 *   are `pendente`.
 *
 * On any `falha` (or `state === "erro"`), it shows a clear **Stage 6 blocked**
 * indicator (Req. 7.2, 7.3, 8.2). When all items are `ok` (`state ===
 * "concluido"`), it shows the confirmation that the environment is ready
 * (Req. 5.6).
 */

/** The state of an individual prerequisite (Req. 5.6). */
export type PrereqState = "pendente" | "ok" | "falha"

/**
 * The provisioning-pipeline steps (`failed_step` of the StageRun /
 * StageProgressEvent) mapped to each prerequisite. The union stays open
 * (`string & {}`) for new steps without breaking the contract.
 */
export type ProvisioningStep =
  | "subir_conteineres"
  | "aguardar_saude"
  | "build_online"
  | "encerrar_egresso"
  | "reisolar"
  | "verificar_egresso"
  | "setup_offline"
  | "carregar_abilities"
  | "verificar_isolamento"
  | "concluir"
  | (string & {})

/**
 * The five steps that map 1:1 to the displayed prerequisites (health, online
 * prerequisites, egress lost, abilities, isolation). It is the key domain
 * {@link derivePrereqStates} always returns — a closed subset of
 * {@link ProvisioningStep}.
 */
export type PrereqStep =
  | "aguardar_saude"
  | "build_online"
  | "verificar_egresso"
  | "carregar_abilities"
  | "verificar_isolamento"

/** Props of {@link PrereqChecklist}. */
export interface PrereqChecklistProps {
  /** Aggregate Stage 5 state (StageRun / StageProgressEvent). */
  state: StageState
  /** Health summary of the target containers (Req. 5.1, 5.6). */
  health: HealthSummary
  /** Isolation summary of the target containers (Req. 5.3, 7.4). */
  isolation: IsolationSummary
  /** `true` iff the ordered Abilities were loaded into Caldera (Req. 5.2). */
  abilitiesLoaded: boolean
  /**
   * Outcome of the Stage-5 sub-stages 5.1/5.2 (Req. 6.3). When omitted, the two
   * new items ("online prerequisites obtained", "egress lost verified") stay
   * `pendente` unless a `failedStep`/`state` says otherwise.
   */
  substages?: SubStageSummary | null
  /** Step that failed (`null`/omitted when there is no failure) (Req. 7.1, 7.2). */
  failedStep?: string | null
  /** Readable failure cause (`null`/omitted when there is no failure) (Req. 7.2). */
  errorMessage?: string | null
  /**
   * `true` iff Stage 6 is blocked. When omitted, it is derived from
   * `state !== "concluido"` (sequential gating — Req. 7.3).
   */
  stage6Blocked?: boolean
  className?: string
}

/** The visual/semantic identity of each of the three prerequisites. */
interface ChecklistItem {
  /** Stable key (also used as the list `key`). */
  id:
    | "conteineresSaudaveis"
    | "prerequisitosOnline"
    | "egressoPerdido"
    | "abilitiesCarregadas"
    | "isolamentoAprovado"
  /** Pipeline step that decides this item (to match `failedStep`). */
  step: ProvisioningStep
  state: PrereqState
  label: string
  /** Readable cause when `falha`; `null` otherwise (Req. 7.2). */
  cause: string | null
  /** Per-container health verdicts (only on the health item). */
  health?: readonly HealthVerdict[]
  /** Per-container isolation verdicts (only on the isolation item). */
  isolation?: readonly ContainerVerdict[]
}

/** Icon + color class per prerequisite state (see EnvironmentStageView). */
const STATE_VISUAL: Record<
  PrereqState,
  { Icon: ComponentType<{ className?: string }>; color: string; statusKey: string }
> = {
  ok: { Icon: CheckCircle2, color: "text-team-blue", statusKey: "ok" },
  falha: { Icon: XCircle, color: "text-team-red", statusKey: "falha" },
  pendente: { Icon: Circle, color: "text-muted-foreground", statusKey: "pendente" },
}

/**
 * Canonical order of the five prerequisites (reflects the pipeline order:
 * health → 5.1 build online → 5.2 runtime egress loss → abilities → isolation).
 */
const STEP_ORDER: readonly PrereqStep[] = [
  "aguardar_saude",
  "build_online",
  "verificar_egresso",
  "carregar_abilities",
  "verificar_isolamento",
]

/**
 * Collapse a raw `failed_step` onto the {@link PrereqStep} of the item it should
 * highlight. The pipeline has more steps than displayed items, so several steps
 * fold into the same item:
 *
 * - `subir_conteineres` → `aguardar_saude` (fails before health).
 * - `build_online` / `encerrar_egresso` → `build_online` (sub-stage 5.1).
 * - `reisolar` / `verificar_egresso` / `setup_offline` → `verificar_egresso`
 *   (sub-stage 5.2, egress loss verified then offline setup).
 * - `carregar_abilities` → itself.
 * - `verificar_isolamento` → itself.
 */
function failedStepToItem(failedStep: string | null | undefined): PrereqStep | null {
  switch (failedStep) {
    case "subir_conteineres":
    case "aguardar_saude":
      return "aguardar_saude"
    case "build_online":
    case "encerrar_egresso":
      return "build_online"
    case "reisolar":
    case "verificar_egresso":
    case "setup_offline":
      return "verificar_egresso"
    case "carregar_abilities":
      return "carregar_abilities"
    case "verificar_isolamento":
      return "verificar_isolamento"
    default:
      return null
  }
}

/**
 * Derive the state (`pendente`/`ok`/`falha`) of the five prerequisites from the
 * aggregate state, the failing step and the health/substages/abilities/isolation
 * data (see the {@link PrereqChecklist} doc). Exported for testing.
 */
export function derivePrereqStates({
  state,
  health,
  isolation,
  abilitiesLoaded,
  substages,
  failedStep,
}: {
  state: StageState
  health: HealthSummary
  isolation: IsolationSummary
  abilitiesLoaded: boolean
  substages?: SubStageSummary | null
  failedStep?: string | null
}): Record<PrereqStep, PrereqState> {
  // Concluded: every prerequisite passed (Req. 5.5, 5.6, 6.3).
  if (state === "concluido") {
    return {
      aguardar_saude: "ok",
      build_online: "ok",
      verificar_egresso: "ok",
      carregar_abilities: "ok",
      verificar_isolamento: "ok",
    }
  }

  // The failing step maps to the prerequisite it should highlight.
  const itemStep = failedStepToItem(failedStep)

  if (itemStep) {
    const failIndex = STEP_ORDER.indexOf(itemStep)
    const result = {} as Record<PrereqStep, PrereqState>
    STEP_ORDER.forEach((step, index) => {
      // Items before the failure passed; the failing item is `falha`;
      // the following ones have not run yet (`pendente`).
      result[step] =
        index < failIndex ? "ok" : index === failIndex ? "falha" : "pendente"
    })
    return result
  }

  // No failing step identified: use the per-item verified data. A "not
  // approved" summary with no data (not run yet) stays `pendente`.
  const healthState: PrereqState = health.approved
    ? "ok"
    : health.verdicts.length > 0
      ? "falha"
      : "pendente"
  const onlineState: PrereqState = substages?.build_online_ok ? "ok" : "pendente"
  const egressState: PrereqState = substages?.egress_lost_verified
    ? "ok"
    : "pendente"
  const abilitiesState: PrereqState = abilitiesLoaded ? "ok" : "pendente"
  const isolationState: PrereqState = isolation.approved
    ? "ok"
    : isolation.verdicts.length > 0
      ? "falha"
      : "pendente"

  return {
    aguardar_saude: healthState,
    build_online: onlineState,
    verificar_egresso: egressState,
    carregar_abilities: abilitiesState,
    verificar_isolamento: isolationState,
  }
}

/** Build the readable reason per failing health container (Req. 7.2). */
function healthReason(verdicts: readonly HealthVerdict[]): string | null {
  const failing = verdicts.filter((v) => !v.running || v.healthy === false)
  if (failing.length === 0) return null
  return failing
    .map((v) => `${v.container}: ${v.reason ?? ""}`.trim().replace(/:\s*$/, ""))
    .join(" · ")
}

/**
 * Build the readable reason per non-isolated container, naming the container and
 * the reason for the failure (Req. 7.4).
 */
function isolationReason(verdicts: readonly ContainerVerdict[]): string | null {
  const failing = verdicts.filter((v) => !v.isolated)
  if (failing.length === 0) return null
  return failing
    .map((v) => `${v.container}: ${v.reasons.join(", ")}`.replace(/:\s*$/, ""))
    .join(" · ")
}

/**
 * Build the readable reason per host whose egress loss was NOT positively
 * verified (egress still present or the probe was non-conclusive — fail-closed),
 * naming the container and the probe outcome (Req. 8.2, 5.5).
 */
function egressReason(verdicts: readonly EgressVerdictView[]): string | null {
  const failing = verdicts.filter((v) => !(v.egress_lost && v.conclusive))
  if (failing.length === 0) return null
  return failing
    .map((v) => `${v.container}: ${v.reason}`.trim().replace(/:\s*$/, ""))
    .join(" · ")
}

export function PrereqChecklist({
  state,
  health,
  isolation,
  abilitiesLoaded,
  substages,
  failedStep,
  errorMessage,
  stage6Blocked,
  className,
}: PrereqChecklistProps) {
  const { t } = useTranslation("pipeline")

  const states = derivePrereqStates({
    state,
    health,
    isolation,
    abilitiesLoaded,
    substages,
    failedStep,
  })

  // Per-item cause: prefers the `errorMessage` of the failing step; completes
  // with the per-container reasons (health/isolation/egress) to name the offender.
  const healthCause =
    states.aguardar_saude === "falha"
      ? healthReason(health.verdicts) ?? errorMessage ?? null
      : null
  const onlineCause =
    states.build_online === "falha" ? errorMessage ?? null : null
  const egressCause =
    states.verificar_egresso === "falha"
      ? egressReason(substages?.egress_verdicts ?? []) ?? errorMessage ?? null
      : null
  const abilitiesCause =
    states.carregar_abilities === "falha" ? errorMessage ?? null : null
  const isolationCause =
    states.verificar_isolamento === "falha"
      ? isolationReason(isolation.verdicts) ?? errorMessage ?? null
      : null

  const items: ChecklistItem[] = [
    {
      id: "conteineresSaudaveis",
      step: "aguardar_saude",
      state: states.aguardar_saude,
      label: t("stage5.provisionamento.prerequisitos.conteineresSaudaveis", {
        defaultValue: "Contêineres saudáveis",
      }),
      cause: healthCause,
      health: health.verdicts,
    },
    {
      id: "prerequisitosOnline",
      step: "build_online",
      state: states.build_online,
      label: t("stage5.provisionamento.prerequisitos.prerequisitosOnline", {
        defaultValue: "Pré-requisitos online obtidos",
      }),
      cause: onlineCause,
    },
    {
      id: "egressoPerdido",
      step: "verificar_egresso",
      state: states.verificar_egresso,
      label: t("stage5.provisionamento.prerequisitos.egressoPerdido", {
        defaultValue: "Egresso perdido (verificado)",
      }),
      cause: egressCause,
    },
    {
      id: "abilitiesCarregadas",
      step: "carregar_abilities",
      state: states.carregar_abilities,
      label: t("stage5.provisionamento.prerequisitos.abilitiesCarregadas", {
        defaultValue: "Abilities carregadas no Caldera",
      }),
      cause: abilitiesCause,
    },
    {
      id: "isolamentoAprovado",
      step: "verificar_isolamento",
      state: states.verificar_isolamento,
      label: t("stage5.provisionamento.prerequisitos.isolamentoAprovado", {
        defaultValue: "Isolamento aprovado",
      }),
      cause: isolationCause,
      isolation: isolation.verdicts,
    },
  ]

  const hasFailure = state === "erro" || items.some((item) => item.state === "falha")
  const ready = state === "concluido" && items.every((item) => item.state === "ok")
  // Stage 6 blocked: explicit via the prop or derived from the state (Req. 7.3).
  const blocked = stage6Blocked ?? state !== "concluido"

  return (
    <StageSection
      data-section="prerequisitos"
      label={t("stage5.provisionamento.prerequisitos.titulo", {
        defaultValue: "Pré-requisitos de execução",
      })}
      kind={hasFailure ? "red" : ready ? "blue" : "neutral"}
      className={className}
    >
      <ul className="flex flex-col gap-2">
        {items.map((item) => (
          <PrereqItem key={item.id} item={item} />
        ))}
      </ul>

      {/* Blocking failure: clearly indicates Stage 6 blocked (Req. 7.2, 7.3). */}
      {hasFailure ? (
        <div
          className="flex flex-col gap-1 rounded-md border border-team-red/40 bg-team-red/10 p-2.5"
          role="alert"
        >
          <span className="inline-flex items-center gap-1.5 text-sm font-medium text-team-red">
            <ShieldAlert className="size-4" aria-hidden />
            {t("stage5.provisionamento.falha.titulo", {
              defaultValue: "Provisionamento falhou",
            })}
          </span>
          {errorMessage ? (
            <p className="text-xs text-team-red">{errorMessage}</p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            {t("stage5.provisionamento.falha.estagio6Bloqueado", {
              defaultValue:
                "O Estágio 6 (Execução da Campanha) permanece bloqueado até que o provisionamento conclua.",
            })}
          </p>
        </div>
      ) : ready ? (
        /* Environment-ready confirmation when all three pass (Req. 5.6). */
        <div
          className="flex items-start gap-1.5 rounded-md border border-team-blue/40 bg-team-blue/10 p-2.5"
          role="status"
        >
          <CheckCircle2 className="mt-px size-4 shrink-0 text-team-blue" aria-hidden />
          <p className="text-sm font-medium text-team-blue">
            {t("stage5.provisionamento.prerequisitos.pronto", {
              defaultValue:
                "Ambiente pronto — todos os pré-requisitos foram verificados.",
            })}
          </p>
        </div>
      ) : blocked ? (
        /* While pending/in-progress: Stage 6 stays blocked (Req. 7.3). */
        <p className="text-xs text-muted-foreground">
          {t("stage5.provisionamento.falha.estagio6Bloqueado", {
            defaultValue:
              "O Estágio 6 (Execução da Campanha) permanece bloqueado até que o provisionamento conclua.",
          })}
        </p>
      ) : null}
    </StageSection>
  )
}

/**
 * One checklist item: icon per state (Req. 5.6), label, status badge and — on
 * failure — the cause (and the per-container reasons, naming the offending
 * resource, Req. 7.2, 7.4).
 */
function PrereqItem({ item }: { item: ChecklistItem }) {
  const { t } = useTranslation("pipeline")
  const { Icon, color, statusKey } = STATE_VISUAL[item.state]

  const statusDefault =
    statusKey === "ok" ? "OK" : statusKey === "falha" ? "Falha" : "Pendente"
  const statusLabel = t(`stage5.provisionamento.prerequisitos.${statusKey}`, {
    defaultValue: statusDefault,
  })

  return (
    <li className="flex flex-col gap-1 rounded-md border p-2">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-sm">
          <Icon className={cn("size-4 shrink-0", color)} aria-hidden />
          {item.label}
        </span>
        <span
          className={cn(
            "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium",
            item.state === "ok" && "bg-team-blue/10 text-team-blue",
            item.state === "falha" && "bg-team-red/10 text-team-red",
            item.state === "pendente" && "bg-muted text-muted-foreground",
          )}
        >
          {statusLabel}
        </span>
      </div>

      {/* Readable failure cause for this item, consistent with the console (Req. 7.2, 7.5). */}
      {item.state === "falha" && item.cause ? (
        <p className="text-xs text-team-red">{item.cause}</p>
      ) : null}
    </li>
  )
}
