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
import { baixarBase, getBases, type BaseStixInfo } from "@/lib/stix"
import { cn } from "@/lib/utils"

export interface StixBasesListProps {
  /**
   * Called when the researcher picks a downloaded base. Only downloaded bases
   * are selectable (Req. 2.3), so this fires exclusively for `baixada` bases.
   */
  onSelecionarBase: (versao: string) => void
}

/**
 * STIX bases listing (Req. 2). Loads the known bases from `GET /api/stix/bases`
 * on mount and renders them in the order returned by the backend — newest to
 * oldest (Req. 2.1); the list is intentionally *not* re-sorted here.
 *
 * Each base card shows its version and name. Downloaded bases (`baixada`) carry
 * a "já baixada" badge and are selectable, firing {@link StixBasesListProps.onSelecionarBase}
 * (Req. 2.2, 2.3). Bases not yet on disk expose a "Baixar" button that calls
 * {@link baixarBase}; while the request is in flight the row shows a per-row
 * loading state (Req. 2.5, 2.6). On success the list is re-fetched so the row
 * flips to downloaded; on failure a descriptive, per-row error is shown and the
 * base stays not-downloaded (Req. 2.7, 2.8).
 */
export function StixBasesList({ onSelecionarBase }: StixBasesListProps) {
  const { t } = useTranslation("stix")

  const [bases, setBases] = useState<BaseStixInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Per-version download state, keyed by `versao`.
  const [baixando, setBaixando] = useState<Record<string, boolean>>({})
  const [erroDownload, setErroDownload] = useState<Record<string, string>>({})

  const carregar = useCallback(
    async (signal?: { cancelado: boolean }) => {
      try {
        const data = await getBases()
        if (signal?.cancelado) return
        // Render in the backend-provided order (newest → oldest); do not re-sort.
        setBases(data)
        setLoadError(null)
      } catch {
        if (signal?.cancelado) return
        setLoadError(t("stix.bases.erroCarregar"))
      } finally {
        if (!signal?.cancelado) setLoading(false)
      }
    },
    [t],
  )

  useEffect(() => {
    const signal = { cancelado: false }
    setLoading(true)
    void carregar(signal)
    return () => {
      signal.cancelado = true
    }
  }, [carregar])

  const handleBaixar = useCallback(
    async (versao: string) => {
      setBaixando((prev) => ({ ...prev, [versao]: true }))
      setErroDownload((prev) => {
        const next = { ...prev }
        delete next[versao]
        return next
      })
      try {
        await baixarBase(versao)
        // Re-fetch so the row flips to `baixada` (Req. 2.6).
        await carregar()
      } catch (error) {
        const mensagem =
          error instanceof ApiError && error.message
            ? error.message
            : t("stix.bases.erroDownload")
        // Keep the base not-downloaded and surface a descriptive error (Req. 2.7).
        setErroDownload((prev) => ({ ...prev, [versao]: mensagem }))
      } finally {
        setBaixando((prev) => {
          const next = { ...prev }
          delete next[versao]
          return next
        })
      }
    },
    [carregar, t],
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
              key={base.versao}
              base={base}
              baixando={Boolean(baixando[base.versao])}
              erro={erroDownload[base.versao] ?? null}
              onSelecionar={() => onSelecionarBase(base.versao)}
              onBaixar={() => void handleBaixar(base.versao)}
            />
          ))}
        </div>
      ) : null}
    </section>
  )
}

function BaseCard({
  base,
  baixando,
  erro,
  onSelecionar,
  onBaixar,
}: {
  base: BaseStixInfo
  baixando: boolean
  erro: string | null
  onSelecionar: () => void
  onBaixar: () => void
}) {
  const { t } = useTranslation("stix")

  // Only downloaded bases are selectable (Req. 2.3). The whole card is clickable
  // in that case, mirroring the explicit "Selecionar" button.
  const selecionavel = base.baixada

  return (
    <Card
      className={cn(
        "flex flex-col transition-colors",
        selecionavel &&
          "cursor-pointer hover:border-team-blue focus-within:border-team-blue",
      )}
      data-versao={base.versao}
      data-baixada={base.baixada}
      onClick={selecionavel ? onSelecionar : undefined}
      role={selecionavel ? "button" : undefined}
      tabIndex={selecionavel ? 0 : undefined}
      onKeyDown={
        selecionavel
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault()
                onSelecionar()
              }
            }
          : undefined
      }
    >
      <CardHeader className="gap-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base">{base.versao}</CardTitle>
          {base.baixada ? (
            <span className="inline-flex items-center rounded-full border border-team-blue/50 bg-team-blue/10 px-2 py-0.5 text-xs font-medium text-team-blue">
              {t("stix.bases.jaBaixada")}
            </span>
          ) : null}
        </div>
        <CardDescription>{base.nome}</CardDescription>
      </CardHeader>

      <CardContent className="mt-auto flex flex-col gap-2">
        {erro ? <p className="text-sm text-team-red">{erro}</p> : null}

        {base.baixada ? (
          <Button
            variant="default"
            size="sm"
            onClick={(event) => {
              // The card itself handles selection; stop the bubble so we don't
              // fire the handler twice.
              event.stopPropagation()
              onSelecionar()
            }}
          >
            {t("stix.bases.selecionar")}
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            disabled={baixando}
            onClick={(event) => {
              event.stopPropagation()
              onBaixar()
            }}
          >
            {baixando ? t("stix.bases.baixando") : t("stix.bases.baixar")}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}
