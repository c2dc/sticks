import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { ArrowLeft, ArrowRight, Loader2, Play, RotateCcw } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { BaseStixView } from "@/components/BaseStixView"
import { CampanhaView } from "@/components/CampanhaView"
import { DagStageView } from "@/components/DagStageView"
import { EnvironmentStageView } from "@/components/EnvironmentStageView"
import { ParametrosAmbienteView } from "@/components/ParametrosAmbienteView"
import { RelatorioView } from "@/components/RelatorioView"
import { Stage2View } from "@/components/Stage2View"
import { Stage3View } from "@/components/Stage3View"
import { StageStateBadge } from "@/components/StageStateBadge"
import { cn } from "@/lib/utils"
import { concluirOperacionalizacao } from "@/lib/estagios"
import { fetchCases, type CaseStageStates, type NumeroEstagio } from "@/lib/casos"
import { PIPELINE_STAGES } from "@/lib/casos"
import {
  derivarApresentacao,
  derivarEstados,
  estagiosConcluidos,
  getCaso,
  resetCaso,
} from "@/lib/wizard"

/** Last stage of the six-stage pipeline (derived from the single source). */
const LAST_STAGE = PIPELINE_STAGES[PIPELINE_STAGES.length - 1]

/**
 * ExecutionWizard — guided flow across the **6 stages** of the pipeline, one
 * screen per stage, with the stage trail always visible at the top:
 *
 * - Stage 1 — STIX base selection ({@link BaseStixView}): lists/downloads the
 *   base and shows the bundle dashboard.
 * - Stage 2 — Campaign selection ({@link CampanhaView}): picks a campaign from
 *   the bundle and extracts its attributes. The chosen campaign's curated slug
 *   becomes the `campaignId` that drives Stages 3..6.
 * - Stage 3 — DAG generation ({@link DagStageView}).
 * - Stage 4 — Operationalization ({@link Stage2View} + {@link ParametrosAmbienteView}).
 * - Stage 5 — Simulation environment ({@link EnvironmentStageView}).
 * - Stage 6 — Execution, measurement and report ({@link Stage3View} +
 *   {@link RelatorioView}). Last stage — no "Next".
 *
 * Entry point: the wizard is reached straight from the Landing, with NO campaign
 * chosen yet. Stages 1 and 2 (base/campaign listings) depend only on the base
 * version, not on a specific campaign, so they run against a `probeId` — the
 * first curated slug fetched from the backend — purely to list options. Once a
 * campaign is selected in Stage 2, `campaignId` is set and governs Stages 3..6.
 *
 * The wizard keeps NO progress state of its own: it re-reads the backend (the
 * source of truth) after each transition and derives badges/controls with
 * `derivarEstados` (sequential gating over 1..6).
 */
