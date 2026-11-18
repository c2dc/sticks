import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import { useTranslation } from "react-i18next"
import { Container } from "lucide-react"

import { formatAttackDescription } from "@/lib/attack-description"
import { useAsyncData } from "@/lib/use-async-data"
import { cn } from "@/lib/utils"
import {
  getCampaignOverview,
  getCampaigns,
  type StixCampaignOverview,
  type StixCampaignSummary,
  type TechniqueElement,
} from "@/lib/stix"

/**
 * CampaignView — compact **campaign selection** within the chosen STIX package.
 *
 * A simple, searchable list of the campaigns in the bundle; clicking a
 * campaign selects it directly (no separate "select" button) and shows a
 * lightweight OVERVIEW panel (identity + aliases + description + a technique
 * summary + attributed actors) — not a heavy dashboard. Every campaign is
 * selectable (no curated gating). The selection is client state only: once the
 * overview of the chosen campaign loads, its key is reported to the wizard,
 * which then moves on to the operationalization screen.
 *
 * Flow:
 * - On mount, reads `getCampaigns(version)` (`GET /stix/bases/{version}/campaigns`).
 * - Clicking a campaign fetches `getCampaignOverview(version, campaignKey)` and
 *   renders the overview panel from the persisted package (Req. 7.3, 7.4). No
 *   server-side selection exists: the run materializes the campaign on start.
 */
export interface CampaignViewProps {
  version: string
  /**
   * Called with the chosen `campaign_key` and the campaign's readable name once
   * its overview has loaded.
   */
  onCompleted?: (campaignKey: string, campaignName?: string) => void
}

/**
 * Remounts the inner view when the version changes, which resets the selection
 * state (selected campaign, overview, completion report) without an effect.
 */
export function CampaignView(props: CampaignViewProps) {
  return <CampaignViewForVersion key={props.version} {...props} />
}

function CampaignViewForVersion({ version, onCompleted }: CampaignViewProps) {
  const { t } = useTranslation("pipeline")

  // Report a selection to the wizard at most once per key.
  const completionReported = useRef<string | null>(null)
  const onCompletedRef = useRef(onCompleted)
  useEffect(() => {
    onCompletedRef.current = onCompleted
  }, [onCompleted])
  const reportCompletion = useCallback((key: string, name?: string) => {
    if (completionReported.current === key) return
    completionReported.current = key
    onCompletedRef.current?.(key, name)
  }, [])

  const campaignsState = useAsyncData<StixCampaignSummary[]>(version, () => getCampaigns(version))
  const campaigns = campaignsState.data
  const loadError = campaignsState.error ? t("phaseView.loadError") : null
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)
  const [selecting, setSelecting] = useState(false)
  // Distinguish "no selection yet" from "selection attempted".
  const [selectionApplied, setSelectionApplied] = useState(false)
  // Set when the overview of the chosen campaign could not be read.
  const [selectionFailed, setSelectionFailed] = useState(false)
  // The campaign overview served by `getCampaignOverview` (Req. 7.4).
  const [overview, setOverview] = useState<StixCampaignOverview | null>(null)
  // Preserve the reader's scroll position when switching the selected
  // campaign. Selecting a different campaign briefly collapses the overview
  // panel below the list (to a one-line "selecting…" placeholder) and then
  // re-expands it with the new campaign's content. That momentary shrink of
  // the page makes the browser clamp the scroll position toward the top, so
  // the reader loses their place. We capture the scroll offset of the nearest
  // scrolling ancestor at selection time and restore it through the layout
  // transitions until the overview settles.
  const rootRef = useRef<HTMLDivElement | null>(null)
  const preservedScroll = useRef<{ element: Element | Window; top: number } | null>(null)

  // Find the nearest scrollable ancestor of `node`, falling back to the window
  // (the InputScreen scrolls the page itself, not an inner container).
  const findScrollParent = (node: HTMLElement | null): Element | Window => {
    let current: HTMLElement | null = node?.parentElement ?? null
    while (current) {
      const style = window.getComputedStyle(current)
      const overflowY = style.overflowY
      if (
        (overflowY === "auto" || overflowY === "scroll") &&
        current.scrollHeight > current.clientHeight
      ) {
        return current
      }
      current = current.parentElement
    }
    return window
  }

  const scrollTopOf = (target: Element | Window): number =>
    target === window
      ? window.scrollY
      : (target as Element).scrollTop

  const restoreScroll = (target: Element | Window, top: number): void => {
    if (target === window) {
      // Prefer setting the document scrolling element directly: it is the most
      // portable path and avoids the browser's smooth-scroll heuristics (and,
      // in jsdom, the unimplemented `window.scrollTo`). Fall back to
      // `window.scrollTo` only when no scrolling element is exposed.
      if (document.scrollingElement) {
        document.scrollingElement.scrollTop = top
      } else if (typeof window.scrollTo === "function") {
        window.scrollTo({ top, behavior: "auto" })
      }
    } else {
      ;(target as Element).scrollTop = top
    }
  }

  // Restore the captured scroll position across the layout changes that happen
  // while a selection is in flight and once the overview re-expands. Runs
  // synchronously before paint so the reader never sees the jump.
  useLayoutEffect(() => {
    const pending = preservedScroll.current
    if (!pending) return
    restoreScroll(pending.element, pending.top)
    // Clear once the selection has settled and the overview has resolved, so a
    // later manual scroll by the reader is not fought by a stale restore.
    if (!selecting) preservedScroll.current = null
  }, [selecting, overview, selectionFailed])


  // Clicking a campaign selects it directly (Req. 7.3): the overview is read
  // from `getCampaignOverview(version, campaignKey)` and, once it loads, the
  // selection is reported to the wizard (Req. 7.4).
  const handleSelect = async (slug: string) => {
    // Capture the scroll position BEFORE the state changes below reflow the
    // page, so the layout effect can hold the reader in place.
    const scrollTarget = findScrollParent(rootRef.current)
    preservedScroll.current = { element: scrollTarget, top: scrollTopOf(scrollTarget) }

    setSelectedSlug(slug)
    setSelecting(true)
    setSelectionApplied(true)
    setSelectionFailed(false)
    setOverview(null)

    try {
      const loaded = await getCampaignOverview(version, slug)
      setOverview(loaded)
      const chosen = campaigns?.find((c) => c.campaign_key === slug)
      reportCompletion(slug, chosen?.name)
    } catch {
      setSelectionFailed(true)
    } finally {
      setSelecting(false)
    }
  }

  if (loadError) {
    return <p className="text-sm text-muted-foreground">{loadError}</p>
  }
  if (!campaigns) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("phaseView.loading")}
      </p>
    )
  }

  return (
    <div ref={rootRef} className="flex flex-col gap-3">
      <CampaignList
        campaigns={campaigns}
        selectedSlug={selectedSlug}
        selecting={selecting}
        onSelect={handleSelect}
      />

      {/* Compact overview — only after a selection is applied (Req. 8.4). */}
      {selectionApplied || selecting ? (
        <div data-section="atributos">
          <CampaignOverview
            overview={overview}
            failed={selectionFailed}
            applied={selectionApplied}
            selecting={selecting}
          />
        </div>
      ) : null}
    </div>
  )
}

