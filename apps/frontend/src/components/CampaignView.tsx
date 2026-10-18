import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, Loader2 } from "lucide-react"

import { CampaignStixBlock } from "@/components/Stage1View"
import { EmptyNote, StageSection } from "@/components/StageSection"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import {
  getStageCampaign,
  selectCampaign,
  type ListedCampaign,
  type Stage2Response,
  type CampaignSelectionResult,
  type TechniqueElement,
} from "@/lib/stages"

/**
 * CampaignView — body of **Stage 2 (Campaign selection)** of the six-stage
 * scheme (Req. 4.1, 6.2, 6.3, 6.5).
 *
 * Stage 2 receives the STIX bundle of the base selected in Stage 1 and lets the
 * Researcher pick ONE campaign from the package. On selection, the backend
 * extracts the campaign attributes (STIX identity + ATT&CK techniques) and
 * completes the stage when the extraction succeeds (Req. 6.3, 6.5, 6.6). The 8
 * curated campaigns are selectable on version 18; other campaigns appear in the
 * list but disabled (no curated artifacts on disk — Req. 6.2, 6.5).
 *
 * Flow:
 * - On mount, reads `getStageCampaign(campaignId, version)` → list of campaigns +
 *   the current selection (read-only; opening the screen never executes).
 * - Selecting a campaign calls `selectCampaign(campaignId, slug, version)` →
 *   {@link CampaignSelectionResult}. An extraction failure surfaces in the typed
 *   result (`completed === false` with `error_message`/`failed_step`), not as an
 *   HTTP error (Req. 6.7).
 * - The extracted attributes are shown **reusing** the campaign identity block of
 *   `Stage1View` ({@link CampaignStixBlock}) plus the technique list; when
 *   `completed`, a completion indicator is shown.
 *
 * Props (lean contract for the wizard wiring in task 14.4):
 * - `campaignId`: Campaign slug; used in the Stage 2 wrappers.
 * - `version`: STIX base version selected in Stage 1 (optional; when omitted,
 *   the backend applies the default curated version).
 * - `onCompleted`: called after a successful extraction with the
 *   {@link CampaignSelectionResult} (`completed === true`). The wizard re-reads/
 *   derives its state from there.
 */
