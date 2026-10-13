import * as React from "react"
import { useTranslation } from "react-i18next"
import { Search, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { cn } from "@/lib/utils"
import {
  getAtores,
  getCampanhas,
  type AtorStix,
  type CampanhaStix,
} from "@/lib/stix"

/**
 * Campaign/Actor selector (CampaignActorSelector) — Task 15.1, Req. 4.1–4.5,
 * plus a frontend-only search/filter layer so the researcher can quickly narrow
 * a large STIX base down to what they want to emulate:
 *
 * - a **text search** that matches a campaign's name and description (and, in
 *   the actor view, the actor's name too), case/diacritics-insensitive;
 * - a **"curated only"** toggle that keeps only campaigns backed by a curated
 *   case (`curada` with a `slug_curado`);
 * - a **result count** ("X of Y campaigns") reflecting the active filters.
 *
 * The search/filter state is shared across both views, so toggling between
 * "Por campanha" and "Por ator de ameaça" keeps the same query applied. All
 * filtering is pure and client-side over the already-fetched lists — no extra
 * network calls. The two views still lazy-load their data the first time they
 * become active and cache it (unchanged from before).
 *
 * The underlying contract is unchanged (Req. 4.1–4.5):
 *
 * - **Por campanha**: lists the base's campaigns (`getCampanhas`, Req. 4.2);
 *   campaigns already worked by a colleague — `curada` with `slug_curado` — get
 *   a visible "já curada" badge (Req. 4.5).
 * - **Por ator de ameaça**: lists the intrusion-sets (`getAtores`, Req. 4.3),
 *   each with its attributed campaigns nested underneath, carrying the same
 *   "já curada" badge. The backend may surface a synthetic actor
 *   ("sem ator associado"); it is rendered like any other actor group.
 *
 * Selecting a campaign in either view calls {@link onSelecionarCampanha} with
 * its `stix_id`, `nome` and `slug_curado` (Req. 4.4) — the host advances the
 * flow to the DAG phase.
 */
export interface CampaignActorSelectorProps {
  /** The downloaded STIX base version whose campaigns/actors to list. */
  versao: string
  /**
   * Called when the researcher selects a campaign in either view (Req. 4.4).
   * Carries the minimal identity the DAG phase needs.
   */
  onSelecionarCampanha: (campanha: {
    stix_id: string
    nome: string
    slug_curado?: string | null
  }) => void
  className?: string
}

type Vista = "campanha" | "ator"

type AsyncState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string }

/** Normalize text for accent/case-insensitive matching. */
function normalizar(texto: string): string {
  return texto
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim()
}

/**
 * The `stix_id` of the backend's synthetic group that holds campaigns with no
 * `attributed-to` relationship (`StixService.ATOR_SEM_ASSOCIACAO_ID`). It is a
 * presentational bucket, NOT a real threat actor, so it is excluded from the
 * actor count (its own name says it has no actor).
 */
export const ATOR_SEM_ASSOCIACAO_ID = "sem-ator"

/** True when an actor group is the synthetic "no associated actor" bucket. */
export function isSemAtor(ator: { stix_id: string }): boolean {
  return ator.stix_id === ATOR_SEM_ASSOCIACAO_ID
}

/** True when a campaign is backed by a curated case (Req. 4.5). */
export function isCurada(campanha: CampanhaStix): boolean {
  return campanha.curada && Boolean(campanha.slug_curado)
}

/**
 * Pure client-side filter: keeps campaigns whose name or description matches the
 * (normalized) search term, optionally restricted to curated campaigns. An
 * empty search term matches everything. Exported for unit testing.
 */
export function filtrarCampanhas(
  campanhas: CampanhaStix[],
  busca: string,
  soCuradas: boolean,
): CampanhaStix[] {
  const termo = normalizar(busca)
  return campanhas.filter((c) => {
    if (soCuradas && !isCurada(c)) return false
    if (termo === "") return true
    const alvo = normalizar(`${c.nome} ${c.descricao ?? ""}`)
    return alvo.includes(termo)
  })
}

