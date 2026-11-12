import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"

import { StixBaseSelect } from "@/components/StixBaseSelect"
import { Button } from "@/components/ui/button"
import {
  getBases,
  getPackageAnalytics,
  type BaseStixInfo,
  getPackageOverview,
  type StixPackageAnalytics,
  type StixPackageOverview,
} from "@/lib/stix"
import { cn } from "@/lib/utils"

export interface BaseStixViewProps {
  /**
   * STIX base version already selected in the flow. When absent, the compact
   * base picker is shown. When present, a one-line confirmation of the
   * selection is shown (no heavy intelligence dashboard — Req. 7.3).
   */
  version?: string
  /**
   * Called when the Researcher picks a base. The wizard stores the selection
   * and passes it back via `version`, carrying it forward to campaign selection
   * (Req. 7.1, 7.4).
   */
  onVersionSelected?: (version: string) => void
  /**
   * Reported whenever the completion verdict is recomputed. `true` once the
   * selected version's bundle is on disk; the wizard uses this to unlock
   * campaign selection. A read failure keeps the step not completed (`false`).
   */
  onCompletionChange?: (completed: boolean) => void
}

/**
 * BaseStixView — the **STIX base selection** input step, now minimalist.
 *
 * - **No version selected** → {@link StixBaseSelect}: a compact dropdown of the
 *   known versions. Clicking a downloaded version selects it directly on the
 *   click (Req. 7.1); a version not yet on disk reveals an inline download
 *   kept as an explicit action (Req. 7.5). Selecting fires `onVersionSelected`.
 * - **Version selected** → a single-line confirmation with a "change base"
 *   action plus a compact **package overview** panel loaded via
 *   {@link getPackageOverview} (Req. 6.4, 7.2): the base identity, the per-type
 *   counts and the headline totals.
 *
 * In parallel it reads `getBases()` (`GET /stix/bases`) to confirm the
 * selected base is persisted ({@link BaseStixInfo.completed})
 * and propagates that verdict via `onCompletionChange` so the wizard can unlock
 * campaign selection. The wizard's "advance" follows from this completion
 * verdict, not from any selection button.
 */
