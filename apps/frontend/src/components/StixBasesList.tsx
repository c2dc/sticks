import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ApiError } from "@/lib/api"
import { downloadBase, getBases, type BaseStixInfo } from "@/lib/stix"
import { cn } from "@/lib/utils"

export interface StixBasesListProps {
  /**
   * Called when the researcher picks a downloaded base. Only downloaded bases
   * are selectable (Req. 2.3), so this fires exclusively for downloaded bases.
   */
  onSelectBase: (version: string) => void
}

/**
 * STIX bases listing (Req. 2). Loads the known bases from `GET /api/stix/bases`
 * on mount and renders them in the order returned by the backend — newest to
 * oldest (Req. 2.1); the list is intentionally *not* re-sorted here.
 *
 * Each base card shows its version and name. Downloaded bases carry a
 * "already downloaded" badge and are selectable, firing
 * {@link StixBasesListProps.onSelectBase} (Req. 2.2, 2.3). Bases not yet on disk
 * expose a "Download" button that calls {@link downloadBase}; while the request
 * is in flight the row shows a per-row loading state (Req. 2.5, 2.6). On success
 * the list is re-fetched so the row flips to downloaded; on failure a
 * descriptive, per-row error is shown and the base stays not-downloaded
 * (Req. 2.7, 2.8).
 */
export function StixBasesList({ onSelectBase }: StixBasesListProps) {
  const { t } = useTranslation("stix")

  const [bases, setBases] = useState<BaseStixInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Per-version download state, keyed by version.
  const [downloading, setDownloading] = useState<Record<string, boolean>>({})
  const [downloadError, setDownloadError] = useState<Record<string, string>>({})

  const load = useCallback(
    async (signal?: { cancelled: boolean }) => {
      try {
        const data = await getBases()
        if (signal?.cancelled) return
        // Render in the backend-provided order (newest → oldest); do not re-sort.
        setBases(data)
        setLoadError(null)
      } catch {
        if (signal?.cancelled) return
        setLoadError(t("stix.bases.erroCarregar"))
      } finally {
        if (!signal?.cancelled) setLoading(false)
      }
    },
    [t],
  )

  useEffect(() => {
    const signal = { cancelled: false }
    setLoading(true)
    void load(signal)
    return () => {
      signal.cancelled = true
    }
  }, [load])

  const handleDownload = useCallback(
    async (version: string) => {
      setDownloading((prev) => ({ ...prev, [version]: true }))
      setDownloadError((prev) => {
        const next = { ...prev }
        delete next[version]
        return next
      })
      try {
        await downloadBase(version)
        // Re-fetch so the row flips to downloaded (Req. 2.6).
        await load()
      } catch (error) {
        const message =
          error instanceof ApiError && error.message
            ? error.message
            : t("stix.bases.erroDownload")
        // Keep the base not-downloaded and surface a descriptive error (Req. 2.7).
        setDownloadError((prev) => ({ ...prev, [version]: message }))
      } finally {
        setDownloading((prev) => {
          const next = { ...prev }
          delete next[version]
          return next
        })
      }
    },
    [load, t],
  )

  return (
    <section className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h2 className="text-xl font-semibold">{t("stix.bases.titulo")}</h2>
      </div>

      {loading ? (
        <p className="text-sm text-muted-foreground">{t("stix.bases.carregando")}</p>
      ) : null}

      {loadError ? (
        <Card className="border-team-red/50 bg-team-red/10">
          <CardContent className="pt-6 text-sm text-team-red">{loadError}</CardContent>
        </Card>
      ) : null}

      {!loading && !loadError ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {bases.map((base) => (
            <BaseCard
              key={base.version}
              base={base}
              downloading={Boolean(downloading[base.version])}
              error={downloadError[base.version] ?? null}
              onSelect={() => onSelectBase(base.version)}
              onDownload={() => void handleDownload(base.version)}
            />
          ))}
        </div>
      ) : null}
    </section>
  )
}

function BaseCard({
  base,
  downloading,
  error,
  onSelect,
  onDownload,
}: {
  base: BaseStixInfo
  downloading: boolean
  error: string | null
  onSelect: () => void
  onDownload: () => void
}) {
  const { t } = useTranslation("stix")

  // Only downloaded bases are selectable (Req. 2.3). The whole card is clickable
  // in that case, mirroring the explicit "Select" button.
  const selectable = base.downloaded

  return (
    <Card
      className={cn(
        "flex flex-col transition-colors",
        selectable &&
          "cursor-pointer hover:border-team-blue focus-within:border-team-blue",
      )}
      data-versao={base.version}
      data-baixada={base.downloaded}
      onClick={selectable ? onSelect : undefined}
      role={selectable ? "button" : undefined}
      tabIndex={selectable ? 0 : undefined}
      onKeyDown={
        selectable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault()
                onSelect()
              }
            }
          : undefined
      }
    >
      <CardHeader className="gap-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base">{base.version}</CardTitle>
          {base.downloaded ? (
            <span className="inline-flex items-center rounded-full border border-team-blue/50 bg-team-blue/10 px-2 py-0.5 text-xs font-medium text-team-blue">
              {t("stix.bases.jaBaixada")}
            </span>
          ) : null}
        </div>
        <CardDescription>{base.name}</CardDescription>
      </CardHeader>

      <CardContent className="mt-auto flex flex-col gap-2">
        {error ? <p className="text-sm text-team-red">{error}</p> : null}

        {base.downloaded ? (
          <Button
            variant="default"
            size="sm"
            onClick={(event) => {
              // The card itself handles selection; stop the bubble so we don't
              // fire the handler twice.
              event.stopPropagation()
              onSelect()
            }}
          >
            {t("stix.bases.selecionar")}
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            disabled={downloading}
            onClick={(event) => {
              event.stopPropagation()
              onDownload()
            }}
          >
            {downloading ? t("stix.bases.baixando") : t("stix.bases.baixar")}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}