/**
 * Simple, searchable campaign list. Every campaign in the bundle is selectable
 * (no curated/non-curated distinction). A compact search box filters by name.
 */
function CampaignList({
  campaigns,
  selectedSlug,
  selecting,
  onSelect,
}: {
  campaigns: StixCampaignSummary[]
  selectedSlug: string | null
  selecting: boolean
  onSelect: (slug: string) => void
}) {
  const { t } = useTranslation("pipeline")
  const [query, setQuery] = useState("")

  const slugOf = (campaign: StixCampaignSummary): string =>
    campaign.campaign_key

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return campaigns
    return campaigns.filter((c) => (c.name ?? "").toLowerCase().includes(q))
  }, [campaigns, query])

  if (campaigns.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("campaign.noCampaigns")}</p>
  }

  return (
    <div className="flex flex-col gap-2">
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("campaign.searchPlaceholder")}
        aria-label={t("campaign.searchLabel")}
        className="h-9 w-full rounded-md border bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-team-blue"
      />

      {filtered.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("campaign.noResults")}</p>
      ) : (
        <ul
          className="flex max-h-64 flex-col gap-1 overflow-y-auto pr-1"
          aria-label={t("campaign.listLabel")}
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
                  onClick={() => onSelect(slug)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors",
                    selected
                      ? "border-team-blue ring-1 ring-team-blue"
                      : "hover:bg-muted/50",
                    selecting && "cursor-not-allowed opacity-60",
                  )}
                >
                  <span className="font-medium">{campaign.name}</span>
                  <ContainerEmulabilityMark campaign={campaign} />
                </button>
              </li>
            )
          })}
        </ul>
      )}

      <span className="text-xs text-muted-foreground" aria-live="polite">
        {t("campaign.count", { count: filtered.length })}
      </span>
    </div>
  )
}

/**
 * Per-campaign Container_Emulability marking in the campaign list (Req. 6.4,
 * 6.5). Shows a container icon ONLY on campaigns marked container-emulable
 * (`container_emulable === true`) — the only ones runnable end-to-end at this
 * phase. A non-emulable campaign (`container_emulable === false`) stays in the
 * list (never removed) with an accessible, screen-reader-only reason naming the
 * blocking techniques when known. A campaign whose marking has not been derived
 * yet (`null`/`undefined`) renders nothing, so the absence of an icon is never
 * mistaken for a non-emulable verdict.
 *
 * Both the icon and the reason use English i18n keys with en + pt-BR values
 * (I18n_Parity); the icon itself carries an `aria-label` so assistive tech
 * announces it rather than treating it as decorative.
 */
