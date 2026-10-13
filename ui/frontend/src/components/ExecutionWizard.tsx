import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { ArrowLeft, ArrowRight, Loader2, Play, RotateCcw } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { DagStageView } from "@/components/DagStageView"
import { EnvironmentStageView } from "@/components/EnvironmentStageView"
import { Stage1View } from "@/components/Stage1View"
import { Stage2View } from "@/components/Stage2View"
import { Stage3View } from "@/components/Stage3View"
import { StageStateBadge } from "@/components/StageStateBadge"
import { cn } from "@/lib/utils"
import type { CaseStageStates, NumeroEstagio } from "@/lib/casos"
import {
  concluirTraducao,
  derivarApresentacao,
  derivarEstados,
  estagiosConcluidos,
  executarModelagem,
  getCaso,
  resetCaso,
} from "@/lib/wizard"

/**
 * ExecutionWizard — fluxo guiado "Modelar → Gerar DAG → Operacionalizar →
 * Provisionar → Executar".
 *
 * Conduz o Pesquisador pelos **5 Estágios** de uma campanha (esquema
 * pipeline-cinco-estagios, 1..5):
 * - Estágio 1 — Modelagem Estrutural: corpo `Stage1View`; o botão "Executar
 *   modelagem" dispara POST /estagio/1/executar (`executarModelagem`). É o
 *   ponto de partida do pipeline e nunca fica bloqueado.
 * - Estágio 2 — Geração do DAG: corpo `DagStageView`. Para casos curados o
 *   Estágio 2 já vem `concluido` (auto-concluído pelo backend). O botão único
 *   "Gerar DAG" vive no `DagStageView` e fica desabilitado quando não há DAG; a
 *   conclusão/escolha do DAG chega pela costura `onDagEscolhido` → relê o
 *   backend.
 * - Estágio 3 — Operacionalização: corpo `Stage2View`; "Avançar" conclui a
 *   Operacionalização (POST /estagio/3/concluir, `concluirTraducao`) e segue
 *   para o Estágio 4.
 * - Estágio 4 — Provisionamento do Ambiente: corpo `EnvironmentStageView`. Para
 *   casos curados é auto-concluído (pronto); quando não isolado/não verificável,
 *   o avanço é bloqueado pelo gate sequencial do backend.
 * - Estágio 5 — Execução da Campanha: corpo `Stage3View` + EmulationConfirmModal
 *   (aberto pelo App via `onRequestEmulation`). É o último Estágio — sem
 *   "Avançar".
 *
 * NÃO mantém estado de progresso próprio: relê o backend (fonte da verdade)
 * após cada transição e deriva badges/controles com `derivarEstados` (bloqueio
 * sequencial em 1..5).
 */