export function CampaignView({
  campaignId,
  version,
  onCompleted,
}: {
  campaignId: string
  version?: string
  onCompleted?: (
    curatedSlug: string,
    result: CampaignSelectionResult,
  ) => void
}) {
  const { t } = useTranslation("pipeline")

  // Report an already-completed selection to the wizard at most once per
  // load. When reopening Stage 2 for a campaign whose selection the backend
  // already persisted (read-only `selection.completed`), the wizard still needs
  // to learn the chosen slug so it adopts the campaignId and marks Stage 2
  // complete — without requiring the researcher to click "select" again.
  const completionReported = useRef<string | null>(null)
  // Keep the parent callback in a ref so `reportCompletion` has a stable
  // identity: the parent (wizard) re-creates onCompleted on every render, and
  // depending on it directly would re-run the load effect in a loop.
  const onCompletedRef = useRef(onCompleted)
  useEffect(() => {
    onCompletedRef.current = onCompleted
  }, [onCompleted])
  const reportCompletion = useCallback(
    (slug: string, result: CampaignSelectionResult) => {
      if (completionReported.current === slug) return
      completionReported.current = slug
      onCompletedRef.current?.(slug, result)
    },
    [],
  )

  const [data, setData] = useState<Stage2Response | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  // Current selection (extraction result). Starts with the read-only selection
  // returned by the Stage 2 read; updates on each `selectCampaign`.
  const [selection, setSelection] = useState<CampaignSelectionResult | null>(null)
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)
  const [selecting, setSelecting] = useState(false)
  // Distinguish "no selection yet" from "selection attempted": the initial
  // read-only read and a failed extraction can carry the same empty content, so
  // the content alone is not enough to choose between the "not extracted" note
  // and the failure block. `selectionApplied` becomes `true` when the initial
  // read already brings a completed selection OR when the Researcher triggers
  // `select`.
  const [selectionApplied, setSelectionApplied] = useState(false)

  useEffect(() => {
    let active = true
    setData(null)
    setLoadError(null)
    setSelection(null)
    setSelectedSlug(null)
    setSelectionApplied(false)
    completionReported.current = null
    getStageCampaign(campaignId, version)
      .then((resp) => {
        if (!active) return
        setData(resp)
        setSelection(resp.selection)
        // The read-only selection only "counts" as applied when it already came
        // completed (e.g. when reopening the screen of a case that already
        // selected a campaign).
        setSelectionApplied(resp.selection.completed)
        // Stage 2 requires an explicit campaign choice: picking the campaign
        // to reproduce is the core action of this stage. We do NOT auto-report
        // a read-only selection nor auto-pick the first campaign, so the
        // researcher actively selects (and the wizard never adopts the probe's
        // campaign on its own). The read-only selection is still shown for
        // context when reopening the stage.
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [campaignId, version, t])

  const handleSelect = async (slug: string) => {
    setSelecting(true)
    setLoadError(null)
    setSelectionApplied(true)
    try {
      const result = await selectCampaign(campaignId, slug, version)
      setSelection(result)
      // Report the chosen curated slug so the wizard can adopt it as the
      // campaignId that drives Stages 3..6.
      if (result.completed) reportCompletion(slug, result)
    } catch {
      setLoadError(t("stageView.loadError"))
    } finally {
      setSelecting(false)
    }
  }

  if (loadError) {
    return <EmptyNote>{loadError}</EmptyNote>
  }
  if (!data) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Bundle campaign list (Input) — campaign selection. */}
      <StageSection
        data-section="campaigns"
        label={t("campanha.titulo")}
        kind="blue"
        description={t("campanha.descricao")}
      >
        <CampaignList
          campaigns={data.campaigns}
          selectedSlug={selectedSlug}
          selecting={selecting}
          onSelectSlug={setSelectedSlug}
          onSelect={handleSelect}
        />
      </StageSection>

      {/* Extracted attributes (Output) — identity reusing the Stage1View block
          + the technique list, and the completion indicator. */}
      <StageSection
        data-section="atributos"
        label={t("campanha.atributos")}
        kind="neutral"
        className="md:col-span-2"
      >
        <ExtractedAttributes selection={selection} applied={selectionApplied} />
      </StageSection>
    </div>
  )
}

/**
 * Bundle campaign list. The Researcher can pick ANY campaign from the STIX
 * bundle — there is no curated/non-curated distinction in the UI (the future AI
 * operationalizer treats every campaign uniformly). A search box filters the
 * list by name so a specific campaign (e.g. ShadowRay) is easy to find among
 * the dozens in the bundle. No campaign is selected by default.
 */
function CampaignList({
  campaigns,
  selectedSlug,
  selecting,
  onSelectSlug,
  onSelect,
}: {
  campaigns: ListedCampaign[]
  selectedSlug: string | null
  selecting: boolean
  onSelectSlug: (slug: string) => void
  onSelect: (slug: string) => void
}) {
  const { t } = useTranslation("pipeline")
  const [query, setQuery] = useState("")

  // The identifier sent to the backend selection: prefer the curated slug when
  // present, otherwise fall back to the STIX id so every campaign is selectable.
  const slugOf = (campaign: ListedCampaign): string =>
    campaign.curated_slug ?? campaign.stix_id

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return campaigns
    return campaigns.filter((c) =>
      (c.name ?? "").toLowerCase().includes(q),
    )
  }, [campaigns, query])

  if (campaigns.length === 0) {
    return <EmptyNote>{t("campanha.semCampanhas")}</EmptyNote>
  }

  return (
    <div className="flex flex-col gap-3">
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("campanha.buscarPlaceholder")}
        aria-label={t("campanha.buscarLabel")}
        className="w-full rounded-md border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-team-blue"
      />

      <p className="text-xs text-muted-foreground" aria-live="polite">
        {t("campanha.contagem", { count: filtered.length })}
      </p>

      {filtered.length === 0 ? (
        <EmptyNote>{t("campanha.semResultados")}</EmptyNote>
      ) : (
        <ul
          className="flex max-h-96 flex-col gap-2 overflow-y-auto pr-1"
          aria-label={t("campanha.listaLabel")}
        >
          {filtered.map((campaign) => {
            const slug = slugOf(campaign)
            const selected = slug === selectedSlug
            return (
              <li key={campaign.stix_id}>
                <button
                  type="button"
                  aria-pressed={selected}
                  disabled={selecting}
                  onClick={() => onSelectSlug(slug)}
                  className={cn(
                    "flex w-full flex-col items-start gap-0.5 rounded-md border p-3 text-left text-sm transition-colors",
                    selected
                      ? "border-team-blue ring-1 ring-team-blue"
                      : "hover:bg-muted/50",
                    selecting && "cursor-not-allowed opacity-60",
                  )}
                >
                  <span className="font-medium">{campaign.name}</span>
                  {campaign.description ? (
                    <span className="line-clamp-2 text-xs text-muted-foreground">
                      {campaign.description}
                    </span>
                  ) : null}
                </button>
              </li>
            )
          })}
        </ul>
      )}

      <div className="flex items-center justify-end">
        <Button
          size="sm"
          onClick={() => selectedSlug && onSelect(selectedSlug)}
          disabled={!selectedSlug || selecting}
        >
          {selecting ? (
            <Loader2 className="mr-1 size-4 animate-spin" aria-hidden />
          ) : null}
          {selecting
            ? t("campanha.selecionando")
            : t("campanha.selecionar")}
        </Button>
      </div>
    </div>
  )
}