export function CampaignActorSelector({
  versao,
  onSelecionarCampanha,
  className,
}: CampaignActorSelectorProps) {
  const { t } = useTranslation("stix")

  // Default to the campaign view (Req. 4.1).
  const [vista, setVista] = React.useState<Vista>("campanha")

  // Shared search/filter state, applied to both views.
  const [busca, setBusca] = React.useState("")
  const [soCuradas, setSoCuradas] = React.useState(false)

  const [campanhas, setCampanhas] = React.useState<AsyncState<CampanhaStix[]>>({
    status: "idle",
  })
  const [atores, setAtores] = React.useState<AsyncState<AtorStix[]>>({
    status: "idle",
  })

  // Lazy-load the campaigns the first time the campaign view is shown.
  const carregarCampanhas = React.useCallback(() => {
    let cancelled = false
    setCampanhas({ status: "loading" })
    getCampanhas(versao)
      .then((data) => {
        if (!cancelled) setCampanhas({ status: "ready", data })
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message =
            err instanceof Error ? err.message : t("stix.selecao.erro")
          setCampanhas({ status: "error", message })
        }
      })
    return () => {
      cancelled = true
    }
  }, [versao, t])

  // Lazy-load the actors the first time the actor view is shown.
  const carregarAtores = React.useCallback(() => {
    let cancelled = false
    setAtores({ status: "loading" })
    getAtores(versao)
      .then((data) => {
        if (!cancelled) setAtores({ status: "ready", data })
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message =
            err instanceof Error ? err.message : t("stix.selecao.erro")
          setAtores({ status: "error", message })
        }
      })
    return () => {
      cancelled = true
    }
  }, [versao, t])

  // Fetch per active view, only when it has not been fetched yet. Changing
  // `versao` resets both caches below, so the active view refetches for it.
  //
  // The `*.status` values are read but intentionally kept out of the dependency
  // array: including them makes this effect re-run on the very idle→loading
  // transition it just triggered, and the cleanup it returns would then cancel
  // the in-flight request before it resolves (leaving the view stuck on
  // "loading" when toggling). The effect reruns on `vista`/`versao` changes
  // (the loader callbacks close over `versao`), which is exactly when a (re)fetch
  // is wanted; the inner `idle` guard still prevents duplicate fetches.
  React.useEffect(() => {
    if (vista === "campanha" && campanhas.status === "idle") {
      return carregarCampanhas()
    }
    if (vista === "ator" && atores.status === "idle") {
      return carregarAtores()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vista, carregarCampanhas, carregarAtores])

  // A new base invalidates both caches; the active view refetches via the
  // effect above once its state is back to `idle`.
  React.useEffect(() => {
    setCampanhas({ status: "idle" })
    setAtores({ status: "idle" })
  }, [versao])

  return (
    <section
      className={cn("flex flex-col gap-4", className)}
      aria-label={t("stix.selecao.titulo")}
    >
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold tracking-tight">
          {t("stix.selecao.titulo")}
        </h2>
        <p className="text-sm text-muted-foreground">
          {t("stix.selecao.subtitulo")}
        </p>
      </header>

      {/* Toggle between the campaign and actor views (Req. 4.1). */}
      <div
        className="inline-flex w-fit rounded-md border bg-muted p-0.5"
        role="tablist"
        aria-label={t("stix.selecao.alternar")}
      >
        <ToggleButton
          active={vista === "campanha"}
          onClick={() => setVista("campanha")}
        >
          {t("stix.selecao.porCampanha")}
        </ToggleButton>
        <ToggleButton
          active={vista === "ator"}
          onClick={() => setVista("ator")}
        >
          {t("stix.selecao.porAtor")}
        </ToggleButton>
      </div>

      {/* Search + "curated only" filter, shared across both views. */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <input
            type="search"
            value={busca}
            onChange={(e) => setBusca(e.target.value)}
            placeholder={t("stix.selecao.buscarPlaceholder")}
            aria-label={t("stix.selecao.buscar")}
            className={cn(
              "h-9 w-full rounded-md border border-input bg-background pl-8 pr-8 text-sm",
              "ring-offset-background placeholder:text-muted-foreground",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            )}
          />
          {busca !== "" ? (
            <button
              type="button"
              onClick={() => setBusca("")}
              aria-label={t("stix.selecao.limparBusca")}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground hover:text-foreground"
            >
              <X className="size-4" aria-hidden />
            </button>
          ) : null}
        </div>

        <label className="inline-flex cursor-pointer select-none items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={soCuradas}
            onChange={(e) => setSoCuradas(e.target.checked)}
            className="size-4 rounded border-input accent-team-purple"
          />
          {t("stix.selecao.soCuradas")}
        </label>
      </div>

      {vista === "campanha" ? (
        <CampanhasView
          state={campanhas}
          busca={busca}
          soCuradas={soCuradas}
          onRetry={carregarCampanhas}
          onSelecionarCampanha={onSelecionarCampanha}
        />
      ) : (
        <AtoresView
          state={atores}
          busca={busca}
          soCuradas={soCuradas}
          onRetry={carregarAtores}
          onSelecionarCampanha={onSelecionarCampanha}
        />
      )}
    </section>
  )
}

function ToggleButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "rounded-[0.3rem] px-3 py-1.5 text-sm font-medium transition-colors",
        active
          ? "bg-background text-foreground shadow-sm"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  )
}