export function ExecutionWizard({
  casoId,
  versao,
  onRequestEmulation,
  onVoltarAntesDoEstagio1,
}: {
  casoId: string
  /**
   * Versão da base STIX selecionada no fluxo (Req. 3.1, 3.2). Propagada ao
   * Estágio 1 para recortar o objeto `campaign` da base correta na Entrada e
   * enriquecer o resultado da Modelagem. Opcional/retrocompatível: quando
   * ausente, o backend aplica o fallback (`versao_ausente`) sem erro.
   */
  versao?: string
  onRequestEmulation?: (casoId: string) => void
  /**
   * Seam opcional: no Estágio 1, acionar "Voltar" dispara este callback para o
   * fluxo externo retornar à fase anterior (ex.: seleção de casos — Req 7.4).
   * Quando ausente, o botão permanece desabilitado no Estágio 1 (comportamento
   * atual). Não altera a navegação interna 1↔2↔3↔4↔5 (estágios > 1 seguem
   * usando handleBack).
   */
  onVoltarAntesDoEstagio1?: () => void
}) {
  const { t } = useTranslation("pipeline")

  const [estagioAtual, setEstagioAtual] = useState<NumeroEstagio>(1)
  // Marca de avanço do Pesquisador (monotônica): maior estágio já alcançado
  // nesta sessão. Governa a *apresentação* (um estágio concluído no backend só
  // aparece como "concluído" depois de ter sido alcançado); a habilitação
  // continua governada por `derivarEstados`. Reseta ao trocar de caso ou
  // reiniciar. (Consumida pela derivação de apresentação na próxima etapa.)
  const [maxEstagioVisitado, setMaxEstagioVisitado] = useState<NumeroEstagio>(1)
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
    setMaxEstagioVisitado(1)
    setErro(null)
    void relerEstado().catch(() => setErro(t("stageView.loadError")))
  }, [casoId, relerEstado, t])

  const derived = derivarEstados(concluidos)
  // Camada de apresentação (wizard-progresso-estagios): deriva o
  // Estado_Apresentado de cada badge a partir do Estado_Real (`concluidos`), do
  // avanço do Pesquisador (`maxEstagioVisitado`) e da janela aberta
  // (`estagioAtual`). NÃO governa o gate/avanço — isso continua em `derived`
  // (`derivarEstados`). Só afeta o que a badge mostra e a nota de pré-concluído.
  const apresentacao = derivarApresentacao(
    concluidos,
    maxEstagioVisitado,
    estagioAtual,
  )
  const estagioAtualPreConcluido =
    apresentacao.estagios.find((e) => e.estagio === estagioAtual)?.preConcluido ??
    false
  const statusAtual = derived.estagios.find((e) => e.estagio === estagioAtual)?.status
  const estagio1Concluido = concluidos.includes(1)
  // Special case for Estágio 3 (Operacionalização): the curated material means
  // "avançar = concluir". Clicking "Próximo" here is what POSTs
  // /estagio/3/concluir, so the button must be enabled while Estágio 3 is still
  // AVAILABLE (its predecessor, Estágio 2, is concluded) but not yet concluded
  // — otherwise the gate would require the stage to be concluded to allow the
  // very action that concludes it (a dead end). For every other stage the plain
  // sequential gate applies (advance only once the current stage is concluded).
  const podeConcluirEstagio3Agora =
    estagioAtual === 3 &&
    !concluidos.includes(3) &&
    statusAtual === "disponivel"
  const podeAvancar =
    estagioAtual < 5 &&
    (derived.podeAvancar(estagioAtual) || podeConcluirEstagio3Agora)

  // ---- ações -------------------------------------------------------------

  /**
   * Estágio 2 — a conclusão vem da escolha/geração do DAG. O `DagStageView`
   * chama este callback com os estados devolvidos pelo backend (Estágio 2 já
   * `concluido` e Estágio 3 liberado); atualizamos `concluidos` a partir deles
   * e, por garantia, relemos o caso como fonte da verdade.
   */
  const handleDagEscolhido = (estados: CaseStageStates) => {
    const concluidosDoBackend = estados.estagios
      .filter((e) => e.estado === "concluido")
      .map((e) => e.estagio)
    setConcluidos(concluidosDoBackend)
    void relerEstado().catch(() => setErro(t("stageView.loadError")))
  }

  /** Estágio 1 — dispara a Modelagem Estrutural (POST /estagio/1/executar). */
  const handleRunModelagem = async () => {
    setBusy(true)
    setErro(null)
    try {
      await executarModelagem(casoId, versao)
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
    // Do Estágio 3, avançar significa concluir a Operacionalização.
    if (estagioAtual === 3 && !concluidos.includes(3)) {
      setBusy(true)
      try {
        await concluirTraducao(casoId)
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
    setEstagioAtual((n) => {
      const novoAtual = n < 5 ? ((n + 1) as NumeroEstagio) : n
      setMaxEstagioVisitado(
        (m) => Math.max(m, novoAtual) as NumeroEstagio,
      )
      return novoAtual
    })
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
      setMaxEstagioVisitado(1)
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
            {t("wizard.stepIndicator", { atual: estagioAtual, total: 5 })}
          </span>
        </div>

        {/* Trilha de badges dos 5 estágios. O `data-status` continua expondo o
            status do gate (`derivarEstados`) — fonte da verdade do avanço e
            atributo observado pelos testes —, enquanto o visual da badge segue
            o Estado_Apresentado (`derivarApresentacao`): revelação gradual +
            marca de pré-concluído (curado). */}
        <ol className="flex flex-wrap items-center gap-2">
          {derived.estagios.map((e) => {
            const ap = apresentacao.estagios.find(
              (x) => x.estagio === e.estagio,
            )
            const apresentado = ap?.apresentado ?? e.status
            // Mapeia o Estado_Apresentado para as props da badge:
            //  "concluido"  → estado="concluido" (+ preConcluido quando curado)
            //  "disponivel" → estado="nao_iniciado", bloqueado={false}
            //  "bloqueado"  → estado="nao_iniciado", bloqueado={true}
            const estadoBadge: "concluido" | "nao_iniciado" =
              apresentado === "concluido" ? "concluido" : "nao_iniciado"
            const bloqueado = apresentado === "bloqueado"
            return (
              <li
                key={e.estagio}
                className={cn(
                  "flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs",
                  e.estagio === estagioAtual && "border-team-blue ring-1 ring-team-blue",
                )}
                data-stage={e.estagio}
                data-status={e.status}
                data-apresentado={apresentado}
                aria-current={e.estagio === estagioAtual ? "step" : undefined}
              >
                <span className="font-medium">{t(`stages.${e.estagio}.short`)}</span>
                <StageStateBadge
                  estado={estadoBadge}
                  bloqueado={bloqueado}
                  preConcluido={ap?.preConcluido ?? false}
                />
              </li>
            )
          })}
        </ol>
      </CardHeader>

      <CardContent className="flex flex-col gap-4">
        {/* Nota explicativa de estágio pré-concluído (curado): quando a janela
            aberta corresponde a um estágio concluído pelo sistema (R2.2). */}
        {estagioAtualPreConcluido ? (
          <p
            className="rounded-md border border-team-blue/40 bg-team-blue/10 px-3 py-2 text-xs text-team-blue"
            data-testid="pre-concluido-nota"
          >
            {t("wizard.preConcluidoNota")}
          </p>
        ) : null}

        {/* Corpo do estágio atual — reaproveita as telas existentes.
            Estágio 1 = Modelagem (Stage1View), 2 = Geração do DAG (DagStageView),
            3 = Operacionalização (Stage2View), 4 = Provisionamento
            (EnvironmentStageView), 5 = Execução (Stage3View). */}
        <div key={`${casoId}-${estagioAtual}-${refreshKey}`}>
          {estagioAtual === 1 ? <Stage1View casoId={casoId} versao={versao} /> : null}
          {estagioAtual === 2 ? (
            <DagStageView casoId={casoId} onDagEscolhido={handleDagEscolhido} />
          ) : null}
          {estagioAtual === 3 ? <Stage2View casoId={casoId} /> : null}
          {estagioAtual === 4 ? <EnvironmentStageView casoId={casoId} /> : null}
          {estagioAtual === 5 ? (
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
              onClick={
                estagioAtual === 1 && onVoltarAntesDoEstagio1
                  ? onVoltarAntesDoEstagio1
                  : handleBack
              }
              disabled={
                busy || (estagioAtual === 1 && !onVoltarAntesDoEstagio1)
              }
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
            {/* Estágio 1 (Modelagem) não concluído: executar modelagem. */}
            {estagioAtual === 1 && !estagio1Concluido ? (
              <Button size="sm" onClick={handleRunModelagem} disabled={busy}>
                {busy ? (
                  <Loader2 className="mr-1 size-4 animate-spin" aria-hidden />
                ) : (
                  <Play className="mr-1 size-4" aria-hidden />
                )}
                {busy ? t("wizard.running") : t("wizard.runStage1")}
              </Button>
            ) : null}

            {/* Estágio 5 (Execução): executar emulação (Finish). */}
            {estagioAtual === 5 && onRequestEmulation ? (
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

            {/* Avançar (estágios 1..4). */}
            {estagioAtual < 5 ? (
              <Button
                size="sm"
                onClick={handleNext}
                disabled={!podeAvancar || busy}
                aria-disabled={!podeAvancar || busy}
                aria-label={t("wizard.nextAria")}
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