export function BaseStixView({
  version,
  onVersionSelected,
  onCompletionChange,
}: BaseStixViewProps) {
  const { t } = useTranslation("pipeline")

  const [result, setResult] = useState<BaseStixInfo | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)

  // The package overview of the selected version (Req. 6.4, 7.2), loaded from
  // the database via getPackageOverview. Independent of the bundle-on-disk
  // completion check above.
  const [overview, setOverview] = useState<StixPackageOverview | null>(null)
  const [overviewError, setOverviewError] = useState<string | null>(null)
  const [overviewLoading, setOverviewLoading] = useState(false)

  // The analytic dashboard of the selected version (Req. 12.9), loaded from the
  // database via getPackageAnalytics. Independent of the overview above: a
  // failure here renders only the analytics error note and never disturbs the
  // overview panel.
  const [analytics, setAnalytics] = useState<StixPackageAnalytics | null>(null)
  const [analyticsError, setAnalyticsError] = useState<string | null>(null)
  const [analyticsLoading, setAnalyticsLoading] = useState(false)

  // Keep the last reported completion so we only notify the wizard on real
  // changes (avoids render loops when the callback is recreated each render).
  const lastCompletion = useRef<boolean | null>(null)
  const notifyCompletion = useCallback(
    (completed: boolean) => {
      if (lastCompletion.current === completed) return
      lastCompletion.current = completed
      onCompletionChange?.(completed)
    },
    [onCompletionChange],
  )

  // Confirm the selected base's bundle is on disk whenever the version changes.
  useEffect(() => {
    let active = true
    setLoadError(null)
    setResult(null)

    if (!version) {
      setChecking(false)
      notifyCompletion(false)
      return
    }

    setChecking(true)
    getBases()
      .then((bases) => {
        if (!active) return
        const found = bases.find((base) => base.version === version) ?? null
        setResult(found)
        notifyCompletion(found ? (found.completed ?? found.downloaded) : false)
      })
      .catch(() => {
        if (!active) return
        setLoadError(t("baseStix.error"))
        notifyCompletion(false)
      })
      .finally(() => {
        if (active) setChecking(false)
      })

    return () => {
      active = false
    }
  }, [version, t, notifyCompletion])

  // Load and display the package overview whenever the selected version
  // changes (Req. 6.4, 7.2). Read from the database only; never keep a stale
  // overview around while (re)loading or on failure.
  useEffect(() => {
    let active = true
    setOverview(null)
    setOverviewError(null)

    if (!version) {
      setOverviewLoading(false)
      return
    }

    setOverviewLoading(true)
    getPackageOverview(version)
      .then((data) => {
        if (!active) return
        setOverview(data)
      })
      .catch(() => {
        if (!active) return
        setOverviewError(t("baseStix.overview.error"))
        setOverview(null)
      })
      .finally(() => {
        if (active) setOverviewLoading(false)
      })

    return () => {
      active = false
    }
  }, [version, t])

  // Load and display the analytic dashboard whenever the selected version
  // changes (Req. 12.9). Read from the database only; isolated from the
  // overview so a failure here keeps the overview intact.
  useEffect(() => {
    let active = true
    setAnalytics(null)
    setAnalyticsError(null)

    if (!version) {
      setAnalyticsLoading(false)
      return
    }

    setAnalyticsLoading(true)
    getPackageAnalytics(version)
      .then((data) => {
        if (!active) return
        setAnalytics(data)
      })
      .catch(() => {
        if (!active) return
        setAnalyticsError(t("baseStix.analytics.error"))
        setAnalytics(null)
      })
      .finally(() => {
        if (active) setAnalyticsLoading(false)
      })

    return () => {
      active = false
    }
  }, [version, t])

  // Version selected: compact one-line confirmation.
  if (version) {
    return (
      <div className="flex flex-col gap-2" data-section="base-stix">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm">{t("baseStix.selected", { versao: version })}</p>
          {onVersionSelected ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onVersionSelected("")}
            >
              {t("baseStix.changeBase")}
            </Button>
          ) : null}
        </div>
        <CompletionLine checking={checking} loadError={loadError} result={result} />
        <PackageOverviewPanel
          loading={overviewLoading}
          error={overviewError}
          overview={overview}
        />
        <PackageAnalyticsPanel
          loading={analyticsLoading}
          error={analyticsError}
          analytics={analytics}
        />
      </div>
    )
  }

  // No version selected: the compact picker.
  return (
    <div data-section="base-stix">
      <StixBaseSelect onSelectBase={(v) => onVersionSelected?.(v)} />
    </div>
  )
}

/** A single-line completion/verification/error note for the selected version. */
function CompletionLine({
  checking,
  loadError,
  result,
}: {
  checking: boolean
  loadError: string | null
  result: BaseStixInfo | null
}) {
  const { t } = useTranslation("pipeline")

  const error = loadError
  if (error) {
    return (
      <p className="text-xs text-team-red" role="alert" data-section="completion">
        {error}
      </p>
    )
  }
  if (checking) {
    return (
      <p className="text-xs text-muted-foreground" role="status" data-section="completion">
        {t("baseStix.verifying")}
      </p>
    )
  }
  const completed = result ? (result.completed ?? result.downloaded) : false
  return (
    <p
      className={cn("text-xs", completed ? "text-team-blue" : "text-muted-foreground")}
      role="status"
      data-section="completion"
      data-completed={completed}
    >
      {completed ? t("baseStix.ready") : t("baseStix.waiting")}
    </p>
  )
}

