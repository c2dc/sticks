import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Loader2, Sparkles } from "lucide-react"

import { MermaidDiagram } from "@/components/MermaidDiagram"
import { EmptyNote } from "@/components/StageSection"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import type { CaseStageStates } from "@/lib/campaigns"
import {
  chooseDag,
  getDagById,
  getDags,
  type DagDetail,
  type DagSummary,
} from "@/lib/dag"

/**
 * DagStageView — body of **Stage 2 (DAG generation)** inside the
 * `ExecutionWizard` (Req. 3). Under the five-stage scheme, DAG generation is
 * Stage 2 (derived from the Stage 1 Structural Modeling). It is not a standalone
 * routed phase: navigation (Next/Back) and the sequential gate live in the
 * wizard and in the backend. Here we only list the campaign's persisted DAGs,
 * show the selected DAG's graph and let the Researcher choose the DAG that
 * completes Stage 2.
 *
 * Props (lean contract for the wizard wiring):
 * - `campaignId`: Campaign slug; used in `getDags`/`getDagById`/`chooseDag`.
 * - `onDagChosen`: called after a successful choice with the
 *   {@link CaseStageStates} returned by the backend (Stage 2 already `concluido`
 *   and Stage 3 unlocked). The wizard re-reads/derives its state from there.
 *
 * Rendered states (Req. 3.4, 3.5):
 * - **loading** — `role="status"` note while `getDags` resolves.
 * - **error** — pt-BR message `stix.dag.erro` on a load/choice failure.
 * - **no DAG** (empty list) — note `stix.dag.semDag` and the "Generate with AI"
 *   button **visible but disabled** with the "coming soon" hint; advancing is
 *   blocked (no DAG to choose). Clicking the button is a no-op (no AI).
 * - **list** — each {@link DagSummary} with its identifier/label and the origin
 *   (`stix.dag.origem.<origin>`), the DAG selection, the selected graph via
 *   `MermaidDiagram` and the "Select DAG" action that calls `chooseDag`.
 *   The "Generate with AI" button stays visible and disabled (Req. 6.1, 6.5).
 */
export function DagStageView({
  campaignId,
  onDagChosen,
}: {
  campaignId: string
  onDagChosen?: (states: CaseStageStates) => void
}) {
  const { t } = useTranslation("stix")

  const [dags, setDags] = useState<DagSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // Load the campaign's persisted DAGs once per slug (active-flag pattern of the
  // other screens, to avoid set-state after unmount).
  useEffect(() => {
    let active = true
    setDags(null)
    setError(null)
    setLoading(true)
    getDags(campaignId)
      .then((resp) => {
        if (!active) return
        setDags(resp.dags)
        setLoading(false)
      })
      .catch(() => {
        if (!active) return
        setError(t("stix.dag.erro"))
        setLoading(false)
      })
    return () => {
      active = false
    }
  }, [campaignId, t])

  return (
    <Card className="flex flex-col">
      <CardHeader className="gap-1 pb-3">
        <CardTitle className="text-base">{t("stix.dag.titulo")}</CardTitle>
        <CardDescription>{campaignId}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 pt-0">
        {loading ? (
          <p className="text-sm text-muted-foreground" role="status">
            {t("stix.dag.carregando")}
          </p>
        ) : error ? (
          <EmptyNote>{error}</EmptyNote>
        ) : dags && dags.length > 0 ? (
          <DagList
            campaignId={campaignId}
            dags={dags}
            onError={() => setError(t("stix.dag.erro"))}
            onDagChosen={onDagChosen}
          />
        ) : (
          <NoDag />
        )}

        {/* "Generate DAG" is always visible but disabled ("coming soon"):
            a no-op in this iteration, no AI client is invoked (Req. 3.4,
            3.5). Kept outside the branches so it also shows in the list. */}
        {!loading && !error ? <GenerateWithAiButton /> : null}
      </CardContent>
    </Card>
  )
}

/**
 * "No DAG" state: the Campaign has no persisted DAG, so advancing beyond
 * Stage 2 is blocked (nothing to choose) and we show the note `stix.dag.semDag`
 * (Req. 3.4). The disabled "Generate DAG" button is rendered by the parent.
 */
function NoDag() {
  const { t } = useTranslation("stix")
  return <EmptyNote>{t("stix.dag.semDag")}</EmptyNote>
}

