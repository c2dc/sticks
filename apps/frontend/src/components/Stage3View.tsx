import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Loader2,
  ShieldAlert,
  WifiOff,
  XCircle,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { EmptyNote, StageSection } from "@/components/StageSection"
import { MermaidDiagram } from "@/components/MermaidDiagram"
import { cn } from "@/lib/utils"
import {
  connectOperation,
  getStage3,
  getTopology,
  startExecution,
  type AbilityResult,
  type AbilityResultStatus,
  type OperationResultView,
  type OperationState,
  type Stage3Input,
  type Stage3Result,
  type Stage5Verdict,
  type Stage6StartResult,
  type TopologyResponse,
} from "@/lib/stages"
import { getEmulationPreview, type EmulationPreview } from "@/lib/emulation"

/** Delay (ms) before the embedded auto-start / Stage-5 redirect fires (≤ 3s, Req. 1.1/1.4). */
const AUTO_START_DELAY_MS = 3000

/** Operation states that count as a persisted (terminal) result — idempotency (Req. 1.5). */
const TERMINAL_OPERATION_STATES: ReadonlySet<OperationState> = new Set<OperationState>([
  "concluida",
  "falha",
  "incompleta_timeout",
])

/** True when a persisted Operation already carries a terminal result (Req. 1.5). */
function hasPersistedResult(output: OperationResultView[] | undefined): boolean {
  if (!output || output.length === 0) return false
  return output.some(
    (op) =>
      TERMINAL_OPERATION_STATES.has(op.state) ||
      op.results.length > 0 ||
      op.total_success > 0 ||
      op.total_failure > 0,
  )
}

/** A pending placeholder for an Ability that has no live result yet (Req. 2.1). */
function pendingResult(abilityId: string): AbilityResult {
  return { ability_id: abilityId, status: "pendente", command_output: "" }
}

/**
 * Build the canonical, execution-ordered list of per-Ability results
 * (Req. 2.6, 2.7).
 *
 * The order is driven by the campaign's atomic ordering (`adversary.atomic_ordering`,
 * falling back to the Input Abilities order) so Abilities are presented from the
 * first to the last and are NOT re-sorted while the Operation runs — the live
 * merge stays order-stable instead of following object-key insertion order.
 *
 * Each `ability_id` appears exactly once (canonical, no duplication, Req. 2.7):
 * known Abilities render in order with their live result (or a `pendente`
 * placeholder when no frame has arrived yet), and any extra live result not in
 * the ordering is appended once at the end so nothing observed is dropped.
 */
function orderedAbilityResults(
  input: Stage3Input,
  live: Record<string, AbilityResult>,
): AbilityResult[] {
  const ordering =
    input.adversary && input.adversary.atomic_ordering.length > 0
      ? input.adversary.atomic_ordering
      : input.abilities.map((a) => a.ability_id)

  const seen = new Set<string>()
  const result: AbilityResult[] = []
  for (const abilityId of ordering) {
    if (seen.has(abilityId)) continue
    seen.add(abilityId)
    result.push(live[abilityId] ?? pendingResult(abilityId))
  }
  // Append any live result not covered by the ordering, once each (Req. 2.7).
  for (const [abilityId, r] of Object.entries(live)) {
    if (seen.has(abilityId)) continue
    seen.add(abilityId)
    result.push(r)
  }
  return result
}

/**
 * Stage3View — Stage 6: Execution, measurement and report (redesign).
 *
 * - Input: the Adversary + curated Abilities of the case (Req. 4.1).
 * - Processing: per-Ability status in real time. When an Operation exists,
 *   it subscribes to `WS /ws/operation/{operation}` and reflects per-Ability
 *   status frames as they arrive (Req. 4.3).
 * - Output: per-Ability result (status + command output) and the aggregate
 *   success/failure totals when the Operation reaches its final state
 *   (Req. 4.4, 4.5).
 *
 * Auto-start (Task 13.1, Req. 1): on mount, after the `GET /stage/6` snapshot,
 * if there is NO persisted result and the prerequisites are OK, the execution
 * starts automatically within 3s through an EMBEDDED inline consent — no modal
 * or pop-up (Req. 1.1, 1.3). When a terminal result is already persisted the
 * view re-displays it WITHOUT re-executing (idempotency, Req. 1.5). When
 * Stage 5 is not concluded the auto-start is blocked and the researcher is
 * redirected back to Stage 5 within ≤ 3s (Req. 1.4) via the `onRedirectToStage`
 * seam.
 *
 * The safety confirmation (Req. 1.6) is the embedded {@link
 * InlineExecutionConfirmation} section — never a `Dialog`/modal.
 */
