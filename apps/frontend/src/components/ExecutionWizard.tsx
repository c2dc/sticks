import { useState } from "react"
import { useTranslation } from "react-i18next"
import {
  ArrowLeft,
  CheckCircle2,
  ClipboardCheck,
  Container,
  Loader2,
  Play,
  Rocket,
  Terminal,
  Workflow,
  XCircle,
  type LucideIcon,
} from "lucide-react"

import { GuidedRunView } from "@/components/GuidedRunView"
import { InputScreen } from "@/components/InputScreen"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { campaignDisplayName } from "@/lib/campaign"
import type { AiRun } from "@/lib/ai"
import { useAiRun } from "@/lib/use-ai-run"
import { cn } from "@/lib/utils"

/**
 * The header phase of the run, derived like `GuidedRunView` does: not started,
 * the running phase, the phase awaiting approval, or a terminal status.
 */
function headerPhase(
  run: AiRun | null,
): { icon: LucideIcon; labelKey: string; spin?: boolean; tone?: string } {
  if (!run) return { icon: Rocket, labelKey: "aiRun.phaseName.ready" }
  if (run.status === "completed") {
    return { icon: CheckCircle2, labelKey: "aiRun.status.completed", tone: "text-team-blue" }
  }
  if (run.status === "failed" || run.status === "aborted") {
    return { icon: XCircle, labelKey: `aiRun.status.${run.status}`, tone: "text-team-red" }
  }
  const phase = run.status === "awaiting_approval" ? run.pending_phase : run.active_phase
  const icon = phase ? PHASE_ICONS[phase] : undefined
  if (phase && icon) return { icon, labelKey: `aiRun.phaseName.${phase}` }
  if (phase === "orchestration") return { icon: Loader2, labelKey: "aiRun.phaseName.orchestration", spin: true }
  return { icon: Loader2, labelKey: `aiRun.status.${run.status}`, spin: true }
}

const PHASE_ICONS: Record<string, LucideIcon> = {
  operationalization: Terminal,
  flow: Workflow,
  environment: Container,
  execution: Play,
  evaluation: ClipboardCheck,
}

/** Layer 2 shell: owns the run state so the header can show the current phase. */
function RunLayer({
  campaignId,
  campaignName,
  version,
  onChangeCampaign,
}: {
  campaignId: string
  campaignName?: string
  version: string
  onChangeCampaign: () => void
}) {
  const { t } = useTranslation("pipeline")
  const ai = useAiRun(campaignId)
  const { icon: PhaseIcon, labelKey, spin, tone } = headerPhase(ai.run)
  return (
    <Card data-testid="execution-wizard" className="flex flex-col gap-2">
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <CardTitle className="inline-flex items-center gap-2 text-lg" data-section="run-phase-title">
            <PhaseIcon
              className={cn("size-5", spin && "animate-spin", tone ?? "text-team-purple")}
              aria-hidden
            />
            {t(labelKey)}
          </CardTitle>
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">
              {t("input.run.campaign", {
                campaign: campaignDisplayName(campaignName, campaignId),
              })}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={onChangeCampaign}
              data-testid="back-to-input"
            >
              <ArrowLeft className="mr-1 size-4" aria-hidden />
              {t("input.run.changeCampaign")}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <GuidedRunView ai={ai} campaignId={campaignId} campaignName={campaignName} version={version} />
      </CardContent>
    </Card>
  )
}

