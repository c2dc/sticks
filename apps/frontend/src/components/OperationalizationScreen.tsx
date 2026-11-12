import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  getOperationalization,
  type AtomicStatus,
  type OperationalizationListing,
  type OperationalizationTechnique,
} from "@/lib/operationalization"

/** Interval (ms) between rereads while the Atomic Red Team base is not ready. */
export const ATOMIC_POLL_INTERVAL_MS = 3000

/**
 * OperationalizationScreen — for EACH technique of the selected campaign, lists
 * the candidate Atomic Red Team commands (Req. 6.3, 6.4, 6.5).
 *
 * - A technique without any command gets an explicit message (never omitted).
 * - While `atomic_status.status !== "ready"` a warning with a progress bar is
 *   shown and the listing is reread every 3 s, with no user action, until the
 *   base becomes `ready`.
 * - Read-only; works from the chosen STIX `version` and `campaignKey`.
 */
export function OperationalizationScreen({
  version,
  campaignKey,
}: {
  version: string
  campaignKey: string
}) {
  const { t } = useTranslation("pipeline")
  const [listing, setListing] = useState<OperationalizationListing | null>(null)
  const [loadError, setLoadError] = useState(false)

  const ready = listing?.atomic_status.status === "ready"

  // Initial load, restarted whenever the version or campaign changes.
  useEffect(() => {
    let active = true
    setListing(null)
    setLoadError(false)
    getOperationalization(version, campaignKey)
      .then((data) => {
        if (active) setListing(data)
      })
      .catch(() => {
        if (active) setLoadError(true)
      })
    return () => {
      active = false
    }
  }, [version, campaignKey])

  // Silent polling until the base is ready; a failed reread keeps the last data.
  useEffect(() => {
    if (!listing || ready) return
    let active = true
    const timer = window.setTimeout(() => {
      getOperationalization(version, campaignKey)
        .then((data) => {
          if (active) setListing(data)
        })
        .catch(() => {
          // Keep the current listing; retry on the next state change.
          if (active) setListing((current) => (current ? { ...current } : current))
        })
    }, ATOMIC_POLL_INTERVAL_MS)
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [listing, ready, version, campaignKey])

  if (loadError) {
    return (
      <p className="text-sm text-muted-foreground" role="alert">
        {t("operationalization.loadError")}
      </p>
    )
  }
  if (!listing) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("operationalization.loading")}
      </p>
    )
  }

  return (
    <Card data-testid="operationalization-screen">
      <CardHeader>
        <CardTitle>{t("operationalization.title")}</CardTitle>
        <CardDescription>{t("operationalization.description")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!ready ? <AtomicNotice status={listing.atomic_status} /> : null}

        {listing.techniques.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {t("operationalization.noTechniques")}
          </p>
        ) : (
          <>
            <span className="text-xs text-muted-foreground" aria-live="polite">
              {t("operationalization.techniqueCount", { count: listing.techniques.length })}
            </span>
            <ul
              className="flex flex-col gap-3"
              aria-label={t("operationalization.techniquesLabel")}
            >
              {listing.techniques.map((technique) => (
                <TechniqueItem
                  key={technique.technique_id}
                  technique={technique}
                  atomicReady={ready}
                />
              ))}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  )
}

/** Warning + progress bar shown while the Atomic Red Team base is not ready. */
function AtomicNotice({ status }: { status: AtomicStatus }) {
  const { t } = useTranslation("pipeline")
  const { loaded_count: loaded, expected_count: expected } = status
  const known = loaded !== null && expected !== null && expected > 0
  const percent = known ? Math.min(100, Math.round((loaded / expected) * 100)) : null
  const failed = status.status === "failed"

  return (
    <div
      role="status"
      data-testid="atomic-notice"
      className="flex flex-col gap-2 rounded-md border border-amber-500/60 bg-amber-500/10 p-3 text-sm"
    >
      <p className="font-medium">{t("operationalization.atomicNotice.title")}</p>
      <p className="text-muted-foreground">
        {failed
          ? t("operationalization.atomicNotice.failed")
          : known
            ? t("operationalization.atomicNotice.progress", { loaded, expected })
            : t("operationalization.atomicNotice.progressUnknown")}
      </p>
      <div
        role="progressbar"
        aria-label={t("operationalization.atomicNotice.progressLabel")}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent ?? undefined}
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className="h-full bg-team-blue transition-all"
          style={{ width: `${percent ?? 0}%` }}
        />
      </div>
      {!failed ? (
        <p className="text-xs text-muted-foreground">
          {t("operationalization.atomicNotice.refreshing")}
        </p>
      ) : null}
    </div>
  )
}

/** One technique with its commands, or an explicit message when it has none. */
function TechniqueItem({
  technique,
  atomicReady,
}: {
  technique: OperationalizationTechnique
  atomicReady: boolean
}) {
  const { t } = useTranslation("pipeline")
  const name = technique.technique_name ?? t("operationalization.unnamedTechnique")
  const headingId = `operationalization-technique-${technique.technique_id}`

  return (
    <li
      className="flex flex-col gap-2 rounded-md border p-3"
      data-testid="operationalization-technique"
      data-technique={technique.technique_id}
      aria-labelledby={headingId}
    >
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 id={headingId} className="text-sm font-medium">
          <span className="font-mono">{technique.technique_id}</span> — {name}
        </h3>
        <span className="text-xs text-muted-foreground">
          {t("operationalization.commandCount", { count: technique.commands.length })}
        </span>
      </div>

      {technique.commands.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="no-commands">
          {atomicReady
            ? t("operationalization.noCommands")
            : t("operationalization.noCommandsPending")}
        </p>
      ) : (
        <ul
          className="flex flex-col gap-2"
          aria-label={t("operationalization.commandsLabel", { technique: technique.technique_id })}
        >
          {technique.commands.map((command, index) => (
            <li
              key={command.id ?? `${technique.technique_id}-${index}`}
              className="flex flex-col gap-1 rounded-md bg-muted/40 p-2"
            >
              <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs">
                {command.command}
              </pre>
              <dl className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <div className="flex gap-1">
                  <dt>{t("operationalization.executor")}:</dt>
                  <dd>{command.executor}</dd>
                </div>
                <div className="flex gap-1">
                  <dt>{t("operationalization.platform")}:</dt>
                  <dd>{command.platform}</dd>
                </div>
                <div className="flex gap-1">
                  <dt>{t("operationalization.source")}:</dt>
                  <dd>{command.source}</dd>
                </div>
              </dl>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}