/**
 * Panel of the attributes extracted from the selected campaign. Before any
 * selection/extraction, it shows the `campanha.naoExtraido` note. After the
 * extraction:
 * - STIX identity reusing {@link CampaignStixBlock} (the same block as
 *   Stage1View, Req. 6.3);
 * - the list of extracted ATT&CK techniques (Req. 6.4);
 * - a completion indicator when `completed`, or the failure cause
 *   (`error_message`/`failed_step`) when the extraction failed (Req. 6.5, 6.7).
 */
function ExtractedAttributes({
  selection,
  applied,
}: {
  selection: CampaignSelectionResult | null
  /** `true` when a selection was already attempted/applied (vs. freshly opened). */
  applied: boolean
}) {
  const { t } = useTranslation("pipeline")

  // Freshly opened screen with no selection applied → nothing to show yet. A
  // failed extraction (`applied` with `!completed`) does NOT fall here: it shows
  // the failure block below with the reported cause (Req. 6.7).
  if (!selection || !applied) {
    return <EmptyNote>{t("campanha.naoExtraido")}</EmptyNote>
  }

  return (
    <div className="flex flex-col gap-3" data-section="atributos-extraidos">
      {/* STIX identity — reuses the Stage1View campaign block. */}
      <CampaignStixBlock campaign={selection.identity} />

      {/* Extracted ATT&CK techniques. */}
      <TechniquesList techniques={selection.techniques} />

      {/* Extraction completion or failure. */}
      {selection.completed ? (
        <p
          className="flex items-center gap-1.5 rounded-md border border-team-blue/40 bg-team-blue/10 px-3 py-2 text-xs text-team-blue"
          data-testid="campaign-completed"
          role="status"
        >
          <CheckCircle2 className="size-4" aria-hidden />
          {t("campanha.concluido")}
        </p>
      ) : (
        <div
          className="flex flex-col gap-1 rounded-md border border-team-red/30 bg-team-red/5 px-3 py-2 text-team-red"
          role="alert"
          data-testid="campaign-failure"
        >
          <p className="text-xs">{selection.error_message || t("campanha.falha")}</p>
          {selection.failed_step ? (
            <p className="text-xs text-muted-foreground">
              {t("campanha.falhaEtapa", { step: selection.failed_step })}
            </p>
          ) : null}
        </div>
      )}
    </div>
  )
}

/** List of the ATT&CK techniques extracted from the campaign (Req. 6.4). */
function TechniquesList({ techniques }: { techniques: TechniqueElement[] }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("campanha.tecnicas", { count: techniques.length })}
      </p>
      {techniques.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("campanha.semTecnicas")}</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {techniques.map((technique, index) => (
            <li
              key={technique.technique_id ?? index}
              className="rounded-md border border-team-blue/30 bg-team-blue/5 px-2 py-0.5 text-xs"
              title={technique.description ?? undefined}
            >
              <span className="font-medium">{technique.technique_id}</span>
              {technique.technique_name ? (
                <span className="text-muted-foreground">
                  {" "}
                  · {technique.technique_name}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
