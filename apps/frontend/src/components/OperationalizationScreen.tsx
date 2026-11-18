import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, Copy } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useAsyncData } from "@/lib/use-async-data"
import {
  getOperationalization,
  type AtomicStatus,
  type OperationalizationListing,
  type OperationalizationTechnique,
} from "@/lib/operationalization"

/** Interval (ms) between rereads while the Atomic Red Team base is not ready. */
export const ATOMIC_POLL_INTERVAL_MS = 3000

/** Id of the screen title, referenced by the region landmark. */
const TITLE_ID = "operationalization-title"

/** How long (ms) the copy button keeps its "copied" feedback. */
const COPY_FEEDBACK_MS = 2000

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
  const request = useAsyncData<OperationalizationListing>(
    JSON.stringify([version, campaignKey]),
    () => getOperationalization(version, campaignKey),
  )
  const listing = request.data
  const loadError = request.error !== null
  const { mutate } = request
  // Bumped when a silent reread fails, so the poll below schedules a retry.
  const [retry, setRetry] = useState(0)

  const ready = listing?.atomic_status.status === "ready"

  // Silent polling until the base is ready; a failed reread keeps the last data.
  useEffect(() => {
    if (!listing || ready) return
    let active = true
    const timer = window.setTimeout(() => {
      getOperationalization(version, campaignKey)
        .then((data) => {
          if (active) mutate(data)
        })
        .catch(() => {
          // Keep the current listing; retry on the next tick.
          if (active) setRetry((n) => n + 1)
        })
    }, ATOMIC_POLL_INTERVAL_MS)
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [listing, ready, retry, version, campaignKey, mutate])

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
    <Card
      data-testid="operationalization-screen"
      role="region"
      aria-labelledby={TITLE_ID}
    >
      <CardHeader>
        <CardTitle id={TITLE_ID}>{t("operationalization.title")}</CardTitle>
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
              {listing.techniques.map((technique, index) => (
                <TechniqueItem
                  key={technique.technique_id}
                  technique={technique}
                  atomicReady={ready}
                  defaultOpen={index === 0}
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
  defaultOpen = false,
}: {
  technique: OperationalizationTechnique
  atomicReady: boolean
  defaultOpen?: boolean
}) {
  const { t } = useTranslation("pipeline")
  const name = technique.technique_name ?? t("operationalization.unnamedTechnique")
  const headingId = `operationalization-technique-${technique.technique_id}`
  const heading = (
    <div className="flex flex-wrap items-baseline gap-2">
      <h3 id={headingId} className="text-sm font-medium">
        <span className="font-mono">{technique.technique_id}</span> — {name}
      </h3>
      <span className="text-xs text-muted-foreground">
        {t("operationalization.commandCount", { count: technique.commands.length })}
      </span>
    </div>
  )

  return (
    <li
      className="flex flex-col gap-2 rounded-md border p-3"
      data-testid="operationalization-technique"
      data-technique={technique.technique_id}
      aria-labelledby={headingId}
    >
      {technique.commands.length === 0 ? (
        <>
          {heading}
          <p className="text-sm text-muted-foreground" data-testid="no-commands">
            {atomicReady
              ? t("operationalization.noCommands")
              : t("operationalization.noCommandsPending")}
          </p>
        </>
      ) : (
        <details open={defaultOpen} className="group" data-testid="technique-details">
          <summary className="flex cursor-pointer list-none items-center gap-2">
            <span
              className="text-xs text-muted-foreground transition-transform group-open:rotate-90"
              aria-hidden
            >
              ▸
            </span>
            {heading}
          </summary>
        <ul
          className="mt-2 flex flex-col gap-2"
          aria-label={t("operationalization.commandsLabel", { technique: technique.technique_id })}
        >
          {technique.commands.map((command, index) => (
            <li
              key={command.id ?? `${technique.technique_id}-${index}`}
              className="flex flex-col gap-1 rounded-md bg-muted/40 p-2"
            >
              {isExample(command) ? (
                <>
                  <div className="flex flex-col gap-0.5" data-testid="example-label">
                    <span className="text-xs font-medium">
                      {t("operationalization.exampleLabel")}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {t("operationalization.exampleHint")}
                    </span>
                  </div>
                  <CommandBlock
                    command={command.resolved_command as string}
                    technique={technique.technique_id}
                    example
                  />
                  <details className="text-xs" data-testid="template-details">
                    <summary className="cursor-pointer text-muted-foreground">
                      {t("operationalization.templateSummary")}
                    </summary>
                    <pre
                      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-focusable scrollable region so keyboard users can scroll long commands (WCAG 2.1.1)
                      tabIndex={0}
                      className="mt-1 overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs"
                    >
                      {command.command}
                    </pre>
                  </details>
                </>
              ) : (
                <CommandBlock
                  command={command.resolved_command || command.command}
                  technique={technique.technique_id}
                />
              )}
              {(command.unresolved_arguments?.length ?? 0) > 0 && (
                <p
                  role="status"
                  className="text-xs text-amber-700 dark:text-amber-400"
                  data-testid="unresolved-arguments"
                >
                  {t("operationalization.unresolvedArguments", {
                    names: command.unresolved_arguments?.join(", "),
                  })}
                </p>
              )}
              {Object.keys(command.arguments_used ?? {}).length > 0 && (
                <details className="text-xs" data-testid="arguments-used">
                  <summary className="cursor-pointer text-muted-foreground">
                    {t("operationalization.argumentsUsed")}
                  </summary>
                  <dl className="mt-1 flex flex-col gap-1">
                    {Object.entries(command.arguments_used ?? {}).map(([name, value]) => (
                      <div key={name} className="flex gap-2">
                        <dt className="font-mono">{name}:</dt>
                        <dd className="min-w-0 break-words font-mono">{value}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              )}
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
                  <dd>
                    {t(`operationalization.sourceName.${command.source}`, {
                      defaultValue: command.source,
                    })}
                  </dd>
                </div>
              </dl>
            </li>
          ))}
        </ul>
        </details>
      )}
    </li>
  )
}

/** True when the resolved example differs from the canonical template. */
function isExample(command: { command: string; resolved_command?: string }): boolean {
  return Boolean(command.resolved_command) && command.resolved_command !== command.command
}

/**
 * One command in a `<pre>` that wraps long lines, breaks long words and scrolls
 * horizontally when it still overflows, plus a copy-to-clipboard button
 * (Req. 10.7). The `<pre>` is focusable so keyboard users can scroll it.
 */
export function CommandBlock({
  command,
  technique,
  example = false,
}: {
  command: string
  technique: string
  /** True when `command` is an example with Atomic default values, not the template. */
  example?: boolean
}) {
  const { t } = useTranslation("pipeline")
  const [feedback, setFeedback] = useState<"copied" | "failed" | null>(null)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const copy = async () => {
    let next: "copied" | "failed" = "copied"
    try {
      await navigator.clipboard.writeText(command)
    } catch {
      next = "failed"
    }
    setFeedback(next)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setFeedback(null), COPY_FEEDBACK_MS)
  }

  return (
    <div className="flex items-start gap-2">
      <pre
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-focusable scrollable region so keyboard users can scroll long commands (WCAG 2.1.1)
        tabIndex={0}
        aria-label={t("operationalization.commandLabel")}
        className="min-w-0 flex-1 overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs"
      >
        {command}
      </pre>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => void copy()}
        aria-label={t(
          example ? "operationalization.copyExampleLabel" : "operationalization.copyLabel",
          { technique },
        )}
        data-testid="copy-command"
      >
        {feedback === "copied" ? (
          <Check className="size-4" aria-hidden />
        ) : (
          <Copy className="size-4" aria-hidden />
        )}
        <span aria-live="polite">
          {feedback === "copied"
            ? t("operationalization.copied")
            : feedback === "failed"
              ? t("operationalization.copyFailed")
              : t("operationalization.copy")}
        </span>
      </Button>
    </div>
  )
}