/**
 * ExecutionWizard — the top-level AI-first flow reached from the Landing's
 * "Start campaign". It is organised as the design's **two conceptual layers**,
 * NOT the old linear `1..6` ruler:
 *
 * - **Layer 1 — Input** ({@link InputScreen}): a SINGLE stylish screen where the
 *   Researcher picks the STIX package version AND the campaign to run, one below
 *   the other (the campaign panel reveals once a base is ready). Selecting a
 *   campaign carries its `campaign_key` into the Run.
 *
 * - **Layer 2 — Run** ({@link GuidedRunView}): the conversational, orchestrator-
 *   led AI loop as the PRIMARY view. A friendly robot reveals each phase
 *   (operationalization → attack flow → environment → execution) and asks permission to
 *   proceed, conducting the Researcher through the multi-agent pipeline. A
 *   "change campaign" control returns to Input.
 *
 * The selection listings use the `/stix` endpoints, which depend only on the
 * STIX base version. The selection is client state: the chosen campaign key
 * (with the version) is handed to the Run, which opens on the operationalization
 * screen and only starts the AI run after the Researcher reviews it.
 */
export function ExecutionWizard({
  onExitToStart,
}: {
  /** "Back" on the Input screen leaves the flow and returns to the Landing. */
  onExitToStart?: () => void
}) {
  /** Which layer is in view: Input (selection) or the Run (Layer 2). */
  const [phase, setPhase] = useState<"input" | "run">("input")

  // STIX base version chosen in Input; consumed by the base + campaign panels.
  const [selectedVersion, setSelectedVersion] = useState<string | undefined>(
    undefined,
  )
  // Base completion verdict (bundle on disk), reported by BaseStixView. Gates
  // the campaign panel on the Input screen.
  const [baseReady, setBaseReady] = useState(false)

  // The chosen campaign's `campaign_key` — set when a campaign is selected in
  // Input. This drives the Run. Until then it is undefined.
  const [campaignId, setCampaignId] = useState<string | undefined>(undefined)
  // The chosen campaign's readable name, shown wherever the slug would be.
  const [campaignName, setCampaignName] = useState<string | undefined>(undefined)

  // The campaign is selected AND its overview loaded. The Researcher stays on
  // Input to review the campaign overview, then advances explicitly via the
  // Next button (no auto-jump on selection).
  const [campaignReady, setCampaignReady] = useState(false)

  // ---- Input actions -----------------------------------------------------

  /** The Researcher chose/changed the STIX base. Empty string = "change base". */
  const handleVersionSelected = (next: string) => {
    setSelectedVersion(next || undefined)
    if (!next) setBaseReady(false)
    // A campaign belongs to one base version: drop any previous selection.
    setCampaignId(undefined)
    setCampaignReady(false)
  }

  /** A campaign was selected. Its key becomes the Run's campaignId. */
  const handleCampaignSelected = (campaignKey: string, name?: string) => {
    setCampaignName(campaignDisplayName(name, campaignKey))
    // Record the selection and reveal the overview + Next action; do NOT
    // jump to the Run here, so the Researcher can review the campaign first.
    setCampaignId(campaignKey)
    setCampaignReady(true)
  }

  /** Explicit advance to the operationalization screen (the Next button). */
  const handleAdvanceToRun = () => {
    if (campaignReady && campaignId && selectedVersion) setPhase("run")
  }

  /** From the Run, return to Input to pick a different campaign. */
  const handleBackToInput = () => {
    setPhase("input")
  }

  // ---- render ------------------------------------------------------------

  // Layer 2 — the guided, orchestrator-led Run as the PRIMARY view.
  if (phase === "run" && campaignId && selectedVersion) {
    return (
      <RunLayer
        campaignId={campaignId}
        campaignName={campaignName}
        version={selectedVersion}
        onChangeCampaign={handleBackToInput}
      />
    )
  }

  // Layer 1 — the unified Input selection screen.
  return (
    <div data-testid="execution-wizard">
      <InputScreen
        selectedVersion={selectedVersion}
        baseReady={baseReady}
        onVersionSelected={handleVersionSelected}
        onBaseCompletionChange={setBaseReady}
        onCampaignSelected={handleCampaignSelected}
        campaignReady={campaignReady}
        onAdvanceToRun={handleAdvanceToRun}
        onExitToStart={onExitToStart}
      />
    </div>
  )
}