type SelecionarCampanha = CampaignActorSelectorProps["onSelecionarCampanha"]

/** A small "N of M" result counter reflecting the active filters. The i18n key
 *  decides the counted noun (campaigns in the campaign view, actors in the
 *  actor view). */
function ResultCount({
  count,
  total,
  i18nKey = "stix.selecao.resultados",
}: {
  count: number
  total: number
  i18nKey?: string
}) {
  const { t } = useTranslation("stix")
  return (
    <p className="text-xs text-muted-foreground" role="status">
      {t(i18nKey, { count, total })}
    </p>
  )
}

/** Campaign view: a flat list of the base's campaigns (Req. 4.2, 4.4, 4.5). */
function CampanhasView({
  state,
  busca,
  soCuradas,
  onRetry,
  onSelecionarCampanha,
}: {
  state: AsyncState<CampanhaStix[]>
  busca: string
  soCuradas: boolean
  onRetry: () => void
  onSelecionarCampanha: SelecionarCampanha
}) {
  const { t } = useTranslation("stix")

  if (state.status === "idle" || state.status === "loading") {
    return <LoadingLine>{t("stix.selecao.carregando")}</LoadingLine>
  }
  if (state.status === "error") {
    return <ErrorBlock message={state.message} onRetry={onRetry} />
  }
  if (state.data.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("stix.selecao.semCampanhas")}
      </p>
    )
  }

  const filtradas = filtrarCampanhas(state.data, busca, soCuradas)

  return (
    <div className="flex flex-col gap-3">
      <ResultCount count={filtradas.length} total={state.data.length} />
      {filtradas.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {t("stix.selecao.semResultados")}
        </p>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2">
          {filtradas.map((campanha) => (
            <li key={campanha.stix_id}>
              <CampanhaCard
                campanha={campanha}
                onSelecionarCampanha={onSelecionarCampanha}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * Actor view: intrusion-sets, each with its attributed campaigns nested
 * underneath (Req. 4.3). The synthetic "sem ator associado" group is rendered
 * like any other actor. Actors are matched when their own name matches the
 * search OR they have at least one campaign surviving the filter; actors left
 * with no campaign after filtering are hidden.
 */
function AtoresView({
  state,
  busca,
  soCuradas,
  onRetry,
  onSelecionarCampanha,
}: {
  state: AsyncState<AtorStix[]>
  busca: string
  soCuradas: boolean
  onRetry: () => void
  onSelecionarCampanha: SelecionarCampanha
}) {
  const { t } = useTranslation("stix")

  if (state.status === "idle" || state.status === "loading") {
    return <LoadingLine>{t("stix.selecao.carregando")}</LoadingLine>
  }
  if (state.status === "error") {
    return <ErrorBlock message={state.message} onRetry={onRetry} />
  }
  if (state.data.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("stix.selecao.semAtores")}
      </p>
    )
  }

  const termo = normalizar(busca)
  // The actor view counts ACTORS (intrusion-sets), not campaigns — that is the
  // unit the researcher is browsing here. The synthetic "sem ator associado"
  // group is a presentational bucket, not a real actor, so it is NOT counted
  // (its own name says it has no actor). It still renders in the list below.
  const totalAtores = state.data.filter((ator) => !isSemAtor(ator)).length

  // Filter each actor's campaigns. When the actor's own name matches the search
  // term, keep all of its (curated-filtered) campaigns so the match is visible;
  // otherwise keep only the campaigns matching name/description.
  const atoresFiltrados = state.data
    .map((ator) => {
      const atorMatch = termo !== "" && normalizar(ator.nome).includes(termo)
      const campanhas = atorMatch
        ? filtrarCampanhas(ator.campanhas, "", soCuradas)
        : filtrarCampanhas(ator.campanhas, busca, soCuradas)
      return { ator, campanhas }
    })
    .filter((grupo) => grupo.campanhas.length > 0)

  // Actors surviving the filter (those with at least one matching campaign),
  // again excluding the synthetic "sem ator associado" bucket from the count.
  const countAtores = atoresFiltrados.filter(
    (grupo) => !isSemAtor(grupo.ator),
  ).length

  return (
    <div className="flex flex-col gap-3">
      <ResultCount
        count={countAtores}
        total={totalAtores}
        i18nKey="stix.selecao.resultadosAtores"
      />
      {atoresFiltrados.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {t("stix.selecao.semResultadosAtores")}
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {atoresFiltrados.map(({ ator, campanhas }) => (
            <li key={ator.stix_id}>
              <AtorCard
                ator={ator}
                campanhas={campanhas}
                onSelecionarCampanha={onSelecionarCampanha}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** A single campaign card, selectable, with the curated badge (Req. 4.4, 4.5). */
function CampanhaCard({
  campanha,
  onSelecionarCampanha,
}: {
  campanha: CampanhaStix
  onSelecionarCampanha: SelecionarCampanha
}) {
  const curada = isCurada(campanha)

  return (
    <Card
      className={cn(
        "h-full transition-colors",
        // Curated campaigns are visually distinct (Req. 4.5).
        curada && "border-team-purple/50 bg-team-purple/5",
      )}
      data-curada={curada}
    >
      <CardHeader className="gap-1 pb-3">
        <div className="flex items-start justify-between gap-2">
          <button
            type="button"
            className="text-left"
            onClick={() =>
              onSelecionarCampanha({
                stix_id: campanha.stix_id,
                nome: campanha.nome,
                slug_curado: campanha.slug_curado,
              })
            }
          >
            <CardTitle className="text-base hover:underline">
              {campanha.nome}
            </CardTitle>
          </button>
          {curada && <CuradaBadge />}
        </div>
        {campanha.descricao && (
          <CardDescription className="line-clamp-3 text-xs">
            {campanha.descricao}
          </CardDescription>
        )}
      </CardHeader>
    </Card>
  )
}

/** An actor group card listing its (already filtered) campaigns (Req. 4.3). */
function AtorCard({
  ator,
  campanhas,
  onSelecionarCampanha,
}: {
  ator: AtorStix
  campanhas: CampanhaStix[]
  onSelecionarCampanha: SelecionarCampanha
}) {
  const { t } = useTranslation("stix")
  // The backend's synthetic group carries this stix_id; show a localized label.
  const nome =
    ator.stix_id === "sem-ator" ? t("stix.selecao.semAtor") : ator.nome

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{nome}</CardTitle>
        <CardDescription className="text-xs">
          {t("stix.selecao.campanhasDoAtor", { count: campanhas.length })}
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        {campanhas.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {t("stix.selecao.semCampanhas")}
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {campanhas.map((campanha) => (
              <li key={campanha.stix_id}>
                <CampanhaLinha
                  campanha={campanha}
                  onSelecionarCampanha={onSelecionarCampanha}
                />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

/** A compact, selectable campaign row nested under an actor (Req. 4.3, 4.4). */
function CampanhaLinha({
  campanha,
  onSelecionarCampanha,
}: {
  campanha: CampanhaStix
  onSelecionarCampanha: SelecionarCampanha
}) {
  const curada = isCurada(campanha)

  return (
    <button
      type="button"
      onClick={() =>
        onSelecionarCampanha({
          stix_id: campanha.stix_id,
          nome: campanha.nome,
          slug_curado: campanha.slug_curado,
        })
      }
      className={cn(
        "flex w-full items-center justify-between gap-2 rounded-md border p-2 text-left transition-colors hover:bg-accent hover:text-accent-foreground",
        curada && "border-team-purple/50 bg-team-purple/5",
      )}
      data-curada={curada}
    >
      <span className="truncate text-sm font-medium">{campanha.nome}</span>
      {curada && <CuradaBadge />}
    </button>
  )
}

/** The "já curada" badge for campaigns already worked by a colleague (Req. 4.5). */
function CuradaBadge() {
  const { t } = useTranslation("stix")
  return (
    <span className="shrink-0 rounded-full bg-team-purple/15 px-2 py-0.5 text-xs font-medium text-team-purple">
      {t("stix.selecao.jaCurada")}
    </span>
  )
}

function LoadingLine({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-sm text-muted-foreground" role="status">
      {children}
    </p>
  )
}

function ErrorBlock({
  message,
  onRetry,
}: {
  message: string
  onRetry: () => void
}) {
  const { t } = useTranslation("stix")
  return (
    <div className="flex flex-col items-start gap-2" role="alert">
      <p className="text-sm text-destructive">{message}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        {t("stix.selecao.tentarNovamente")}
      </Button>
    </div>
  )
}
