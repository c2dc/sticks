import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { ArrowLeft, ArrowRight, Loader2, RotateCcw } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { BaseStixView } from "@/components/BaseStixView"
import { CampaignView } from "@/components/CampaignView"
import { DagStageView } from "@/components/DagStageView"
import { EnvironmentStageView } from "@/components/EnvironmentStageView"
import { EnvironmentParametersView } from "@/components/EnvironmentParametersView"
import { ReportView } from "@/components/ReportView"
import { Stage2View } from "@/components/Stage2View"
import { Stage3View } from "@/components/Stage3View"
import { StageStateBadge } from "@/components/StageStateBadge"
import { cn } from "@/lib/utils"
import { completeOperationalization } from "@/lib/stages"
import { fetchCases, type CaseStageStates, type StageNumber } from "@/lib/campaigns"
import { PIPELINE_STAGES } from "@/lib/campaigns"
import {
  derivePresentation,
  deriveStates,
  completedStages,
  getCampaign,
  resetCampaign,
} from "@/lib/wizard"

/** Last stage of the six-stage pipeline (derived from the single source). */
const LAST_STAGE = PIPELINE_STAGES[PIPELINE_STAGES.length - 1]

/**
 * ExecutionWizard — guided flow across the **6 stages** of the pipeline, one
 * screen per stage, with the stage trail always visible at the top:
 *
 * - Stage 1 — STIX base selection ({@link BaseStixView}): lists/downloads the
 *   base and shows the bundle dashboard.
 * - Stage 2 — Campaign selection ({@link CampaignView}): picks a campaign from
 *   the bundle and extracts its attributes. The chosen campaign's curated slug
 *   becomes the `campaignId` that drives Stages 3..6.
 * - Stage 3 — DAG generation ({@link DagStageView}).
 * - Stage 4 — Operationalization ({@link Stage2View} + {@link EnvironmentParametersView}).
 * - Stage 5 — Simulation environment ({@link EnvironmentStageView}).
 * - Stage 6 — Execution, measurement and report ({@link Stage3View} +
 *   {@link ReportView}). Last stage — no "Next".
 *
 * Entry point: the wizard is reached straight from the Landing, with NO campaign
 * chosen yet. Stages 1 and 2 (base/campaign listings) depend only on the base
 * version, not on a specific campaign, so they run against a `probeId` — the
 * first curated slug fetched from the backend — purely to list options. Once a
 * campaign is selected in Stage 2, `campaignId` is set and governs Stages 3..6.
 *
 * The wizard keeps NO progress state of its own: it re-reads the backend (the
 * source of truth) after each transition and derives badges/controls with
 * `deriveStates` (sequential gating over 1..6).
 */
