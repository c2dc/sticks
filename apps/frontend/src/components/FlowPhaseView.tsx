import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { MermaidDiagram } from "@/components/MermaidDiagram"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  getFlow,
  getFlows,
  type FlowDetail,
  type FlowProvenanceKind,
} from "@/lib/attackFlow"

/**
 * FlowPhaseView — the `flow` phase of the guided run: the attack flow generated
 * for the campaign, rendered from the Mermaid source supplied by the API, with
 * its provenance badge (exact / adapted / created), the MITRE reference flows
 * and, when present, the list of detected cycles (Req. 3.6, 2.6).
 *
 * Pass `flow` to render an already-loaded flow, or `campaign` (optionally with
 * `flowId`) to have the view fetch it. Without a `flowId` the most recent flow
 * of the campaign is shown.
 */
export interface FlowPhaseViewProps {
  campaign?: string
  flowId?: number
  flow?: FlowDetail
}

const PROVENANCE_STYLES: Record<FlowProvenanceKind, string> = {
  exact: "border-green-500/50 bg-green-500/10 text-green-700 dark:text-green-300",
  adapted: "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  created: "border-blue-500/50 bg-blue-500/10 text-blue-700 dark:text-blue-300",
}

export function FlowPhaseView({ campaign, flowId, flow: providedFlow }: FlowPhaseViewProps) {
  const { t } = useTranslation("pipeline")
  const [fetched, setFetched] = useState<FlowDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (providedFlow || !campaign) return
    let cancelled = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- resets the request state synchronously when the key changes, before the async fetch; deriving it during render would change the loading timing
    setLoading(true)
    setFailed(false)

    async function load(campaignId: string) {
      try {
        let id = flowId
        if (id === undefined) {
          const { flows } = await getFlows(campaignId)
          const latest = [...flows].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
          if (!latest) {
            if (!cancelled) setFetched(null)
            return
          }
          id = latest.id
        }
        const detail = await getFlow(campaignId, id)
        if (!cancelled) setFetched(detail)
      } catch {
        if (!cancelled) setFailed(true)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void load(campaign)
    return () => {
      cancelled = true
    }
  }, [campaign, flowId, providedFlow])

  const flow = providedFlow ?? fetched

  if (loading) {
    return (
      <p className="text-sm text-muted-foreground" data-section="flow-phase-loading" role="status">
        {t("flow.loading")}
      </p>
    )
  }
  if (failed) {
    return (
      <p className="text-sm text-destructive" data-section="flow-phase-error" role="alert">
        {t("flow.error")}
      </p>
    )
  }
  if (!flow) {
    return (
      <p className="text-sm text-muted-foreground" data-section="flow-phase-empty">
        {t("flow.empty")}
      </p>
    )
  }

  const provenance = flow.provenance
  const cycles = flow.cycles ?? []
  const backEdges = flow.back_edges ?? []

  return (
    <Card data-section="flow-phase-view" data-flow-id={flow.id}>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>{flow.label ?? t("flow.title")}</CardTitle>
          {provenance ? (
            <span
              className={`rounded-full border px-2 py-0.5 text-xs font-medium ${PROVENANCE_STYLES[provenance.kind] ?? ""}`}
              data-part="provenance-badge"
              data-kind={provenance.kind}
              title={t("flow.provenance.title")}
            >
              {t(`flow.provenance.${provenance.kind}`, { defaultValue: provenance.kind })}
            </span>
          ) : null}
        </div>
        {flow.description ? (
          <p className="text-sm text-muted-foreground">{flow.description}</p>
        ) : null}
      </CardHeader>

      <CardContent className="flex flex-col gap-4">
        <div className="overflow-auto rounded-md border p-2" data-part="diagram">
          <MermaidDiagram definition={flow.mermaid} />
        </div>

        {provenance ? (
          <section className="flex flex-col gap-2" data-part="provenance">
            <h3 className="text-sm font-semibold">{t("flow.provenance.title")}</h3>
            <p className="text-xs text-muted-foreground">
              {t("flow.provenance.similarity", { value: provenance.similarity.toFixed(2) })}
            </p>
            <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("flow.references.title")}
            </h4>
            {provenance.reference_flows.length === 0 ? (
              <p className="text-xs text-muted-foreground" data-part="references-empty">
                {t("flow.references.none")}
              </p>
            ) : (
              <ul className="flex flex-col gap-1 text-sm" data-part="references">
                {provenance.reference_flows.map((ref) => (
                  <li key={ref.id} className="flex flex-wrap items-center gap-2 rounded-md border p-2">
                    <span className="font-medium">{ref.label ?? `#${ref.id}`}</span>
                    {ref.source_key ? (
                      <span className="text-xs text-muted-foreground">{ref.source_key}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        ) : null}

        {flow.has_cycles || cycles.length > 0 ? (
          <section
            className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3"
            data-part="cycles"
            role="alert"
          >
            <h3 className="text-sm font-semibold">{t("flow.cycles.title")}</h3>
            <p className="text-xs text-muted-foreground">{t("flow.cycles.description")}</p>
            {cycles.length > 0 ? (
              <ul className="flex flex-col gap-1 text-sm">
                {cycles.map((cycle, index) => (
                  <li key={index} className="font-mono text-xs">
                    {cycle.join(" → ")}
                  </li>
                ))}
              </ul>
            ) : null}
            {backEdges.length > 0 ? (
              <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
                {backEdges.map(([source, target]) => (
                  <li key={`${source}->${target}`}>
                    {t("flow.cycles.backEdge", { source, target })}
                  </li>
                ))}
              </ul>
            ) : null}
          </section>
        ) : null}
      </CardContent>
    </Card>
  )
}