/**
 * A compact package-overview panel for the selected version (Req. 6.4, 7.2):
 * the base identity, the UTC sync timestamp, the headline totals (campaigns,
 * actors, techniques, tactics, malware, relationships) and a curated/total
 * share, all read from the database via {@link getPackageOverview} (the
 * curated count comes from the analytics load). On a load failure it shows a
 * descriptive note and renders no partial data.
 */
function PackageOverviewPanel({
  loading,
  error,
  overview,
}: {
  loading: boolean
  error: string | null
  overview: StixPackageOverview | null
}) {
  const { t } = useTranslation("pipeline")

  if (error) {
    return (
      <p className="text-xs text-team-red" role="alert" data-section="overview">
        {error}
      </p>
    )
  }
  if (loading || !overview) {
    return (
      <p
        className="text-xs text-muted-foreground"
        role="status"
        data-section="overview"
      >
        {t("baseStix.overview.loading")}
      </p>
    )
  }

  const totals: { key: string; label: string; value: number }[] = [
    { key: "campaigns", label: t("baseStix.overview.campaigns"), value: overview.campaigns_total },
    { key: "actors", label: t("baseStix.overview.actors"), value: overview.actors_total },
    { key: "techniques", label: t("baseStix.overview.techniques"), value: overview.techniques_total },
    { key: "tactics", label: t("baseStix.overview.tactics"), value: overview.tactics_total },
    { key: "malware", label: t("baseStix.overview.malware"), value: overview.malware_total },
    { key: "relationships", label: t("baseStix.overview.relationships"), value: overview.relationships_total },
  ]

  return (
    <details
      className="group flex flex-col gap-2 rounded-md border bg-background p-3"
      data-section="overview"
      open
    >
      <summary className="flex cursor-pointer list-none items-center justify-between text-sm font-medium">
        <span>{t("baseStix.overview.title")}</span>
        <span className="text-xs text-muted-foreground transition-transform group-open:rotate-180" aria-hidden>
          ▾
        </span>
      </summary>
      <p className="text-xs text-muted-foreground">
        {overview.name} — v{overview.version}
      </p>
      {overview.downloaded_at ? (
        <p className="text-xs text-muted-foreground" data-field="synced-at">
          {t("baseStix.overview.syncedAt", {
            datetime: formatUtc(overview.downloaded_at),
          })}
        </p>
      ) : null}

      {/* Headline totals (Req. 6.4). */}
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {totals.map((total) => (
          <div
            key={total.key}
            className="flex flex-col rounded border px-2 py-1.5"
            data-total={total.key}
          >
            <dt className="text-xs text-muted-foreground">{total.label}</dt>
            <dd className="text-base font-semibold tabular-nums text-team-purple">
              {total.value}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  )
}

/**
 * Format an ISO-8601 timestamp as a readable UTC string, always in UTC so the
 * value is never shifted by the viewer's local timezone. The backend marks the
 * value with a `Z` (Zulu/UTC) designator; we render day/month/year with the
 * `UTC` suffix to make the zone explicit to the Researcher.
 */
function formatUtc(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const formatted = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    hour12: false,
  }).format(date)
  return `${formatted} UTC`
}

/**
 * A compact analytic dashboard for the selected version: two complementary
 * rankings read from the database via {@link getPackageAnalytics}.
 *
 * - **Techniques per campaign** — each campaign ranked by the number of ATT&CK
 *   techniques it uses (name + technique count with a proportional bar).
 * - **Campaigns per technique** — the inverse view: each ATT&CK technique
 *   ranked by the number of distinct campaigns that use it (technique id/name +
 *   campaign count with a proportional bar).
 *
 * The panel is fully isolated from {@link PackageOverviewPanel}: on a load
 * failure it shows a descriptive note and renders no partial data, while the
 * overview panel keeps rendering unaffected.
 */
function PackageAnalyticsPanel({
  loading,
  error,
  analytics,
}: {
  loading: boolean
  error: string | null
  analytics: StixPackageAnalytics | null
}) {
  const { t } = useTranslation("pipeline")

  if (error) {
    return (
      <p className="text-xs text-team-red" role="alert" data-section="analytics">
        {error}
      </p>
    )
  }
  if (loading || !analytics) {
    return (
      <p
        className="text-xs text-muted-foreground"
        role="status"
        data-section="analytics"
      >
        {t("baseStix.analytics.loading")}
      </p>
    )
  }

  // Proportional bar widths are scaled to the largest value in each ranking so
  // the leader fills the track and the rest scale relative to it.
  const maxCampaign = analytics.top_campaigns.reduce(
    (max, campaign) => Math.max(max, campaign.techniques_total),
    0,
  )
  const maxTechnique = analytics.top_techniques.reduce(
    (max, technique) => Math.max(max, technique.campaigns_total),
    0,
  )

  const barWidth = (value: number, max: number): string =>
    max > 0 ? `${Math.round((value / max) * 100)}%` : "0%"

  return (
    <details
      className="group flex flex-col gap-3 rounded-md border bg-background p-3"
      data-section="analytics"
      open
    >
      <summary className="flex cursor-pointer list-none items-center justify-between text-sm font-medium">
        <span>{t("baseStix.analytics.title")}</span>
        <span className="text-xs text-muted-foreground transition-transform group-open:rotate-180" aria-hidden>
          ▾
        </span>
      </summary>

      {/* Techniques per campaign: each campaign ranked by how many ATT&CK
          techniques it uses, with a proportional bar. */}
      <div className="flex flex-col gap-1.5" data-section="analytics-campaigns">
        <p className="text-xs font-medium text-muted-foreground">
          {t("baseStix.analytics.techniquesPerCampaign")}
        </p>
        {analytics.top_campaigns.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {analytics.top_campaigns.map((campaign) => (
              <li
                key={campaign.campaign_key}
                className="flex flex-col gap-0.5"
                data-campaign={campaign.campaign_key}
              >
                <div className="flex items-center justify-between gap-2 text-xs">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate">{campaign.name}</span>
                  </span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">
                    {campaign.techniques_total} {t("baseStix.analytics.techniquesUnit")}
                  </span>
                </div>
                <div className="h-1.5 w-full rounded-full bg-muted">
                  <div
                    className="h-1.5 rounded-full bg-team-purple"
                    style={{
                      width: barWidth(campaign.techniques_total, maxCampaign),
                    }}
                    data-bar="campaign"
                  />
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">
            {t("baseStix.analytics.techniquesPerCampaignEmpty")}
          </p>
        )}
      </div>

      {/* Campaigns per technique: the inverse view — each ATT&CK technique
          ranked by how many distinct campaigns use it, with a proportional bar. */}
      <div className="flex flex-col gap-1.5" data-section="analytics-techniques">
        <p className="text-xs font-medium text-muted-foreground">
          {t("baseStix.analytics.campaignsPerTechnique")}
        </p>
        {analytics.top_techniques.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {analytics.top_techniques.map((technique) => (
              <li
                key={technique.technique_id}
                className="flex flex-col gap-0.5"
                data-technique={technique.technique_id}
              >
                <div className="flex items-center justify-between gap-2 text-xs">
                  <span className="flex min-w-0 items-baseline gap-1.5">
                    <span className="font-medium">{technique.technique_id}</span>
                    {technique.technique_name ? (
                      <span className="truncate text-muted-foreground">
                        {technique.technique_name}
                      </span>
                    ) : null}
                  </span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">
                    {technique.campaigns_total} {t("baseStix.analytics.campaignsUnit")}
                  </span>
                </div>
                <div className="h-1.5 w-full rounded-full bg-muted">
                  <div
                    className="h-1.5 rounded-full bg-team-blue"
                    style={{
                      width: barWidth(technique.campaigns_total, maxTechnique),
                    }}
                    data-bar="technique"
                  />
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">
            {t("baseStix.analytics.campaignsPerTechniqueEmpty")}
          </p>
        )}
      </div>
    </details>
  )
}