export function Stage3View({
  campaignId,
  onRedirectToStage,
}: {
  campaignId: string
  /**
   * Seam invoked to send the researcher back to Stage 5 when the environment is
   * not ready (auto-start blocked / `redirect_to_stage === 5`, Req. 1.4). The
   * redirect fires within ≤ 3s of detecting the block.
   */
  onRedirectToStage?: (stage: number) => void
}) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<Stage3Result | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Auto-start / inline-confirmation state (Req. 1).
  // `phase` drives the embedded Stage-6 execution lifecycle:
  //  - "idle"       : snapshot not loaded yet
  //  - "confirming" : awaiting explicit inline consent (Req. 1.6)
  //  - "starting"   : consent given, POST /stage/6/execute in flight
  //  - "running"    : the runner started (live follow-up, Req. 2)
  //  - "persisted"  : a terminal result was re-displayed, no run (Req. 1.5)
  //  - "blocked"    : Stage 5 incomplete; redirecting to Stage 5 (Req. 1.4)
  //  - "refused"    : the runner refused (isolation/containment/unavailable, Req. 1.2/6.2)
  type ExecutionPhase =
    | "idle"
    | "confirming"
    | "starting"
    | "running"
    | "persisted"
    | "blocked"
    | "refused"
  const [phase, setPhase] = useState<ExecutionPhase>("idle")
  const [startError, setStartError] = useState<string | null>(null)
  const [startResult, setStartResult] = useState<Stage6StartResult | null>(null)
  // Command/target preview shown inside the inline confirmation (Req. 1.6, 6.5).
  const [preview, setPreview] = useState<EmulationPreview | null>(null)
  // Guards so the auto-start / redirect timers fire at most once per campaign.
  const startedRef = useRef(false)

  // Live per-Ability status + aggregate, seeded from the REST snapshot and then
  // updated in real time from the operation channel (Req. 4.3, 4.4, 4.5).
  const [liveResults, setLiveResults] = useState<Record<string, AbilityResult>>({})
  const [liveAggregate, setLiveAggregate] = useState<{
    total_success: number
    total_failure: number
  } | null>(null)

  // Contained target-network topology (read-only). Shown in Processing as
  // the environment the campaign is emulated against (Mermaid diagram + nets).
  const [topology, setTopology] = useState<TopologyResponse | null>(null)

  // Read-only Stage-5 environment verdict surfaced in the Stage-6 snapshot
  // (Req. 5.4). Stage 6 only reads it — never re-runs any verification.
  const [stage5, setStage5] = useState<Stage5Verdict | null>(null)

  // Live connection state of the Operation channel (Req. 2.8). `false` while the
  // WS is dropped so the UI shows a "connection lost" indicator; it flips back to
  // `true` on reconnect, resuming updates without reloading the page.
  const [wsConnected, setWsConnected] = useState(true)

  useEffect(() => {
    let active = true
    setTopology(null)
    getTopology(campaignId)
      .then((data) => {
        if (active) setTopology(data)
      })
      .catch(() => {
        /* topology is best-effort context; its absence must not block Stage 3 */
      })
    return () => {
      active = false
    }
  }, [campaignId])

  useEffect(() => {
    let active = true
    setResult(null)
    setLoadError(null)
    setLiveResults({})
    setLiveAggregate(null)
    setStage5(null)
    setWsConnected(true)
    // Reset the auto-start lifecycle for the (re)loaded campaign.
    startedRef.current = false
    setPhase("idle")
    setStartError(null)
    setStartResult(null)
    setPreview(null)
    getStage3(campaignId)
      .then((data) => {
        if (!active) return
        setResult(data)
        // Read-only Stage-5 verdict from the snapshot (Req. 5.4); may be absent.
        setStage5(data.stage5 ?? null)
        const seed: Record<string, AbilityResult> = {}
        for (const op of data.output) {
          for (const r of op.results) seed[r.ability_id] = r
        }
        setLiveResults(seed)

        // Idempotency (Req. 1.5): a persisted terminal result is re-displayed
        // as-is; the campaign is NOT re-executed.
        if (hasPersistedResult(data.output)) {
          setPhase("persisted")
          return
        }

        // Stage 5 not concluded (environment not ready): the backend reports it
        // via `can_emulate=false`. Block and redirect to Stage 5 within ≤ 3s
        // (Req. 1.4). We do not call the execute endpoint on this path.
        if (!data.input.can_emulate) {
          setPhase("blocked")
          return
        }

        // Prerequisites OK and nothing persisted → render the embedded inline
        // safety confirmation (Req. 1.6) and prime the command/target preview.
        setPhase("confirming")
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [campaignId, t])

  // Prime the command/target preview for the inline confirmation (Req. 1.6,
  // 6.5). Best-effort: its absence must not prevent explicit consent.
  useEffect(() => {
    if (phase !== "confirming") return
    let active = true
    setPreview(null)
    getEmulationPreview(campaignId)
      .then((data) => {
        if (active) setPreview(data)
      })
      .catch(() => {
        /* preview is best-effort context; consent stays available without it */
      })
    return () => {
      active = false
    }
  }, [phase, campaignId])

  // Redirect back to Stage 5 within ≤ 3s when the environment is not ready
  // (Req. 1.4). Fires once per blocked load.
  useEffect(() => {
    if (phase !== "blocked") return
    if (!onRedirectToStage) return
    const timer = window.setTimeout(() => onRedirectToStage(5), AUTO_START_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [phase, onRedirectToStage])

  // Fire the auto-start (via explicit inline consent) within 3s of rendering
  // the confirmation (Req. 1.1). `startExecution` carries the inline-consent
  // `confirmed=true`; the backend enforces the isolation gate (Req. 6.5).
  const runAutoStart = useCallback(async () => {
    if (startedRef.current) return
    startedRef.current = true
    setPhase("starting")
    setStartError(null)
    try {
      const res = await startExecution(campaignId, true)
      setStartResult(res)
      if (res.blocked) {
        // Stage 5 incomplete reported by the runner → redirect (Req. 1.4).
        setPhase("blocked")
        return
      }
      if (res.output) {
        // The runner started (or re-displayed): seed the per-Ability view and
        // follow the Operation live (Req. 2). Idempotent re-display also lands
        // here with the persisted snapshot.
        const seed: Record<string, AbilityResult> = {}
        for (const r of res.output.results) seed[r.ability_id] = r
        setLiveResults(seed)
        setLiveAggregate({
          total_success: res.output.total_success,
          total_failure: res.output.total_failure,
        })
        setResult((prev) =>
          prev ? { ...prev, output: [...prev.output, res.output as OperationResultView] } : prev,
        )
        setPhase(res.reused ? "persisted" : "running")
        return
      }
      // Not executed by the runner (missing confirmation, containment/isolation
      // violation, Caldera unavailable): surface the cause embedded (Req. 1.2,
      // 6.2) without any pop-up.
      setPhase("refused")
      setStartError(res.message ?? res.error_message ?? t("stage6.execution.refused"))
    } catch {
      setPhase("refused")
      setStartError(t("stage6.execution.startError"))
    }
  }, [campaignId, t])

  // Auto-start timer: once the inline confirmation is showing, trigger the
  // execution within 3s (Req. 1.1). Consent is implicit in entering Stage 6
  // with a ready environment; the embedded confirmation renders the preview and
  // the explicit control, and never opens a modal (Req. 1.3, 1.6).
  useEffect(() => {
    if (phase !== "confirming") return
    const timer = window.setTimeout(() => {
      void runAutoStart()
    }, AUTO_START_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [phase, runAutoStart])

  // The current (latest) Operation, if any. We subscribe to its channel while
  // it exists so status updates stream in (Req. 4.3).
  const currentOperation: OperationResultView | undefined = useMemo(
    () => result?.output[result.output.length - 1],
    [result],
  )
  const operationId = currentOperation?.operation_id

  useEffect(() => {
    if (!operationId) return
    // A fresh subscription starts optimistically connected; the lifecycle
    // callbacks below flip it on drop/reconnect (Req. 2.8).
    setWsConnected(true)
    const connection = connectOperation(operationId, {
      onFrame: (frame) => {
        if (frame.type === "ability") {
          // Merge the enriched per-Ability fields (command/target/host/output)
          // so a frame that omits one does not wipe a value already received
          // (Req. 2.4, 2.5). Non-null incoming values win; absent keys keep the
          // prior datum. The status always reflects the latest frame (Req. 2.1).
          setLiveResults((prev) => {
            const previous = prev[frame.ability_id]
            const merged: AbilityResult = {
              ability_id: frame.ability_id,
              status: frame.status,
              command_output: frame.command_output ?? previous?.command_output ?? "",
              command: frame.command ?? previous?.command ?? null,
              target_container:
                frame.target_container ?? previous?.target_container ?? null,
              attacker_host: frame.attacker_host ?? previous?.attacker_host ?? null,
            }
            return { ...prev, [frame.ability_id]: merged }
          })
        } else {
          setLiveAggregate({
            total_success: frame.total_success,
            total_failure: frame.total_failure,
          })
        }
      },
      // Connection-lost indicator + resume on reconnect without reload (Req. 2.8).
      onOpen: () => setWsConnected(true),
      onClose: () => setWsConnected(false),
    })
    return () => connection.close()
  }, [operationId])

  if (loadError) return <EmptyNote>{loadError}</EmptyNote>
  if (!result) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  const { input } = result
  // Canonical, execution-ordered per-Ability list — one entry per Ability, in
  // atomic ordering, never re-sorted while running (Req. 2.6, 2.7).
  const abilityResults = orderedAbilityResults(input, liveResults)
  const aggregate = liveAggregate ?? {
    total_success: currentOperation?.total_success ?? 0,
    total_failure: currentOperation?.total_failure ?? 0,
  }
  const hasOperation = Boolean(operationId)

  return (
    <div className="flex flex-col gap-4" data-testid="stage6-view">
      {/* Embedded execution lifecycle — auto-start, inline confirmation,
          blocked/redirect and refusal — all inside the page, never a modal
          (Req. 1.1, 1.3, 1.4, 1.6). */}
      {phase === "confirming" || phase === "starting" ? (
        <InlineExecutionConfirmation
          preview={preview}
          starting={phase === "starting"}
          onConfirm={() => void runAutoStart()}
        />
      ) : null}

      {phase === "blocked" ? (
        <section
          data-testid="stage6-blocked"
          role="alert"
          className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-600 dark:text-amber-400"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>
            {startResult?.message ?? t("stage6.blocked.body")}
          </span>
        </section>
      ) : null}

      {phase === "refused" ? (
        <section
          data-testid="stage6-refused"
          role="alert"
          className="flex items-start gap-2 rounded-md border border-team-red/40 bg-team-red/5 p-3 text-sm text-team-red"
        >
          <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{startError ?? t("stage6.execution.refused")}</span>
        </section>
      ) : null}

      {/* Connection-lost indicator (Req. 2.8). Shown only while following a live
          Operation whose channel dropped; the channel reconnects on its own and
          this banner clears on `onOpen`, resuming updates without a page reload. */}
      {hasOperation && !wsConnected ? (
        <section
          data-testid="stage6-connection-lost"
          role="status"
          className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-600 dark:text-amber-400"
        >
          <WifiOff className="size-4 shrink-0" aria-hidden="true" />
          <span>{t("stage6.connection.lost")}</span>
        </section>
      ) : null}

      <div className="grid gap-4 md:grid-cols-3">
      {/* Input — Adversary + Abilities to emulate (Req. 4.1). */}
      <StageSection data-section="entrada" label={t("sections.entrada")} kind="red">
        {input.adversary ? (
          <div className="flex flex-col gap-0.5">
            <span className="text-xs font-medium text-muted-foreground">
              {t("stage3.entrada.adversary")}
            </span>
            <span className="text-sm font-medium">{input.adversary.name}</span>
          </div>
        ) : (
          <EmptyNote>{t("stage3.entrada.noAdversary")}</EmptyNote>
        )}
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium text-muted-foreground">
            {t("stage3.entrada.abilities", { count: input.abilities.length })}
          </span>
          <ul className="flex flex-wrap gap-1.5">
            {input.abilities.map((ability) => (
              <li
                key={ability.ability_id}
                className="rounded-md border border-team-red/30 bg-team-red/5 px-2 py-0.5 text-xs"
                title={ability.description}
              >
                {ability.name}
              </li>
            ))}
          </ul>
        </div>
        {/* Stage 6 execution starts through the embedded inline confirmation
            below (Req. 1.1, 1.3) — no legacy "request emulation" button. */}
        {!input.can_emulate && input.note ? (
          <p className="text-xs text-muted-foreground">{input.note}</p>
        ) : null}
      </StageSection>

      {/* Processing — target environment (contained topology) + per-Ability
          status in real time (Req. 4.3). The topology is read-only context: it
          shows the isolated network the campaign is emulated against. */}
      <StageSection
        data-section="processamento"
        label={t("sections.processamento")}
        kind="purple"
      >
        {topology ? (
          <div className="mb-3 flex flex-col gap-2 rounded-md border p-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t("stage3.ambiente.titulo")}
              </span>
              {topology.isolated ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-team-blue/10 px-2 py-0.5 text-[11px] text-team-blue">
                  <CheckCircle2 className="size-3" aria-hidden />
                  {t("stage3.ambiente.isolado")}
                </span>
              ) : null}
            </div>
            <p className="text-xs text-muted-foreground">
              {t("stage3.ambiente.descricao")}
            </p>
            <MermaidDiagram definition={topology.mermaid} className="w-full overflow-auto" />
            <ul className="flex flex-col gap-0.5">
              {(topology.networks ?? []).map((network) => (
                <li key={network.name} className="flex justify-between gap-2 text-[11px]">
                  <span className="text-muted-foreground">{network.name}</span>
                  <span className="font-mono">{network.subnet}</span>
                </li>
              ))}
            </ul>
            {topology.compose_file ? (
              <p className="truncate text-[11px] text-muted-foreground" title={topology.compose_file}>
                {t("stage3.ambiente.composeFile")}: <code>{topology.compose_file}</code>
              </p>
            ) : null}
          </div>
        ) : null}

        {/* Read-only Stage-5 environment verdict (Req. 5.4). The Stage-6 page
            only READS the already-produced verdict — no provision/verify
            controls — so the environment panel never re-runs any check. */}
        {stage5 ? <Stage5ReadOnlyPanel verdict={stage5} /> : null}

        {!hasOperation ? (
          <EmptyNote>{t("stage3.processamento.noOperation")}</EmptyNote>
        ) : abilityResults.length === 0 ? (
          <p className="text-xs text-muted-foreground">{t("stage3.processamento.pending")}</p>
        ) : (
          // Canonical live tracking per Ability (Req. 2.1–2.7): exactly one of
          // the four states, an activity indicator while `em_execucao`, and the
          // concrete command / target / attacker host / logs — each shown once,
          // with an explicit "não disponível" label for missing data (Req. 2.5).
          <ul className="flex flex-col gap-2" data-testid="stage6-live-abilities">
            {abilityResults.map((r) => (
              <AbilityLiveCard key={r.ability_id} result={r} />
            ))}
          </ul>
        )}
      </StageSection>

      {/* Output — aggregate success/failure totals (Req. 4.5). The per-Ability
          command/target/host/state/logs live canonically in Processing (above);
          this block carries ONLY the aggregate so no Ability datum is duplicated
          across blocks (Req. 2.7). */}
      <StageSection data-section="saida" label={t("sections.saida")} kind="red">
        {!hasOperation ? (
          <EmptyNote>{t("stage3.saida.noResults")}</EmptyNote>
        ) : (
          <div className="flex items-center gap-3 text-sm">
            <span className="inline-flex items-center gap-1 text-team-blue">
              <CheckCircle2 className="size-4" aria-hidden />
              {t("stage3.saida.success", { count: aggregate.total_success })}
            </span>
            <span className="inline-flex items-center gap-1 text-team-red">
              <XCircle className="size-4" aria-hidden />
              {t("stage3.saida.failure", { count: aggregate.total_failure })}
            </span>
          </div>
        )}
      </StageSection>
      </div>
    </div>
  )
}

/**
 * InlineExecutionConfirmation — the embedded safety confirmation for Stage 6
 * (Req. 1.3, 1.6). It renders the preview of concrete commands + target
 * containers and an explicit consent control, EMBEDDED in the page (a section,
 * not a `Dialog`/modal). While `starting` is true the execution is already in
 * flight (the 3s auto-start fired or the researcher clicked), so the control
 * reflects the in-flight state. The isolation/containment gate is enforced by
 * the backend — this section never bypasses it (Req. 6.5).
 */
function InlineExecutionConfirmation({
  preview,
  starting,
  onConfirm,
}: {
  preview: EmulationPreview | null
  starting: boolean
  onConfirm: () => void
}) {
  const { t } = useTranslation("pipeline")
  const abilities = preview?.abilities ?? []

  return (
    <section
      data-testid="stage6-inline-confirmation"
      aria-label={t("stage6.confirmation.title")}
      className="flex flex-col gap-3 rounded-lg border border-team-red/40 bg-team-red/5 p-4"
    >
      <header className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-team-red" aria-hidden="true" />
        <div className="flex flex-col gap-0.5">
          <h3 className="text-sm font-semibold text-team-red">
            {t("stage6.confirmation.title")}
          </h3>
          <p className="text-xs text-muted-foreground">
            {t("stage6.confirmation.description")}
          </p>
        </div>
      </header>

      {/* Preview of the concrete commands + target containers (Req. 1.6, 6.5). */}
      <div className="max-h-64 overflow-y-auto pr-1">
        {abilities.length === 0 ? (
          <p className="text-xs text-muted-foreground" role="status">
            {t("stage6.confirmation.previewLoading")}
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {abilities.map((ability) => (
              <li key={ability.ability_id} className="rounded-md border bg-background p-2">
                <p className="truncate text-xs font-semibold" title={ability.ability_id}>
                  {ability.ability_name}
                </p>
                <ul className="mt-1 flex flex-col gap-1.5">
                  {ability.commands.map((command, index) => (
                    <li key={`${ability.ability_id}-${index}`}>
                      <code className="block max-h-20 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-1.5 py-1 text-[11px]">
                        {command.command}
                      </code>
                      {command.targets.length > 0 ? (
                        <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px]">
                          <span className="text-muted-foreground">
                            {t("stage6.confirmation.targets")}:
                          </span>
                          {command.targets.map((target) => (
                            <span
                              key={target}
                              className="rounded-md border border-team-blue/30 bg-team-blue/5 px-1.5 py-0.5 text-team-blue"
                            >
                              {target}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Explicit consent control. The 3s auto-start also triggers execution;
          the researcher may confirm earlier via this control (Req. 1.1, 1.6). */}
      <div className="flex items-center justify-end">
        <Button
          variant="destructive"
          size="sm"
          onClick={onConfirm}
          disabled={starting}
          aria-disabled={starting}
          data-testid="stage6-confirm-execution"
        >
          {starting ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />
              {t("stage6.confirmation.starting")}
            </>
          ) : (
            t("stage6.confirmation.confirm")
          )}
        </Button>
      </div>
    </section>
  )
}

/**
 * AbilityLiveCard — the canonical live view of a single Ability (Req. 2.1–2.7).
 *
 * Renders EXACTLY ONE of the four mutually-exclusive states (Req. 2.1) via the
 * status badge, an activity indicator while `em_execucao` that is removed on
 * transition to `sucesso`/`falha` (Req. 2.2), and the concrete command, target
 * (container + IP), attacker host and logs/output (Req. 2.4). Any missing/empty
 * datum shows an explicit "não disponível" label — never a blank unlabeled field
 * (Req. 2.5). Each datum appears once here (canonical, Req. 2.7).
 */
function AbilityLiveCard({ result }: { result: AbilityResult }) {
  const { t } = useTranslation("pipeline")
  const running = result.status === "em_execucao"
  const hasOutput = Boolean(result.command_output && result.command_output.trim())

  return (
    <li className="rounded-md border p-2" data-testid="stage6-ability-card" data-status={result.status}>
      <div className="flex items-center gap-2">
        <StatusIcon status={result.status} />
        <span className="min-w-0 flex-1 truncate text-sm font-medium" title={result.ability_id}>
          {result.ability_id}
        </span>
        {/* Activity indicator present only while running (Req. 2.2). */}
        {running ? (
          <span
            data-testid="stage6-ability-activity"
            className="inline-flex items-center gap-1 rounded-full bg-team-purple/10 px-2 py-0.5 text-xs text-team-purple"
          >
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
            {t("stage3.status.em_execucao")}
          </span>
        ) : (
          <span
            className={cn(
              "shrink-0 rounded-full px-2 py-0.5 text-xs",
              result.status === "sucesso" && "bg-team-blue/10 text-team-blue",
              result.status === "falha" && "bg-team-red/10 text-team-red",
              result.status === "pendente" && "bg-muted text-muted-foreground",
            )}
          >
            {t(`stage3.status.${result.status}`)}
          </span>
        )}
      </div>

      {/* Concrete command / target / attacker host — each labelled, each shown
          once, with "não disponível" when absent (Req. 2.4, 2.5). */}
      <dl className="mt-2 flex flex-col gap-1 text-xs">
        <AbilityDatum label={t("stage6.live.command")} value={result.command}>
          {(value) => (
            <code className="block max-h-20 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-1.5 py-1 text-[11px]">
              {value}
            </code>
          )}
        </AbilityDatum>
        <AbilityDatum label={t("stage6.live.target")} value={result.target_container} />
        <AbilityDatum label={t("stage6.live.attacker")} value={result.attacker_host} />
      </dl>

      {/* Logs / output — labelled, with an explicit "não disponível" when the
          output has not been collected yet (Req. 2.4, 2.5). */}
      <div className="mt-2 flex flex-col gap-1 text-xs">
        <span className="font-medium text-muted-foreground">{t("stage6.live.logs")}</span>
        {hasOutput ? (
          <code
            data-testid="stage6-ability-logs"
            className="block max-h-24 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-1.5 py-1 text-[11px]"
          >
            {result.command_output}
          </code>
        ) : (
          <NotAvailable />
        )}
      </div>
    </li>
  )
}

/**
 * A single labelled Ability datum. Renders the value (optionally through
 * `children` for custom formatting) or the explicit "não disponível" indicator
 * when the value is missing/empty — never a blank unlabeled field (Req. 2.5).
 */
function AbilityDatum({
  label,
  value,
  children,
}: {
  label: string
  value: string | null | undefined
  children?: (value: string) => ReactNode
}) {
  const present = typeof value === "string" && value.trim().length > 0
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="font-medium text-muted-foreground">{label}</dt>
      <dd className="min-w-0">
        {present ? (
          children ? (
            children(value as string)
          ) : (
            <span className="break-all">{value}</span>
          )
        ) : (
          <NotAvailable />
        )}
      </dd>
    </div>
  )
}

/** The explicit "não disponível" indicator for a missing/empty datum (Req. 2.5). */
function NotAvailable() {
  const { t } = useTranslation("pipeline")
  return (
    <span
      data-testid="stage6-not-available"
      className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[11px] italic text-muted-foreground"
    >
      {t("stage6.live.notAvailable")}
    </span>
  )
}

/**
 * Stage5ReadOnlyPanel — the Stage-5 environment verdict rendered READ-ONLY in the
 * Stage-6 page (Req. 5.4). It only displays the already-produced verdict
 * (concluded / isolated / reason) with NO provision or verify controls; Stage 6
 * never re-runs any Stage-5 verification (Req. 5.2).
 */
function Stage5ReadOnlyPanel({ verdict }: { verdict: Stage5Verdict }) {
  const { t } = useTranslation("pipeline")
  const ready = verdict.concluded && verdict.isolated
  return (
    <div
      data-testid="stage6-stage5-panel"
      className="mb-3 flex flex-col gap-1.5 rounded-md border p-2"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t("stage6.stage5.title")}
        </span>
        {ready ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-team-blue/10 px-2 py-0.5 text-[11px] text-team-blue">
            <CheckCircle2 className="size-3" aria-hidden />
            {t("stage6.stage5.ready")}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400">
            <AlertTriangle className="size-3" aria-hidden />
            {t("stage6.stage5.notReady")}
          </span>
        )}
      </div>
      <p className="text-[11px] text-muted-foreground">{t("stage6.stage5.readOnly")}</p>
      {!ready && verdict.reason ? (
        <p className="text-[11px] text-muted-foreground">{verdict.reason}</p>
      ) : null}
    </div>
  )
}

function StatusIcon({ status }: { status: AbilityResultStatus }) {
  switch (status) {
    case "sucesso":
      return <CheckCircle2 className="size-4 text-team-blue" aria-hidden />
    case "falha":
      return <XCircle className="size-4 text-team-red" aria-hidden />
    case "em_execucao":
      return <Loader2 className="size-4 animate-spin text-team-purple" aria-hidden />
    default:
      return <CircleDashed className="size-4 text-muted-foreground" aria-hidden />
  }
}