export function ExecutionWizard({
  onRequestEmulation,
  onExitToStart,
}: {
  /** Opens the emulation confirm modal for the selected campaign id. */
  onRequestEmulation?: (campaignId: string) => void
  /**
   * "Back" on Stage 1 leaves the wizard and returns to the Landing. When
   * absent, the Back button stays disabled on Stage 1. Does not affect the
   * internal 1↔…↔6 navigation.
   */
  onExitToStart?: () => void
}) {
  const { t } = useTranslation("pipeline")

  const [estagioAtual, setEstagioAtual] = useState<NumeroEstagio>(1)
  // Monotonic advance mark: the furthest stage reached this session. Governs
  // presentation (a stage completed on the backend only shows as "completed"
  // once it has been reached); enablement stays governed by `derivarEstados`.
  const [maxEstagioVisitado, setMaxEstagioVisitado] = useState<NumeroEstagio>(1)
  const [concluidos, setConcluidos] = useState<number[]>([])
  // STIX base version chosen in Stage 1; consumed by Stages 1 and 2.
  const [versaoSelecionada, setVersaoSelecionada] = useState<string | undefined>(
    undefined,
  )
  // Probe slug used only to list base/campaign options in Stages 1-2 before a
  // campaign is chosen. Any curated slug works since those listings depend on
  // the base version, not on a specific campaign.
  const [probeId, setProbeId] = useState<string | undefined>(undefined)
  // The chosen campaign's curated slug — set when the campaign is selected in
  // Stage 2. Drives Stages 3..6. Until then it is undefined.
  const [campaignId, setCampaignId] = useState<string | undefined>(undefined)
  // Stage 1 completion is a client-side verdict (bundle on disk + dashboard
  // shown), reported by BaseStixView via onConclusaoChange. Stage 1 is pure
  // selection/visualization with no backend StageRun to persist, so the
  // wizard merges this verdict into the completed set that governs the gate.
  const [estagio1Ok, setEstagio1Ok] = useState(false)
  // Stage 2 completion mirrors Stage 1: selecting a campaign extracts its
  // attributes and yields a client-side "concluido" verdict
  // (ResultadoSelecaoCampanha.concluido) that the backend does not persist as
  // a StageRun. CampanhaView only calls onConcluido on a successful
  // extraction, so this flag tracks that success and feeds the gate.
  const [estagio2Ok, setEstagio2Ok] = useState(false)
  const [busy, setBusy] = useState(false)
  const [erro, setErro] = useState<string | null>(null)
  // Key to force the Stage*View remount and data reload after a transition.
  const [refreshKey, setRefreshKey] = useState(0)

  // The id that backend-addressed stages operate on: the chosen campaign once
  // selected, otherwise the probe slug for the pre-selection stages (1-2).
  const activeId = campaignId ?? probeId

  const relerEstado = useCallback(async () => {
    if (!activeId) return
    const caso = await getCaso(activeId)
    setConcluidos(estagiosConcluidos(caso.estados))
    setRefreshKey((k) => k + 1)
  }, [activeId])

  // Stage 5 reports provisioning completion WHILE the stage is open. Re-read
  // the backend state to unlock "Próximo" (Req. 6.4, 7.3) WITHOUT bumping
  // `refreshKey` — remounting the Stage 5 body would re-trigger provisioning
  // (idempotent, but it would loop with the completion callback). Stable
  // identity so it never re-runs the EnvironmentStageView mount effect.
  const handleProvisionamentoConcluido = useCallback(() => {
    if (!activeId) return
    getCaso(activeId)
      .then((caso) => setConcluidos(estagiosConcluidos(caso.estados)))
      .catch(() => {
        // Best-effort: the next transition re-reads the backend anyway.
      })
  }, [activeId])

  // Resolve the probe slug once on mount (first curated case). Failing to fetch
  // it leaves Stage 1 able to list bases (that path does not need the probe for
  // the base list itself), but the stage verdict read is skipped until an id
  // exists.
  useEffect(() => {
    let active = true
    fetchCases()
      .then((resp) => {
        if (!active) return
        const first = resp.casos[0]?.id
        if (first) setProbeId(first)
      })
      .catch(() => {
        // Best-effort: the base list still renders; selecting a campaign later
        // provides a real id for the backend-addressed reads.
      })
    return () => {
      active = false
    }
  }, [])

  // Load initial state once an addressable id is known.
  useEffect(() => {
    setErro(null)
    void relerEstado().catch(() => setErro(t("stageView.loadError")))
  }, [relerEstado, t])

  // Effective completed set: the backend-reported stages plus Stage 1 when its
  // client-side verdict holds. This is the single source for both the gate
  // (`derivarEstados`) and the presentation (`derivarApresentacao`).
  // Stages 1 and 2 are governed by client-side verdicts (estagio1Ok /
  // estagio2Ok). The backend may auto-conclude them for curated cases, which
  // would show "completed" badges before the researcher actually selected a
  // base or campaign. So: stage 1 is only counted when estagio1Ok, stage 2
  // only when estagio2Ok — regardless of what the backend reports for them.
  const concluidosEfetivos = (() => {
    const set = new Set(concluidos)
    // Remove backend auto-concluded 1/2 unless the client-side verdict agrees.
    if (!estagio1Ok) set.delete(1)
    if (!estagio2Ok) set.delete(2)
    if (estagio1Ok) set.add(1)
    if (estagio2Ok) set.add(2)
    return [...set]
  })()
  const derived = derivarEstados(concluidosEfetivos)
  // Presentation layer (gradual reveal + pre-completed mark). Does NOT govern
  // the gate/advance — that stays in `derived` (`derivarEstados`).
  const apresentacao = derivarApresentacao(
    concluidosEfetivos,
    maxEstagioVisitado,
    estagioAtual,
  )
  const estagioAtualPreConcluido =
    apresentacao.estagios.find((e) => e.estagio === estagioAtual)?.preConcluido ??
    false
  const statusAtual = derived.estagios.find((e) => e.estagio === estagioAtual)?.status
  // Stage 4 (Operationalization) special case: the material is curated, so
  // "advance = complete". Clicking "Next" here is what POSTs
  // /estagio/4/concluir, so the button must be enabled while Stage 4 is
  // AVAILABLE (its predecessor, Stage 3, is complete) but not yet complete —
  // otherwise the gate would require the stage to be complete to allow the very
  // action that completes it. Other stages use the plain sequential gate.
  const podeConcluirEstagio4Agora =
    estagioAtual === 4 &&
    !concluidosEfetivos.includes(4) &&
    statusAtual === "disponivel"
  const podeAvancar =
    estagioAtual < LAST_STAGE &&
    (derived.podeAvancar(estagioAtual) || podeConcluirEstagio4Agora)

  // ---- actions -----------------------------------------------------------

  /**
   * Stage 3 (DAG generation) — completion comes from choosing the DAG. The
   * DagStageView calls this with the backend-returned states (Stage 3 already
   * `concluido`); we update `concluidos` from them and re-read as the source of
   * truth.
   */
  const handleDagEscolhido = (estados: CaseStageStates) => {
    // Reconcile the gate straight from the backend-returned states. We do NOT
    // call relerEstado() here: it bumps refreshKey and would remount the
    // DagStageView mid-render, collapsing the just-drawn Mermaid diagram. The
    // states already carry Stage 3 concluded + Stage 4 unlocked.
    const concluidosDoBackend = estados.estagios
      .filter((e) => e.estado === "concluido")
      .map((e) => e.estagio)
    setConcluidos(concluidosDoBackend)
  }

  /**
   * Stage 2 (Campaign selection) — the chosen campaign's curated slug becomes
   * the campaignId that drives Stages 3..6; then re-read the backend.
   */
  const handleCampanhaConcluida = (slugCurado: string) => {
    // Idempotent: CampanhaView may report the same already-concluded
    // selection more than once (e.g. after a remount). Only react when the
    // chosen campaign actually changes, so we never loop by re-setting state
    // that triggers another report. No refreshKey bump here — the stage
    // transition (handleNext) is what forces a reload.
    if (slugCurado === campaignId && estagio2Ok) return
    setCampaignId(slugCurado)
    // Successful extraction concludes Stage 2 (client-side verdict).
    setEstagio2Ok(true)
    // The effect keyed on `activeId` re-reads the backend; also read now for
    // responsiveness so the gate reflects the chosen campaign immediately.
    getCaso(slugCurado)
      .then((caso) => setConcluidos(estagiosConcluidos(caso.estados)))
      .catch(() => setErro(t("stageView.loadError")))
  }

  /** Stage 1 — the researcher chose/changed the STIX base. */
  const handleVersaoSelecionada = (nova: string) => {
    // Empty string means "change base" (back to the list) in BaseStixView.
    setVersaoSelecionada(nova || undefined)
    // Changing/clearing the base invalidates the Stage 1 verdict until the
    // new base's bundle + dashboard are confirmed again, and also drops any
    // prior campaign selection (Stage 2) made against the old base.
    if (!nova) {
      setEstagio1Ok(false)
      setEstagio2Ok(false)
    }
  }

  const handleNext = async () => {
    setErro(null)
    // From Stage 4, advancing means completing Operationalization.
    if (estagioAtual === 4 && !concluidos.includes(4) && activeId) {
      setBusy(true)
      try {
        await concluirOperacionalizacao(activeId)
        await relerEstado()
      } catch (e) {
        setErro(e instanceof Error ? e.message : t("stageView.loadError"))
        setBusy(false)
        return
      }
      setBusy(false)
    }
    setEstagioAtual((n) => {
      const novoAtual = n < LAST_STAGE ? ((n + 1) as NumeroEstagio) : n
      setMaxEstagioVisitado((m) => Math.max(m, novoAtual) as NumeroEstagio)
      return novoAtual
    })
  }

  const handleBack = () => {
    setErro(null)
    setEstagioAtual((n) => (n > 1 ? ((n - 1) as NumeroEstagio) : n))
  }

  const handleRestart = async () => {
    if (!activeId) return
    setBusy(true)
    setErro(null)
    try {
      await resetCaso(activeId)
      setEstagioAtual(1)
      setMaxEstagioVisitado(1)
      setEstagio2Ok(false)
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
            {t("wizard.stepIndicator", {
              atual: estagioAtual,
              total: LAST_STAGE,
            })}
          </span>
        </div>

        {/* Six-stage badge trail. `data-status` keeps exposing the gate status
            (`derivarEstados`) — the source of truth for advancing and the
            attribute asserted by tests —, while the badge visual follows the
            presented state (`derivarApresentacao`): gradual reveal + the
            pre-completed (curated) mark. */}
        <ol className="flex flex-wrap items-center gap-2">
          {derived.estagios.map((e) => {
            const ap = apresentacao.estagios.find((x) => x.estagio === e.estagio)
            const apresentado = ap?.apresentado ?? e.status
            const estadoBadge: "concluido" | "nao_iniciado" =
              apresentado === "concluido" ? "concluido" : "nao_iniciado"
            const bloqueado = apresentado === "bloqueado"
            return (
              <li
                key={e.estagio}
                className={cn(
                  "flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs",
                  e.estagio === estagioAtual &&
                    "border-team-blue ring-1 ring-team-blue",
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
                  compact
                />
              </li>
            )
          })}
        </ol>
      </CardHeader>

      <CardContent className="flex flex-col gap-4">
        {/* Pre-completed (curated) stage note: when the open stage corresponds
            to a stage the system completed. */}
        {estagioAtualPreConcluido ? (
          <p
            className="rounded-md border border-team-blue/40 bg-team-blue/10 px-3 py-2 text-xs text-team-blue"
            data-testid="pre-concluido-nota"
          >
            {t("wizard.preConcluidoNota")}
          </p>
        ) : null}

        {/* Current stage body. 1 = STIX base selection, 2 = campaign selection,
            3 = DAG generation, 4 = operationalization, 5 = environment,
            6 = execution/measurement/report. Stages 1-2 use the probe id to
            list options; 3..6 require the chosen campaign id. */}
        <div key={`${activeId ?? "nocampaign"}-${estagioAtual}-${refreshKey}`}>
          {estagioAtual === 1 ? (
            <BaseStixView
              casoId={activeId ?? ""}
              versao={versaoSelecionada}
              onVersaoSelecionada={handleVersaoSelecionada}
              onConclusaoChange={setEstagio1Ok}
            />
          ) : null}
          {estagioAtual === 2 ? (
            // Index Stage 2 by the chosen campaign once one is picked,
            // falling back to the probe only while none is chosen. Keying on
            // activeId makes the read-only selection the backend returns match
            // the campaign the researcher actually selected (no reverting to
            // the probe's campaign).
            <CampanhaView
              casoId={activeId ?? ""}
              versao={versaoSelecionada}
              onConcluido={handleCampanhaConcluida}
            />
          ) : null}
          {estagioAtual === 3 && campaignId ? (
            <DagStageView casoId={campaignId} onDagEscolhido={handleDagEscolhido} />
          ) : null}
          {estagioAtual === 4 && campaignId ? (
            <div className="grid gap-4 lg:grid-cols-2">
              <Stage2View casoId={campaignId} />
              <ParametrosAmbienteView casoId={campaignId} />
            </div>
          ) : null}
          {estagioAtual === 5 && campaignId ? (
            <EnvironmentStageView
              casoId={campaignId}
              // Provisioning happens WHILE on Stage 5; when it concludes we
              // re-read the backend state so Stage 5 shows `concluido` and the
              // "Próximo" gate unlocks (Req. 6.4, 7.3). `relerEstado` is a
              // stable useCallback so it won't re-trigger provisioning.
              onProvisionamentoConcluido={handleProvisionamentoConcluido}
            />
          ) : null}
          {estagioAtual === 6 && campaignId ? (
            <div className="flex flex-col gap-4">
              <Stage3View casoId={campaignId} onRequestEmulation={onRequestEmulation} />
              <RelatorioView casoId={campaignId} />
            </div>
          ) : null}
          {estagioAtual >= 3 && !campaignId ? (
            <p className="text-sm text-muted-foreground" role="status">
              {t("wizard.selectCampaignFirst")}
            </p>
          ) : null}
        </div>

        {erro ? (
          <p className="text-sm text-team-red" role="alert">
            {erro}
          </p>
        ) : null}

        {/* Wizard navigation controls. */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={
                estagioAtual === 1 && onExitToStart ? onExitToStart : handleBack
              }
              disabled={busy || (estagioAtual === 1 && !onExitToStart)}
            >
              <ArrowLeft className="mr-1 size-4" aria-hidden />
              {t("wizard.back")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleRestart}
              disabled={busy || !activeId}
              title={t("wizard.restart")}
            >
              <RotateCcw className="mr-1 size-4" aria-hidden />
              {t("wizard.restart")}
            </Button>
          </div>

          <div className="flex items-center gap-2">
            {/* Stage 6 (Execution): run emulation (Finish). */}
            {estagioAtual === LAST_STAGE && onRequestEmulation && campaignId ? (
              <Button
                variant="destructive"
                size="sm"
                onClick={() => onRequestEmulation(campaignId)}
                disabled={busy}
              >
                <Play className="mr-1 size-4" aria-hidden />
                {t("wizard.runEmulation")}
              </Button>
            ) : null}

            {/* Next (stages 1..5). */}
            {estagioAtual < LAST_STAGE ? (
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