function ContainerEmulabilityMark({ campaign }: { campaign: StixCampaignSummary }) {
  const { t } = useTranslation("pipeline")

  if (campaign.container_emulable === true) {
    return (
      <span
        className="ml-auto inline-flex shrink-0 items-center text-team-blue"
        data-testid="container-emulable-icon"
        title={t("campaign.containerEmulability.emulable")}
      >
        <Container
          className="size-4"
          aria-label={t("campaign.containerEmulability.emulable")}
        />
      </span>
    )
  }

  if (campaign.container_emulable === false) {
    const blocking = campaign.blocking_techniques ?? []
    const reason =
      blocking.length > 0
        ? t("campaign.containerEmulability.notEmulableWithTechniques", {
            techniques: blocking.join(", "),
          })
        : t("campaign.containerEmulability.notEmulable")
    return (
      <span className="sr-only" data-testid="container-not-emulable-reason">
        {reason}
      </span>
    )
  }

  return null
}

/**
 * The compact overview panel of the selected campaign: identity line (name +
 * ATT&CK id), aliases, a short description, a technique summary and the
 * attributed actors. Mounted only after a selection is applied (Req. 8.4).
 *
 * The content is served by `getCampaignOverview` (`overview`, Req. 7.4); a
 * failed read is surfaced as an alert and keeps the selection uncompleted.
 */
function CampaignOverview({
  overview,
  failed,
  applied,
  selecting,
}: {
  overview: StixCampaignOverview | null
  failed: boolean
  applied: boolean
  selecting: boolean
}) {
  const { t } = useTranslation("pipeline")

  if (selecting) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("campaign.selecting")}
      </p>
    )
  }
  if (!applied || (!overview && !failed)) {
    return <p className="text-sm text-muted-foreground">{t("campaign.notExtracted")}</p>
  }

  return (
    <div
      className="flex flex-col gap-2 rounded-md border bg-muted/20 p-3"
      data-section="atributos-extraidos"
    >
      {overview ? <OverviewBody overview={overview} /> : null}

      {!failed ? (
        <p className="sr-only" data-testid="campaign-completed" role="status">
          {t("campaign.completed")}
        </p>
      ) : (
        <div
          className="flex flex-col gap-0.5 rounded-md border border-team-red/30 bg-team-red/5 px-2 py-1.5 text-team-red"
          role="alert"
          data-testid="campaign-failure"
        >
          <p className="text-xs">{t("campaign.failure")}</p>
        </div>
      )}
    </div>
  )
}

/**
 * The richer campaign overview served by `getCampaignOverview` (Req. 7.4):
 * identity (name + ATT&CK id), aliases, a short description, the ATT&CK
 * technique summary and the attributed actors.
 */
function OverviewBody({ overview }: { overview: StixCampaignOverview }) {
  return (
    <>
      <IdentityOverview overview={overview} />
      <TechniqueSummary techniques={overview.techniques} />
      <ActorSummary actors={overview.actors} />
    </>
  )
}

/** Compact identity line: name, ATT&CK id, aliases, short description. */
function IdentityOverview({ overview }: { overview: StixCampaignOverview }) {
  const { t } = useTranslation("pipeline")
  // An alias equal to the campaign name adds nothing; show only real alternatives.
  const name = overview.name.trim().toLowerCase()
  const aliases = overview.aliases.filter((alias) => alias.trim().toLowerCase() !== name)
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-sm font-semibold">
          {overview.name || t("campaign.attributes")}
        </span>
        {overview.external_id ? (
          <span className="rounded border px-1.5 py-0.5 text-xs font-medium text-muted-foreground">
            {overview.external_id}
          </span>
        ) : null}
      </div>
      {aliases.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {aliases.map((alias) => (
            <span
              key={alias}
              className="rounded-full border border-team-blue/30 bg-team-blue/5 px-2 py-0.5 text-xs"
            >
              {alias}
            </span>
          ))}
        </div>
      ) : null}
      {overview.description ? (
        <p className="whitespace-pre-line text-xs text-muted-foreground">
          {formatAttackDescription(overview.description)}
        </p>
      ) : null}
    </div>
  )
}

/** A short technique summary: count + a few technique chips. */
function TechniqueSummary({ techniques }: { techniques: TechniqueElement[] }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("campaign.techniques", { count: techniques.length })}
      </span>
      {techniques.length === 0 ? (
        <span className="text-xs text-muted-foreground">{t("campaign.noTechniques")}</span>
      ) : (
        <ul className="flex flex-wrap gap-1">
          {techniques.map((technique, index) => (
            <li
              key={technique.technique_id ?? index}
              className="rounded border border-team-blue/30 bg-team-blue/5 px-1.5 py-0.5 text-xs"
              title={technique.technique_name ?? undefined}
            >
              <span className="font-medium">{technique.technique_id}</span>
              {technique.technique_name ? (
                <span className="text-muted-foreground"> · {technique.technique_name}</span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** The actors attributed to the campaign: count + a chip per actor. */
function ActorSummary({ actors }: { actors: { stix_id: string; name: string }[] }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("campaign.actors", { count: actors.length })}
      </span>
      {actors.length === 0 ? (
        <span className="text-xs text-muted-foreground">{t("campaign.noActors")}</span>
      ) : (
        <ul className="flex flex-wrap gap-1">
          {actors.map((actor) => (
            <li
              key={actor.stix_id}
              className="rounded border border-team-red/30 bg-team-red/5 px-1.5 py-0.5 text-xs"
            >
              <span className="font-medium">{actor.name}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
