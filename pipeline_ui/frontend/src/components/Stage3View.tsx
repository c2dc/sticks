import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react"

import { Button } from "@/components/ui/button"
import { EmptyNote, StageSection } from "@/components/StageSection"
import { cn } from "@/lib/utils"
import {
  connectOperacao,
  getEstagio3,
  type AbilityResult,
  type AbilityResultStatus,
  type OperationResultView,
  type Stage3Result,
} from "@/lib/estagios"

/**
 * Stage3View — Adversary Emulation (Task 12.2, Req. 4).
 *
 * - Entrada: the Adversary + curated Abilities of the case (Req. 4.1).
 * - Processamento: per-Ability status in real time. When an Operation exists,
 *   it subscribes to `WS /ws/operacao/{operacao}` and reflects per-Ability
 *   status frames as they arrive (Req. 4.3).
 * - Saída: per-Ability result (status + command output) and the aggregate
 *   success/failure totals when the Operation reaches its final state
 *   (Req. 4.4, 4.5).
 *
 * The actual emulation trigger + confirm modal is Task 12.4; this view only
 * exposes an `onRequestEmulation` seam that 12.4 wires. It does NOT open a
 * confirm modal or start an Operation itself.
 */
export function Stage3View({
  casoId,
  onRequestEmulation,
}: {
  casoId: string
  /**
   * Seam for Task 12.4: invoked when the researcher asks to emulate the case.
   * 12.4 wires the preview + explicit confirmation modal here. When omitted,
   * the emulation button is hidden (this view never starts an Operation).
   */
  onRequestEmulation?: (casoId: string) => void
}) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<Stage3Result | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Live per-Ability status + aggregate, seeded from the REST snapshot and then
  // updated in real time from the operation channel (Req. 4.3, 4.4, 4.5).
  const [liveResults, setLiveResults] = useState<Record<string, AbilityResult>>({})
  const [liveAgregado, setLiveAgregado] = useState<{
    total_sucesso: number
    total_falha: number
  } | null>(null)

  useEffect(() => {
    let active = true
    setResult(null)
    setLoadError(null)
    setLiveResults({})
    setLiveAgregado(null)
    getEstagio3(casoId)
      .then((data) => {
        if (!active) return
        setResult(data)
        const seed: Record<string, AbilityResult> = {}
        for (const op of data.saida) {
          for (const r of op.resultados) seed[r.ability_id] = r
        }
        setLiveResults(seed)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [casoId, t])

  // The current (latest) Operation, if any. We subscribe to its channel while
  // it exists so status updates stream in (Req. 4.3).
  const currentOperation: OperationResultView | undefined = useMemo(
    () => result?.saida[result.saida.length - 1],
    [result],
  )
  const operacaoId = currentOperation?.operacao_id

  useEffect(() => {
    if (!operacaoId) return
    const connection = connectOperacao(operacaoId, (frame) => {
      if (frame.tipo === "ability") {
        setLiveResults((prev) => ({
          ...prev,
          [frame.ability_id]: {
            ability_id: frame.ability_id,
            status: frame.status,
            saida_comando: frame.saida_comando,
          },
        }))
      } else {
        setLiveAgregado({
          total_sucesso: frame.total_sucesso,
          total_falha: frame.total_falha,
        })
      }
    })
    return () => connection.close()
  }, [operacaoId])

  if (loadError) return <EmptyNote>{loadError}</EmptyNote>
  if (!result) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  const { entrada } = result
  const abilityResults = Object.values(liveResults)
  const agregado = liveAgregado ?? {
    total_sucesso: currentOperation?.total_sucesso ?? 0,
    total_falha: currentOperation?.total_falha ?? 0,
  }
  const hasOperation = Boolean(operacaoId)

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Entrada — Adversary + Abilities to emulate (Req. 4.1). */}
      <StageSection data-section="entrada" label={t("sections.entrada")} kind="red">
        {entrada.adversary ? (
          <div className="flex flex-col gap-0.5">
            <span className="text-xs font-medium text-muted-foreground">
              {t("stage3.entrada.adversary")}
            </span>
            <span className="text-sm font-medium">{entrada.adversary.name}</span>
          </div>
        ) : (
          <EmptyNote>{t("stage3.entrada.noAdversary")}</EmptyNote>
        )}
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium text-muted-foreground">
            {t("stage3.entrada.abilities", { count: entrada.abilities.length })}
          </span>
          <ul className="flex flex-wrap gap-1.5">
            {entrada.abilities.map((ability) => (
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
            disabled={!entrada.can_emulate}
            aria-disabled={!entrada.can_emulate}
            onClick={() => onRequestEmulation(casoId)}
          >
            {t("stage3.entrada.requestEmulation")}
          </Button>
        ) : null}
        {!entrada.can_emulate && entrada.note ? (
          <p className="text-xs text-muted-foreground">{entrada.note}</p>
        ) : null}
      </StageSection>

      {/* Processamento — per-Ability status in real time (Req. 4.3). */}
      <StageSection
        data-section="processamento"
        label={t("sections.processamento")}
        kind="purple"
      >
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

      {/* Saída — per-Ability result + aggregate totals (Req. 4.4, 4.5). */}
      <StageSection data-section="saida" label={t("sections.saida")} kind="red">
        {!hasOperation ? (
          <EmptyNote>{t("stage3.saida.noResults")}</EmptyNote>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-3 text-sm">
              <span className="inline-flex items-center gap-1 text-team-blue">
                <CheckCircle2 className="size-4" aria-hidden />
                {t("stage3.saida.success", { count: agregado.total_sucesso })}
              </span>
              <span className="inline-flex items-center gap-1 text-team-red">
                <XCircle className="size-4" aria-hidden />
                {t("stage3.saida.failure", { count: agregado.total_falha })}
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
                  {r.saida_comando ? (
                    <code className="mt-1 block max-h-24 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-1.5 py-1 text-xs">
                      {r.saida_comando}
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
