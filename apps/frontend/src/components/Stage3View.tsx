import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react"

import { Button } from "@/components/ui/button"
import { EmptyNote, StageSection } from "@/components/StageSection"
import { MermaidDiagram } from "@/components/MermaidDiagram"
import { cn } from "@/lib/utils"
import {
  connectOperation,
  getStage3,
  getTopology,
  type AbilityResult,
  type AbilityResultStatus,
  type OperationResultView,
  type Stage3Result,
  type TopologyResponse,
} from "@/lib/stages"

/**
 * Stage3View — Adversary Emulation (Task 12.2, Req. 4).
 *
 * - Input: the Adversary + curated Abilities of the case (Req. 4.1).
 * - Processing: per-Ability status in real time. When an Operation exists,
 *   it subscribes to `WS /ws/operation/{operation}` and reflects per-Ability
 *   status frames as they arrive (Req. 4.3).
 * - Output: per-Ability result (status + command output) and the aggregate
 *   success/failure totals when the Operation reaches its final state
 *   (Req. 4.4, 4.5).
 *
 * The actual emulation trigger + confirm modal is Task 12.4; this view only
 * exposes an `onRequestEmulation` seam that 12.4 wires. It does NOT open a
 * confirm modal or start an Operation itself.
 */
export function Stage3View({
  campaignId,
  onRequestEmulation,
}: {
  campaignId: string
  /**
   * Seam for Task 12.4: invoked when the researcher asks to emulate the case.
   * 12.4 wires the preview + explicit confirmation modal here. When omitted,
   * the emulation button is hidden (this view never starts an Operation).
   */
  onRequestEmulation?: (campaignId: string) => void
}) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<Stage3Result | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

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
    getStage3(campaignId)
      .then((data) => {
        if (!active) return
        setResult(data)
        const seed: Record<string, AbilityResult> = {}
        for (const op of data.output) {
          for (const r of op.results) seed[r.ability_id] = r
        }
        setLiveResults(seed)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [campaignId, t])

  // The current (latest) Operation, if any. We subscribe to its channel while
  // it exists so status updates stream in (Req. 4.3).
  const currentOperation: OperationResultView | undefined = useMemo(
    () => result?.output[result.output.length - 1],
    [result],
  )
  const operationId = currentOperation?.operation_id

  useEffect(() => {
    if (!operationId) return
    const connection = connectOperation(operationId, (frame) => {
      if (frame.type === "ability") {
        setLiveResults((prev) => ({
          ...prev,
          [frame.ability_id]: {
            ability_id: frame.ability_id,
            status: frame.status,
            command_output: frame.command_output,
          },
        }))
      } else {
        setLiveAggregate({
          total_success: frame.total_success,
          total_failure: frame.total_failure,
        })
      }
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
  const abilityResults = Object.values(liveResults)
  const aggregate = liveAggregate ?? {
    total_success: currentOperation?.total_success ?? 0,
    total_failure: currentOperation?.total_failure ?? 0,
  }
  const hasOperation = Boolean(operationId)

  return (
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
        {onRequestEmulation ? (
          <Button
            variant="destructive"
            size="sm"
            className="mt-auto"
            disabled={!input.can_emulate}
            aria-disabled={!input.can_emulate}
            onClick={() => onRequestEmulation(campaignId)}
          >
            {t("stage3.entrada.requestEmulation")}
          </Button>
        ) : null}
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

        {!hasOperation ? (
          <EmptyNote>{t("stage3.processamento.noOperation")}</EmptyNote>
        ) : abilityResults.length === 0 ? (
          <p className="text-xs text-muted-foreground">{t("stage3.processamento.pending")}</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {abilityResults.map((r) => (
              <li key={r.ability_id} className="flex items-center gap-2 text-sm">
                <StatusIcon status={r.status} />
                <span className="min-w-0 flex-1 truncate">{r.ability_id}</span>
                <span className="text-xs text-muted-foreground">
                  {t(`stage3.status.${r.status}`)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </StageSection>

      {/* Output — per-Ability result + aggregate totals (Req. 4.4, 4.5). */}
      <StageSection data-section="saida" label={t("sections.saida")} kind="red">
        {!hasOperation ? (
          <EmptyNote>{t("stage3.saida.noResults")}</EmptyNote>
        ) : (
          <div className="flex flex-col gap-3">
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
            <ul className="flex flex-col gap-2">
              {abilityResults.map((r) => (
                <li key={r.ability_id} className="rounded-md border p-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="min-w-0 flex-1 truncate text-sm font-medium">
                      {r.ability_id}
                    </span>
                    <span
                      className={cn(
                        "shrink-0 rounded-full px-2 py-0.5 text-xs",
                        r.status === "sucesso" && "bg-team-blue/10 text-team-blue",
                        r.status === "falha" && "bg-team-red/10 text-team-red",
                        (r.status === "pendente" || r.status === "em_execucao") &&
                          "bg-muted text-muted-foreground",
                      )}
                    >
                      {t(`stage3.status.${r.status}`)}
                    </span>
                  </div>
                  {r.command_output ? (
                    <code className="mt-1 block max-h-24 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-1.5 py-1 text-xs">
                      {r.command_output}
                    </code>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        )}
      </StageSection>
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
