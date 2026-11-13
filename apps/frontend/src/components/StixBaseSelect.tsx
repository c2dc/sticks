import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, ChevronDown, Download, Loader2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { ApiError } from "@/lib/api"
import { downloadBase, getBases, type BaseStixInfo } from "@/lib/stix"
import { cn } from "@/lib/utils"

export interface StixBaseSelectProps {
  /**
   * Called when the Researcher picks a downloaded base. Only downloaded
   * versions are selectable, so this fires exclusively for versions on disk.
   */
  onSelectBase: (version: string) => void
}

/**
 * StixBaseSelect — a compact, minimalist STIX base picker with a custom
 * dropdown so each version can carry an icon that makes its download state
 * obvious at a glance:
 *
 * - **Downloaded** versions show a check icon ("on disk").
 * - **Not downloaded** versions show a download icon.
 *
 * A native `<select>` can only render text in its options, so a small custom
 * menu is used instead. Clicking a **downloaded** version in the list fires
 * `onSelectBase` directly on the click — there is no separate "Select version"
 * button (Req. 7.1). Clicking a **not-yet-downloaded** version reveals an
 * inline Download action, keeping the download an explicit action (Req. 7.5).
 * The whole control stays on one compact row, keeping the Input screen simple.
 */
export function StixBaseSelect({ onSelectBase }: StixBaseSelectProps) {
  const { t } = useTranslation("stix")

  const [bases, setBases] = useState<BaseStixInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // The version highlighted in the dropdown (may not be on disk yet).
  const [candidate, setCandidate] = useState<string>("")
  const [open, setOpen] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)

  const rootRef = useRef<HTMLDivElement>(null)

  const load = useCallback(
    async (signal?: { cancelled: boolean }) => {
      try {
        const data = await getBases()
        if (signal?.cancelled) return
        setBases(data)
        setLoadError(null)
        // Do NOT pre-select any version: the dropdown starts empty and the
        // Researcher picks a version explicitly.
      } catch {
        if (signal?.cancelled) return
        setLoadError(t("stix.bases.loadError"))
      } finally {
        if (!signal?.cancelled) setLoading(false)
      }
    },
    [t],
  )

  useEffect(() => {
    const signal = { cancelled: false }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- resets the request state synchronously when the key changes, before the async fetch; deriving it during render would change the loading timing
    setLoading(true)
    void load(signal)
    return () => {
      signal.cancelled = true
    }
  }, [load])

  // Close the menu on an outside click.
  useEffect(() => {
    if (!open) return
    const onDocClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener("mousedown", onDocClick)
    return () => document.removeEventListener("mousedown", onDocClick)
  }, [open])

  const candidateInfo = useMemo(
    () => bases.find((b) => b.version === candidate) ?? null,
    [bases, candidate],
  )

  const handleDownload = useCallback(async () => {
    if (!candidate) return
    setDownloading(true)
    setDownloadError(null)
    try {
      await downloadBase(candidate)
      await load()
    } catch (error) {
      const message =
        error instanceof ApiError && error.message
          ? error.message
          : t("stix.bases.downloadError")
      setDownloadError(message)
    } finally {
      setDownloading(false)
    }
  }, [candidate, load, t])

  if (loading) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stix.bases.loading")}
      </p>
    )
  }
  if (loadError) {
    return <p className="text-sm text-team-red">{loadError}</p>
  }

  const candidateDownloaded = candidateInfo?.downloaded ?? false

  return (
    <div className="flex flex-col gap-2" data-section="stix-base-select">
      <div className="flex flex-wrap items-center gap-2">
        {/* Custom dropdown so each option can carry a download-state icon. */}
        <div ref={rootRef} className="relative">
          <button
            type="button"
            aria-label={t("stix.bases.title")}
            aria-haspopup="listbox"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="flex h-9 w-44 items-center justify-between gap-2 whitespace-nowrap rounded-md border bg-background px-2.5 text-sm outline-none focus:ring-1 focus:ring-team-blue"
          >
            {candidate ? (
              <span className="inline-flex items-center gap-1.5">
                <StateIcon downloaded={candidateDownloaded} />
                <span className="font-medium">{candidate}</span>
              </span>
            ) : (
              <span className="text-muted-foreground">{t("stix.bases.choose")}</span>
            )}
            <ChevronDown className="size-4 text-muted-foreground" aria-hidden />
          </button>

          {open ? (
            <ul
              role="listbox"
              aria-label={t("stix.bases.title")}
              className="absolute z-50 mt-1 max-h-72 w-full overflow-y-auto rounded-md border bg-popover p-1 shadow-lg"
            >
              {bases.map((base) => {
                const selected = base.version === candidate
                return (
                  <li key={base.version} role="option" aria-selected={selected}>
                    <button
                      type="button"
                      onClick={() => {
                        setDownloadError(null)
                        setOpen(false)
                        // A downloaded version is selected directly on click,
                        // with no separate "Select version" button (Req. 7.1).
                        // A not-yet-downloaded version only becomes the
                        // candidate, revealing the inline Download action
                        // (Req. 7.5).
                        if (base.downloaded) {
                          setCandidate(base.version)
                          onSelectBase(base.version)
                        } else {
                          setCandidate(base.version)
                        }
                      }}
                      className={cn(
                        "flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-sm transition-colors",
                        selected ? "bg-muted" : "hover:bg-muted/60",
                      )}
                    >
                      <StateIcon downloaded={base.downloaded} />
                      <span className="font-medium">{base.version}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          ) : null}
        </div>

        {/* Only a not-yet-downloaded candidate shows an action: the inline
            Download, kept as an explicit action (Req. 7.5). A downloaded
            version needs no button — it was already selected on click. */}
        {candidate && !candidateDownloaded ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => void handleDownload()}
            disabled={downloading}
          >
            {downloading ? (
              <Loader2 className="size-4 animate-spin" aria-hidden />
            ) : (
              <Download className="size-4" aria-hidden />
            )}
            {downloading ? t("stix.bases.downloading") : t("stix.bases.download")}
          </Button>
        ) : null}
      </div>

      {/* While a not-yet-downloaded version is being fetched, show an
          indeterminate progress bar so the download is visibly in flight. */}
      {downloading ? (
        <div
          className="h-1 w-44 overflow-hidden rounded-full bg-muted"
          role="progressbar"
          aria-label={t("stix.bases.downloading")}
        >
          <span className="block h-full w-1/2 animate-[loading_1.2s_ease-in-out_infinite] rounded-full bg-team-blue" />
        </div>
      ) : null}

      {candidateInfo ? (
        <p className="text-xs text-muted-foreground">{candidateInfo.name}</p>
      ) : null}
      {downloadError ? <p className="text-xs text-team-red">{downloadError}</p> : null}
    </div>
  )
}

/**
 * The per-version download-state icon: a check for a downloaded base, a download
 * icon for one still on the remote source. Makes the on-disk state obvious at a
 * glance (the user's request).
 */
function StateIcon({ downloaded }: { downloaded: boolean }) {
  const { t } = useTranslation("stix")
  return downloaded ? (
    <Check
      className="size-4 shrink-0 text-team-blue"
      aria-label={t("stix.bases.alreadyDownloaded")}
    />
  ) : (
    <Download
      className="size-4 shrink-0 text-muted-foreground"
      aria-label={t("stix.bases.download")}
    />
  )
}
