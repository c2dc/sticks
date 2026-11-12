import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import {
  getIngestionStatus,
  isIngestionInProgress,
  startIngestion,
  verifyIngestion,
  type IngestSource,
  type IngestionSourceStatus,
} from "@/lib/ingestion"

const POLL_INTERVAL_MS = 3000

/**
 * Non-blocking bar showing the ingestion state of each knowledge source.
 *
 * Calls `POST /api/ingestion/verify` on mount, then polls
 * `GET /api/ingestion/status` while any source is `pending` or `running`.
 * A failed source can be resumed with a button.
 */
export function IngestionStatusBar() {
  const { t } = useTranslation("common")
  const [statuses, setStatuses] = useState<IngestionSourceStatus[]>([])
  const [requestFailed, setRequestFailed] = useState(false)
  const [resuming, setResuming] = useState<IngestSource | null>(null)
  const mounted = useRef(true)

  const inProgress = isIngestionInProgress(statuses)

  useEffect(() => {
    mounted.current = true
    verifyIngestion()
      .then((items) => {
        if (!mounted.current) return
        setStatuses(items)
        setRequestFailed(false)
      })
      .catch(() => {
        if (mounted.current) setRequestFailed(true)
      })
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (!inProgress) return
    const timer = window.setInterval(() => {
      getIngestionStatus()
        .then((items) => {
          if (!mounted.current) return
          setStatuses(items)
          setRequestFailed(false)
        })
        .catch(() => {
          if (mounted.current) setRequestFailed(true)
        })
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [inProgress])

  const handleResume = useCallback(async (source: IngestSource) => {
    setResuming(source)
    try {
      const updated = await startIngestion(source)
      if (!mounted.current) return
      setStatuses((current) =>
        current.map((item) => (item.source === source ? updated : item)),
      )
      setRequestFailed(false)
    } catch {
      if (mounted.current) setRequestFailed(true)
    } finally {
      if (mounted.current) setResuming(null)
    }
  }, [])

  if (statuses.length === 0 && !requestFailed) return null

  return (
    <section
      aria-label={t("ingestion.title")}
      className="border-b bg-muted/40 text-sm"
    >
      <ul className="mx-auto flex max-w-6xl flex-wrap gap-x-6 gap-y-2 px-4 py-2">
        {statuses.map((item) => (
          <li
            key={item.source}
            className="flex flex-wrap items-center gap-2"
            data-source={item.source}
            data-status={item.status}
          >
            <span className="font-medium">{t(`ingestion.source.${item.source}`)}</span>
            <span
              role="status"
              className={
                item.status === "failed"
                  ? "text-destructive"
                  : item.status === "ready"
                    ? "text-green-600 dark:text-green-400"
                    : "text-muted-foreground"
              }
            >
              {t(`ingestion.status.${item.status}`)}
            </span>
            {item.expected_count !== null && (
              <span className="text-muted-foreground">
                {t("ingestion.counts", {
                  loaded: item.loaded_count ?? 0,
                  embedded: item.embedded_count ?? 0,
                  expected: item.expected_count,
                })}
              </span>
            )}
            {item.skipped_count > 0 && (
              <span className="text-muted-foreground">
                {t("ingestion.skipped", { count: item.skipped_count })}
              </span>
            )}
            {item.status === "failed" && (
              <>
                {item.error && (
                  <span className="text-destructive">
                    {t("ingestion.failure_cause", { error: item.error })}
                  </span>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={resuming === item.source}
                  onClick={() => void handleResume(item.source)}
                >
                  {resuming === item.source
                    ? t("ingestion.resuming")
                    : t("ingestion.resume")}
                </Button>
              </>
            )}
          </li>
        ))}
        {requestFailed && (
          <li className="text-destructive" role="alert">
            {t("ingestion.request_error")}
          </li>
        )}
      </ul>
    </section>
  )
}
