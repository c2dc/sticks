import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { ArrowLeft } from "lucide-react"

import { GuidedRunView } from "@/components/GuidedRunView"
import { OrchestratorAvatar } from "@/components/OrchestratorAvatar"
import { SetupScreen } from "@/components/SetupScreen"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { fetchCases } from "@/lib/campaigns"
import type { CampaignSelectionResult } from "@/lib/stages"

/**
 * ExecutionWizard — the top-level AI-first flow reached from the Landing's
 * "Start campaign". It is organised as the design's **two conceptual layers**,
 * NOT the old linear `1..6` ruler:
 *
 * - **Layer 1 — Setup** ({@link SetupScreen}): a SINGLE stylish screen where the
 *   Researcher picks the STIX package version AND the campaign to run, one below
 *   the other (the campaign panel reveals once a base is ready). Selecting a
 *   campaign carries its `campaign_id` (and curated-ness) into the Run.
 * - **Layer 2 — Run** ({@link GuidedRunView}): the conversational, orchestrator-
 *   led AI loop as the PRIMARY view. A friendly robot reveals each phase (DAG →
 *   operationalization → environment → execution) and asks permission to
 *   proceed, conducting the Researcher through the multi-agent pipeline. A
 *   "change campaign" control returns to Setup.
 *
 * The selection listings depend only on the STIX base, so they run against a
 * `probeId` — the first curated slug fetched from the backend — purely to list
 * options. The chosen campaign's slug becomes the Run's `campaignId`.
 */
export function ExecutionWizard({
  onExitToStart,
}: {
  /** "Back" on the Setup screen leaves the flow and returns to the Landing. */
  onExitToStart?: () => void
}) {
  const { t } = useTranslation("pipeline")

  /** Which layer is in view: Setup (selection) or the Run (Layer 2). */
  const [phase, setPhase] = useState<"setup" | "run">("setup")

  // STIX base version chosen in Setup; consumed by the base + campaign panels.
  const [selectedVersion, setSelectedVersion] = useState<string | undefined>(
    undefined,
  )
  // Base completion verdict (bundle on disk), reported by BaseStixView. Gates
  // the campaign panel on the Setup screen.
  const [baseReady, setBaseReady] = useState(false)

  // Probe slug used only to list base/campaign options before a campaign is
  // chosen. Any curated slug works since those listings depend on the base.
  const [probeId, setProbeId] = useState<string | undefined>(undefined)

  // The chosen campaign's slug — set when a campaign is selected in Setup. This
  // drives the Run. Until then it is undefined.
  const [campaignId, setCampaignId] = useState<string | undefined>(undefined)

  // The campaign is selected AND its extraction completed. The Researcher
  // stays on Setup to review the campaign overview, then advances to the Run
  // explicitly via the Next button (no auto-jump on selection).
  const [campaignReady, setCampaignReady] = useState(false)

  // The id the Setup selection screens address is ALWAYS the probe slug. The
  // campaign list depends only on the STIX base, and the Stage 2 extraction is
  // keyed by the chosen slug in the request BODY (not by this path id), so a
  // stable probe id here keeps the extraction correct while preventing
  // CampaignView from remounting on selection (which would clear the
  // just-shown campaign overview). The chosen campaign is tracked separately
  // in `campaignId` and only drives the Run (Layer 2).
  const activeId = probeId ?? ""

  // Resolve the probe slug once on mount (first curated case).
  useEffect(() => {
    let active = true
    fetchCases()
      .then((resp) => {
        if (!active) return
        const first = resp.campaigns[0]?.id
        if (first) setProbeId(first)
      })
      .catch(() => {
        // Best-effort: selecting a campaign later provides the real id.
      })
    return () => {
      active = false
    }
  }, [])

  // ---- Setup actions -----------------------------------------------------

  /** The Researcher chose/changed the STIX base. Empty string = "change base". */
  const handleVersionSelected = (next: string) => {
    setSelectedVersion(next || undefined)
    if (!next) setBaseReady(false)
  }

  /** A campaign was selected. Its slug becomes the Run's campaignId. */
  const handleCampaignCompleted = (
    curatedSlug: string,
    _result: CampaignSelectionResult,
  ) => {
    // Record the selection and reveal the overview + Next action; do NOT
    // jump to the Run here, so the Researcher can review the campaign first.
    setCampaignId(curatedSlug)
    setCampaignReady(true)
  }

  /** Explicit advance to the guided Run, triggered by the Next button. */
  const handleAdvanceToRun = () => {
    if (campaignReady && campaignId) setPhase("run")
  }

  /** From the Run, return to Setup to pick a different campaign. */
  const handleBackToSetup = () => {
    setPhase("setup")
  }

  // ---- render ------------------------------------------------------------

  // Layer 2 — the guided, orchestrator-led Run as the PRIMARY view.
  if (phase === "run" && campaignId) {
    return (
      <Card data-testid="execution-wizard" className="flex flex-col gap-2">
        <CardHeader className="gap-3">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <CardTitle className="inline-flex items-center gap-2 text-lg">
              <OrchestratorAvatar mood="idle" className="size-8" />
              {t("setup.run.title")}
            </CardTitle>
            <div className="flex items-center gap-3">
              <span className="text-sm text-muted-foreground">
                {t("setup.run.campaign", { campanha: campaignId })}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={handleBackToSetup}
                data-testid="back-to-setup"
              >
                <ArrowLeft className="mr-1 size-4" aria-hidden />
                {t("setup.run.changeCampaign")}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <GuidedRunView campaignId={campaignId} />
        </CardContent>
      </Card>
    )
  }

  // Layer 1 — the unified Setup selection screen.
  return (
    <div data-testid="execution-wizard">
      <SetupScreen
        activeId={activeId}
        selectedVersion={selectedVersion}
        baseReady={baseReady}
        onVersionSelected={handleVersionSelected}
        onBaseCompletionChange={setBaseReady}
        onCampaignCompleted={handleCampaignCompleted}
        campaignReady={campaignReady}
        onAdvanceToRun={handleAdvanceToRun}
        onExitToStart={onExitToStart}
      />
    </div>
  )
}