export function ExecutionWizard({
  onExitToStart,
}: {
  /**
   * "Back" on Stage 1 leaves the wizard and returns to the Landing. When
   * absent, the Back button stays disabled on Stage 1. Does not affect the
   * internal 1↔…↔6 navigation.
   */
  onExitToStart?: () => void
}) {
  const { t } = useTranslation("pipeline")

  const [currentStage, setCurrentStage] = useState<StageNumber>(1)
  // Monotonic advance mark: the furthest stage reached this session. Governs
  // presentation (a stage completed on the backend only shows as "completed"
  // once it has been reached); enablement stays governed by `deriveStates`.
  const [maxVisitedStage, setMaxVisitedStage] = useState<StageNumber>(1)
  const [completed, setCompleted] = useState<number[]>([])
  // STIX base version chosen in Stage 1; consumed by Stages 1 and 2.
  const [selectedVersion, setSelectedVersion] = useState<string | undefined>(
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
  // shown), reported by BaseStixView via onCompletionChange. Stage 1 is pure
  // selection/visualization with no backend StageRun to persist, so the
  // wizard merges this verdict into the completed set that governs the gate.
  const [stage1Ok, setStage1Ok] = useState(false)
  // Stage 2 completion mirrors Stage 1: selecting a campaign extracts its
  // attributes and yields a client-side "completed" verdict
  // (CampaignSelectionResult.completed) that the backend does not persist as
  // a StageRun. CampaignView only calls onCompleted on a successful
  // extraction, so this flag tracks that success and feeds the gate.
  const [stage2Ok, setStage2Ok] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Key to force the Stage*View remount and data reload after a transition.
  const [refreshKey, setRefreshKey] = useState(0)

  // The id that backend-addressed stages operate on: the chosen campaign once
  // selected, otherwise the probe slug for the pre-selection stages (1-2).
  const activeId = campaignId ?? probeId

  const rereadState = useCallback(async () => {
    if (!activeId) return
    const campaign = await getCampaign(activeId)
    setCompleted(completedStages(campaign.states))
    setRefreshKey((k) => k + 1)
  }, [activeId])

  // Stage 5 reports provisioning completion WHILE the stage is open. Re-read
  // the backend state to unlock "Next" (Req. 6.4, 7.3) WITHOUT bumping
  // `refreshKey` — remounting the Stage 5 body would re-trigger provisioning
  // (idempotent, but it would loop with the completion callback). Stable
  // identity so it never re-runs the EnvironmentStageView mount effect.
  const handleProvisioningComplete = useCallback(() => {
    if (!activeId) return
    getCampaign(activeId)
      .then((campaign) => setCompleted(completedStages(campaign.states)))
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
        const first = resp.campaigns[0]?.id
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
    setError(null)
    void rereadState().catch(() => setError(t("stageView.loadError")))
  }, [rereadState, t])

  // Effective completed set: the backend-reported stages plus Stage 1 when its
  // client-side verdict holds. This is the single source for both the gate
  // (`deriveStates`) and the presentation (`derivePresentation`).
  // Stages 1 and 2 are governed by client-side verdicts (stage1Ok /
  // stage2Ok). The backend may auto-conclude them for curated cases, which
  // would show "completed" badges before the researcher actually selected a
  // base or campaign. So: stage 1 is only counted when stage1Ok, stage 2
  // only when stage2Ok — regardless of what the backend reports for them.
  const effectiveCompleted = (() => {
    const set = new Set(completed)
    // Remove backend auto-concluded 1/2 unless the client-side verdict agrees.
    if (!stage1Ok) set.delete(1)
    if (!stage2Ok) set.delete(2)
    if (stage1Ok) set.add(1)
    if (stage2Ok) set.add(2)
    return [...set]
  })()
  const derived = deriveStates(effectiveCompleted)
  // Presentation layer (gradual reveal + pre-completed mark). Does NOT govern
  // the gate/advance — that stays in `derived` (`deriveStates`).
  const presentation = derivePresentation(
    effectiveCompleted,
    maxVisitedStage,
    currentStage,
  )
  const currentStagePreCompleted =
    presentation.stages.find((e) => e.stage === currentStage)?.preCompleted ??
    false
  const currentStatus = derived.stages.find((e) => e.stage === currentStage)?.status
  // Stage 4 (Operationalization) special case: the material is curated, so
  // "advance = complete". Clicking "Next" here is what POSTs
  // /stage/4/complete, so the button must be enabled while Stage 4 is
  // AVAILABLE (its predecessor, Stage 3, is complete) but not yet complete —
  // otherwise the gate would require the stage to be complete to allow the very
  // action that completes it. Other stages use the plain sequential gate.
  const canCompleteStage4Now =
    currentStage === 4 &&
    !effectiveCompleted.includes(4) &&
    currentStatus === "available"
  const canAdvance =
    currentStage < LAST_STAGE &&
    (derived.canAdvance(currentStage) || canCompleteStage4Now)

  // ---- actions -----------------------------------------------------------

  /**
   * Stage 3 (DAG generation) — completion comes from choosing the DAG. The
   * DagStageView calls this with the backend-returned states (Stage 3 already
   * `concluido`); we update `completed` from them and re-read as the source of
   * truth.
   */
  const handleDagChosen = (states: CaseStageStates) => {
    // Reconcile the gate straight from the backend-returned states. We do NOT
    // call rereadState() here: it bumps refreshKey and would remount the
    // DagStageView mid-render, collapsing the just-drawn Mermaid diagram. The
    // states already carry Stage 3 concluded + Stage 4 unlocked.
    const completedFromBackend = states.stages
      .filter((e) => e.state === "concluido")
      .map((e) => e.stage)
    setCompleted(completedFromBackend)
  }

  /**
   * Stage 2 (Campaign selection) — the chosen campaign's curated slug becomes
   * the campaignId that drives Stages 3..6; then re-read the backend.
   */
  const handleCampaignCompleted = (curatedSlug: string) => {
    // Idempotent: CampaignView may report the same already-concluded
    // selection more than once (e.g. after a remount). Only react when the
    // chosen campaign actually changes, so we never loop by re-setting state
    // that triggers another report. No refreshKey bump here — the stage
    // transition (handleNext) is what forces a reload.
    if (curatedSlug === campaignId && stage2Ok) return
    setCampaignId(curatedSlug)
    // Successful extraction concludes Stage 2 (client-side verdict).
    setStage2Ok(true)
    // The effect keyed on `activeId` re-reads the backend; also read now for
    // responsiveness so the gate reflects the chosen campaign immediately.
    getCampaign(curatedSlug)
      .then((campaign) => setCompleted(completedStages(campaign.states)))
      .catch(() => setError(t("stageView.loadError")))
  }

  /** Stage 1 — the researcher chose/changed the STIX base. */
  const handleVersionSelected = (next: string) => {
    // Empty string means "change base" (back to the list) in BaseStixView.
    setSelectedVersion(next || undefined)
    // Changing/clearing the base invalidates the Stage 1 verdict until the
    // new base's bundle + dashboard are confirmed again, and also drops any
    // prior campaign selection (Stage 2) made against the old base.
    if (!next) {
      setStage1Ok(false)
      setStage2Ok(false)
    }
  }

  const handleNext = async () => {
    setError(null)
    // From Stage 4, advancing means completing Operationalization.
    if (currentStage === 4 && !completed.includes(4) && activeId) {
      setBusy(true)
      try {
        await completeOperationalization(activeId)
        await rereadState()
      } catch (e) {
        setError(e instanceof Error ? e.message : t("stageView.loadError"))
        setBusy(false)
        return
      }
      setBusy(false)
    }
    setCurrentStage((n) => {
      const nextCurrent = n < LAST_STAGE ? ((n + 1) as StageNumber) : n
      setMaxVisitedStage((m) => Math.max(m, nextCurrent) as StageNumber)
      return nextCurrent
    })
  }

  const handleBack = () => {
    setError(null)
    setCurrentStage((n) => (n > 1 ? ((n - 1) as StageNumber) : n))
  }

  const handleRestart = async () => {
    if (!activeId) return
    setBusy(true)
    setError(null)
    try {
      await resetCampaign(activeId)
      setCurrentStage(1)
      setMaxVisitedStage(1)
      setStage2Ok(false)
      await rereadState()
    } catch {
      setError(t("stageView.loadError"))
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
            {t(`stages.${currentStage}.name`)}
          </CardTitle>
          <span className="text-sm text-muted-foreground">
            {t("wizard.stepIndicator", {
              atual: currentStage,
              total: LAST_STAGE,
            })}
          </span>
        </div>

        {/* Six-stage badge trail. `data-status` keeps exposing the gate status
            (`deriveStates`) — the source of truth for advancing and the
            attribute asserted by tests —, while the badge visual follows the
            presented state (`derivePresentation`): gradual reveal + the
            pre-completed (curated) mark. */}
        <ol className="flex flex-wrap items-center gap-2">
          {derived.stages.map((e) => {
            const ap = presentation.stages.find((x) => x.stage === e.stage)
            const presented = ap?.presented ?? e.status
            const badgeState: "concluido" | "nao_iniciado" =
              presented === "completed" ? "concluido" : "nao_iniciado"
            const blocked = presented === "blocked"
            return (
              <li
                key={e.stage}
                className={cn(
                  "flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs",
                  e.stage === currentStage &&
                    "border-team-blue ring-1 ring-team-blue",
                )}
                data-stage={e.stage}
                data-status={e.status}
                data-apresentado={presented}
                aria-current={e.stage === currentStage ? "step" : undefined}
              >
                <span className="font-medium">{t(`stages.${e.stage}.short`)}</span>
                <StageStateBadge
                  state={badgeState}
                  blocked={blocked}
                  preCompleted={ap?.preCompleted ?? false}
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
        {currentStagePreCompleted ? (
          <p
            className="rounded-md border border-team-blue/40 bg-team-blue/10 px-3 py-2 text-xs text-team-blue"
            data-testid="pre-completed-note"
          >
            {t("wizard.preConcluidoNota")}
          </p>
        ) : null}

        {/* Current stage body. 1 = STIX base selection, 2 = campaign selection,
            3 = DAG generation, 4 = operationalization, 5 = environment,
            6 = execution/measurement/report. Stages 1-2 use the probe id to
            list options; 3..6 require the chosen campaign id. */}
        <div key={`${activeId ?? "nocampaign"}-${currentStage}-${refreshKey}`}>
          {currentStage === 1 ? (
            <BaseStixView
              campaignId={activeId ?? ""}
              version={selectedVersion}
              onVersionSelected={handleVersionSelected}
              onCompletionChange={setStage1Ok}
            />
          ) : null}
          {currentStage === 2 ? (
            // Index Stage 2 by the chosen campaign once one is picked,
            // falling back to the probe only while none is chosen. Keying on
            // activeId makes the read-only selection the backend returns match
            // the campaign the researcher actually selected (no reverting to
            // the probe's campaign).
            <CampaignView
              campaignId={activeId ?? ""}
              version={selectedVersion}
              onCompleted={handleCampaignCompleted}
            />
          ) : null}
          {currentStage === 3 && campaignId ? (
            <DagStageView campaignId={campaignId} onDagChosen={handleDagChosen} />
          ) : null}
          {currentStage === 4 && campaignId ? (
            <div className="grid gap-4 lg:grid-cols-2">
              <Stage2View campaignId={campaignId} />
              <EnvironmentParametersView campaignId={campaignId} />
            </div>
          ) : null}
          {currentStage === 5 && campaignId ? (
            <EnvironmentStageView
              campaignId={campaignId}
              // Provisioning happens WHILE on Stage 5; when it concludes we
              // re-read the backend state so Stage 5 shows `concluido` and the
              // "Next" gate unlocks (Req. 6.4, 7.3). `rereadState` is a
              // stable useCallback so it won't re-trigger provisioning.
              onProvisioningComplete={handleProvisioningComplete}
            />
          ) : null}
          {currentStage === 6 && campaignId ? (
            <div className="flex flex-col gap-4">
              <Stage3View
                campaignId={campaignId}
                // Auto-start blocked (Stage 5 not ready): send the researcher
                // back to Stage 5 within ≤ 3s (Req. 1.4).
                onRedirectToStage={(stage) =>
                  setCurrentStage(Math.min(Math.max(stage, 1), LAST_STAGE) as StageNumber)
                }
              />
              <ReportView campaignId={campaignId} />
            </div>
          ) : null}
          {currentStage >= 3 && !campaignId ? (
            <p className="text-sm text-muted-foreground" role="status">
              {t("wizard.selectCampaignFirst")}
            </p>
          ) : null}
        </div>

        {error ? (
          <p className="text-sm text-team-red" role="alert">
            {error}
          </p>
        ) : null}

        {/* Wizard navigation controls. */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={
                currentStage === 1 && onExitToStart ? onExitToStart : handleBack
              }
              disabled={busy || (currentStage === 1 && !onExitToStart)}
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
            {/* Stage 6 (Execution) auto-starts via the inline confirmation
                embedded in Stage3View (Req. 1.1, 1.3) — no run button here. */}

            {/* Next (stages 1..5). */}
            {currentStage < LAST_STAGE ? (
              <Button
                size="sm"
                onClick={handleNext}
                disabled={!canAdvance || busy}
                aria-disabled={!canAdvance || busy}
                aria-label={t("wizard.nextAria")}
                title={!canAdvance ? t("wizard.blockedHint") : undefined}
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

        {currentStatus === "blocked" ? (
          <p className="text-xs text-muted-foreground">{t("wizard.blockedHint")}</p>
        ) : null}
      </CardContent>
    </Card>
  )
}
