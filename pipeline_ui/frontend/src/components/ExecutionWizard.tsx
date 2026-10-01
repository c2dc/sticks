import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { ArrowLeft, ArrowRight, Loader2, Play, RotateCcw } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Stage1View } from "@/components/Stage1View"
import { Stage2View } from "@/components/Stage2View"
import { Stage3View } from "@/components/Stage3View"
import { StageStateBadge } from "@/components/StageStateBadge"
import { cn } from "@/lib/utils"
import type { NumeroEstagio } from "@/lib/casos"
import {
  concluirEstagio2,
  derivarEstados,
  estagiosConcluidos,
  executarEstagio1,
  getCaso,
  resetCaso,
} from "@/lib/wizard"

/**
 * ExecutionWizard — fluxo guiado "Avançar → Avançar → Executar/Finish".
 *
 * Conduz o Pesquisador pelos 3 estágios de uma campanha, reaproveitando
 * Stage1/2/3View para o corpo de cada passo e o EmulationConfirmModal (aberto
 * pelo App via `onRequestEmulation`) para a confirmação do Estágio 3. NÃO mantém
 * estado de progresso próprio: relê o backend (fonte da verdade) após cada
 * transição e deriva badges/controles com `derivarEstados` (bloqueio sequencial).
 */
export function ExecutionWizard({
  casoId,
  onRequestEmulation,
}: {
  casoId: string
  onRequestEmulation?: (casoId: string) => void
}) {
  const { t } = useTranslation("pipeline")

  const [estagioAtual, setEstagioAtual] = useState<NumeroEstagio>(1)
  const [concluidos, setConcluidos] = useState<number[]>([])
  const [busy, setBusy] = useState(false)
  const [erro, setErro] = useState<string | null>(null)
  // Chave para forçar o remount das Stage*View e recarregarem os dados após
  // uma transição (ex.: após executar a modelagem do Estágio 1).
  const [refreshKey, setRefreshKey] = useState(0)

  const relerEstado = useCallback(async () => {
    const caso = await getCaso(casoId)
    setConcluidos(estagiosConcluidos(caso.estados))
    setRefreshKey((k) => k + 1)
  }, [casoId])

  // Carrega o estado inicial e volta ao Estágio 1 quando a campanha muda.
  useEffect(() => {
    setEstagioAtual(1)
    setErro(null)
    void relerEstado().catch(() => setErro(t("stageView.loadError")))
  }, [casoId, relerEstado, t])

  const derived = derivarEstados(concluidos)
  const statusAtual = derived.estagios.find((e) => e.estagio === estagioAtual)?.status
  const estagio1Concluido = concluidos.includes(1)
  const podeAvancar = derived.podeAvancar(estagioAtual) && estagioAtual < 3

  // ---- ações -------------------------------------------------------------

  const handleRunStage1 = async () => {
    setBusy(true)
    setErro(null)
    try {
      await executarEstagio1(casoId)
      // A fonte da verdade é o estado relido do backend: se o Estágio 1 não
      // ficou concluido, a modelagem falhou — sinaliza para o Pesquisador.
      const caso = await getCaso(casoId)
      const e1 = caso.estados?.estagios.find((e) => e.estagio === 1)
      if (e1 && e1.estado === "erro") {
        setErro(e1.erro || t("wizard.stage1ErrorHint"))
      }
      setConcluidos(estagiosConcluidos(caso.estados))
      setRefreshKey((k) => k + 1)
    } catch {
      setErro(t("stageView.loadError"))
    } finally {
      setBusy(false)
    }
  }

  const handleNext = async () => {
    setErro(null)
    // Do Estágio 2, avançar significa concluí-lo (tradução curada).
    if (estagioAtual === 2 && !concluidos.includes(2)) {
      setBusy(true)
      try {
        await concluirEstagio2(casoId)
        await relerEstado()
      } catch (e) {
        setErro(
          e instanceof Error ? e.message : t("stageView.loadError"),
        )
        setBusy(false)
        return
      }
      setBusy(false)
    }
    setEstagioAtual((n) => (n < 3 ? ((n + 1) as NumeroEstagio) : n))
  }

  const handleBack = () => {
    setErro(null)
    setEstagioAtual((n) => (n > 1 ? ((n - 1) as NumeroEstagio) : n))
  }

  const handleRestart = async () => {
    setBusy(true)
    setErro(null)
    try {
      await resetCaso(casoId)
      setEstagioAtual(1)
      await relerEstado()
    } catch {
      setErro(t("stageView.loadError"))
    } finally {
      setBusy(false)
    }
  }

  // ---- render ------------------------------------------------------------

  return (
    <Card data-testid="execution-wizard" className="flex flex-col gap-2">
      <CardHeader className="gap-3">
        <div className="flex items-center justify-between gap-4">
          <CardTitle className="text-lg">
            {t(`stages.${estagioAtual}.name`)}
          </CardTitle>
          <span className="text-sm text-muted-foreground">
            {t("wizard.stepIndicator", { atual: estagioAtual, total: 3 })}
          </span>
        </div>

        {/* Trilha de badges dos 3 estágios (estado derivado do backend). */}
        <ol className="flex flex-wrap items-center gap-2">
          {derived.estagios.map((e) => {
            const estado =
              e.status === "concluido"
                ? "concluido"
                : e.status === "bloqueado"
                  ? "nao_iniciado"
                  : "nao_iniciado"
            return (
              <li
                key={e.estagio}
                className={cn(
                  "flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs",
                  e.estagio === estagioAtual && "border-team-blue ring-1 ring-team-blue",
                )}
                data-stage={e.estagio}
                data-status={e.status}
                aria-current={e.estagio === estagioAtual ? "step" : undefined}
              >
                <span className="font-medium">{t(`stages.${e.estagio}.short`)}</span>
                <StageStateBadge
                  estado={estado as "concluido" | "nao_iniciado"}
                  bloqueado={e.status === "bloqueado"}
                />
              </li>
            )
          })}
        </ol>
      </CardHeader>

      <CardContent className="flex flex-col gap-4">
        {/* Corpo do estágio atual — reaproveita as telas existentes. */}
        <div key={`${casoId}-${estagioAtual}-${refreshKey}`}>
          {estagioAtual === 1 ? <Stage1View casoId={casoId} /> : null}
          {estagioAtual === 2 ? <Stage2View casoId={casoId} /> : null}
          {estagioAtual === 3 ? (
            <Stage3View casoId={casoId} onRequestEmulation={onRequestEmulation} />
          ) : null}
        </div>

        {erro ? (
          <p className="text-sm text-team-red" role="alert">
            {erro}
          </p>
        ) : null}

        {/* Controles de navegação do wizard. */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleBack}
              disabled={estagioAtual === 1 || busy}
            >
              <ArrowLeft className="mr-1 size-4" aria-hidden />
              {t("wizard.back")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleRestart}
              disabled={busy}
              title={t("wizard.restart")}
            >
              <RotateCcw className="mr-1 size-4" aria-hidden />
              {t("wizard.restart")}
            </Button>
          </div>

          <div className="flex items-center gap-2">
            {/* Estágio 1 não concluído: executar modelagem. */}
            {estagioAtual === 1 && !estagio1Concluido ? (
              <Button size="sm" onClick={handleRunStage1} disabled={busy}>
                {busy ? (
                  <Loader2 className="mr-1 size-4 animate-spin" aria-hidden />
                ) : (
                  <Play className="mr-1 size-4" aria-hidden />
                )}
                {busy ? t("wizard.running") : t("wizard.runStage1")}
              </Button>
            ) : null}

            {/* Estágio 3: executar emulação (Finish). */}
            {estagioAtual === 3 && onRequestEmulation ? (
              <Button
                variant="destructive"
                size="sm"
                onClick={() => onRequestEmulation(casoId)}
                disabled={busy}
              >
                <Play className="mr-1 size-4" aria-hidden />
                {t("wizard.runEmulation")}
              </Button>
            ) : null}

            {/* Avançar (estágios 1 e 2). */}
            {estagioAtual < 3 ? (
              <Button
                size="sm"
                onClick={handleNext}
                disabled={!podeAvancar || busy}
                aria-disabled={!podeAvancar || busy}
                title={!podeAvancar ? t("wizard.blockedHint") : undefined}
              >
                {busy ? (
                  <Loader2 className="mr-1 size-4 animate-spin" aria-hidden />
                ) : null}
                {t("wizard.next")}
                <ArrowRight className="ml-1 size-4" aria-hidden />
              </Button>
            ) : null}
          </div>
        </div>

        {statusAtual === "bloqueado" ? (
          <p className="text-xs text-muted-foreground">{t("wizard.blockedHint")}</p>
        ) : null}
      </CardContent>
    </Card>
  )
}
