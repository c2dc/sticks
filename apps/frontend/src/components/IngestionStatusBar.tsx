import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import {
  Binary,
  CircleCheck,
  CircleX,
  Clock,
  Database,
  Loader2,
  SkipForward,
  Target,
} from "lucide-react"

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

/** Icon that stands for the state of one source; its label is the translated status. */
function StatusIcon({ status, label }: { status: IngestionSourceStatus["status"]; label: string }) {
  const common = { className: "size-3.5 shrink-0", "aria-hidden": true } as const
  const icon =
    status === "ready" ? (
      <CircleCheck {...common} className="size-3.5 shrink-0 text-green-600 dark:text-green-400" />
    ) : status === "failed" ? (
      <CircleX {...common} className="size-3.5 shrink-0 text-destructive" />
    ) : status === "running" ? (
      <Loader2 {...common} className="size-3.5 shrink-0 animate-spin text-muted-foreground" />
    ) : (
      <Clock {...common} className="size-3.5 shrink-0 text-muted-foreground" />
    )
  return (
    <span role="status" title={label} aria-label={label} className="inline-flex">
      {icon}
    </span>
  )
}

/** A count preceded by an icon; the label (shown on hover and read aloud) names the count. */
function Count({ icon, value, label }: { icon: ReactNode; value: number; label: string }) {
  return (
    <span
      title={label}
      aria-label={label}
      className="inline-flex items-center gap-0.5 tabular-nums text-muted-foreground"
    >
      {icon}
      <span aria-hidden>{value}</span>
    </span>
  )
}

const COUNT_ICON = "size-3 shrink-0"

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
      className="border-b bg-muted/40 text-[11px]"
    >
      <ul className="mx-auto flex max-w-6xl flex-nowrap items-center gap-x-3 overflow-x-auto whitespace-nowrap px-3 py-1.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {statuses.map((item) => (
          <li
            key={item.source}
            className="flex shrink-0 items-center gap-1"
            data-source={item.source}
            data-status={item.status}
          >
            <span className="font-medium">{t(`ingestion.source.${item.source}`)}</span>
            <StatusIcon status={item.status} label={t(`ingestion.status.${item.status}`)} />
            {item.loaded_count !== null && (
              <Count
                icon={<Database className={COUNT_ICON} aria-hidden />}
                value={item.loaded_count}
                label={t("ingestion.loaded", { count: item.loaded_count })}
              />
            )}
            {item.embedded_count !== null && (
              <Count
                icon={<Binary className={COUNT_ICON} aria-hidden />}
                value={item.embedded_count}
                label={t("ingestion.embedded", { count: item.embedded_count })}
              />
            )}
            {item.expected_count !== null && (
              <Count
                icon={<Target className={COUNT_ICON} aria-hidden />}
                value={item.expected_count}
                label={t("ingestion.expected", { count: item.expected_count })}
              />
            )}
            {item.skipped_count > 0 && (
              <Count
                icon={<SkipForward className={COUNT_ICON} aria-hidden />}
                value={item.skipped_count}
                label={t("ingestion.skipped", { count: item.skipped_count })}
              />
            )}
            {item.status === "failed" && (
              <>
                {item.error && (
                  <span
                    className="max-w-64 truncate text-destructive"
                    title={t("ingestion.failure_cause", { error: item.error })}
                  >
                    {t("ingestion.failure_cause", { error: item.error })}
                  </span>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  className="h-6 px-2 text-xs"
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