/**
 * "Generate DAG" button — visible and **disabled** with the "coming soon" hint
 * (Req. 3.4). It is a no-op: no AI client is invoked in this iteration
 * (Req. 3.5).
 */
function GenerateWithAiButton() {
  const { t } = useTranslation("stix")
  return (
    <div className="flex items-center gap-2">
      <Button variant="outline" disabled aria-disabled title={t("stix.dag.emBreve")}>
        <Sparkles aria-hidden />
        {t("stix.dag.gerarComIa")}
      </Button>
      <span className="text-xs text-muted-foreground">({t("stix.dag.emBreve")})</span>
    </div>
  )
}

/**
 * List of persisted DAGs (≥1): selection, preview of the selected graph and the
 * "Select DAG" action that completes Stage 2 (Req. 3.3, 3.7). The first DAG in
 * the list is pre-selected so a graph shows immediately.
 */
function DagList({
  campaignId,
  dags,
  onError,
  onDagChosen,
}: {
  campaignId: string
  dags: DagSummary[]
  onError: () => void
  onDagChosen?: (states: CaseStageStates) => void
}) {
  const { t } = useTranslation("stix")
  const [selectedId, setSelectedId] = useState<number>(dags[0].id)
  const [choosing, setChoosing] = useState(false)

  const autoChosen = useRef(false)
  const handleChoose = useCallback(async () => {
    setChoosing(true)
    try {
      const states = await chooseDag(campaignId, selectedId)
      onDagChosen?.(states)
    } catch {
      onError()
    } finally {
      setChoosing(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaignId, selectedId])

  // Auto-conclude when there is exactly one DAG (curated pipeline): the
  // generated DAG is the one that will be used, no user choice needed.
  useEffect(() => {
    if (dags.length === 1 && !autoChosen.current) {
      autoChosen.current = true
      void handleChoose()
    }
  }, [dags.length, handleChoose])

  // Single-DAG: no selection UI needed, just the graph.
  const showSelectionUI = dags.length > 1

  return (
    <div className="flex flex-col gap-4">
      {showSelectionUI ? (
      <ul className="flex flex-col gap-2" aria-label={t("stix.dag.selecionar")}>
        {dags.map((dag) => {
          const selected = dag.id === selectedId
          return (
            <li key={dag.id}>
              <button
                type="button"
                aria-pressed={selected}
                onClick={() => setSelectedId(dag.id)}
                className={
                  "flex w-full flex-col items-start gap-0.5 rounded-md border p-3 text-left text-sm transition-colors " +
                  (selected
                    ? "border-team-blue ring-1 ring-team-blue"
                    : "hover:bg-muted/50")
                }
              >
                <span className="font-medium">
                  {dag.label || dag.identifier}
                </span>
                <span className="text-xs text-muted-foreground">
                  {t("stix.dag.origem.label")}: {t(`stix.dag.origem.${dag.origin}`)}
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      ) : null}

      <DagGraph campaignId={campaignId} dagId={selectedId} onError={onError} />

      {showSelectionUI ? (
        <div className="flex items-center justify-end">
          <Button onClick={handleChoose} disabled={choosing}>
            {choosing ? (
              <Loader2 className="animate-spin" aria-hidden />
            ) : null}
            {t("stix.dag.selecionar")}
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/**
 * Preview of the selected DAG's graph: fetches `getDagById` by `dagId` and
 * renders it with `MermaidDiagram` (which already pins `securityLevel: "strict"`).
 * Reloads on each `dagId` change, with an active-flag to drop stale responses.
 */
function DagGraph({
  campaignId,
  dagId,
  onError,
}: {
  campaignId: string
  dagId: number
  onError: () => void
}) {
  const { t } = useTranslation("stix")
  const [detail, setDetail] = useState<DagDetail | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    setDetail(null)
    setLoading(true)
    getDagById(campaignId, dagId)
      .then((d) => {
        if (!active) return
        setDetail(d)
        setLoading(false)
      })
      .catch(() => {
        if (!active) return
        setLoading(false)
        onError()
      })
    return () => {
      active = false
    }
    // onError is a stable callback from the parent; reload only per campaign/dag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaignId, dagId])

  if (loading) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stix.dag.carregando")}
      </p>
    )
  }
  if (!detail || detail.mermaid.trim().length === 0) return null

  return (
    <div className="max-h-[60vh] overflow-auto rounded-md border bg-background p-3">
      <MermaidDiagram definition={detail.mermaid} />
    </div>
  )
}
