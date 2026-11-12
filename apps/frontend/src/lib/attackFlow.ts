/**
 * Typed API wrappers for attack flows.
 *
 * - `GET /api/campaigns/{campaign}/flows`           -> {@link getFlows}
 * - `GET /api/campaigns/{campaign}/flows/{flow_id}` -> {@link getFlow}
 * - `GET /api/attack-flows/{flow_id}`               -> {@link getAttackFlow}
 *
 * Mirrors `app/api/flows.py` and `app/services/flow/flow_service.py`. The name
 * `lib/flow.ts` belongs to the navigation reducer, hence `attackFlow.ts`.
 * Read-only; an unknown flow is a 404 (`flow_not_found`) surfaced as `ApiError`.
 */
import { api } from "@/lib/api"

/** Origin of an attack flow (backend `FlowOrigin`); open for new values. */
export type FlowOrigin =
  | "human_curation"
  | "ai_generated"
  | "mitre_corpus"
  | (string & {})

/** How a campaign flow was obtained from the corpus (backend `FlowProvenance`). */
export type FlowProvenanceKind = "exact" | "adapted" | "created"

/** A corpus flow referenced by a provenance (backend `FlowReference`). */
export interface FlowReference {
  id: number
  label: string | null
  source_key: string | null
}

/** Provenance of a flow (backend `FlowProvenanceView`). */
export interface FlowProvenance {
  kind: FlowProvenanceKind
  similarity: number
  /** Ids of the referenced corpus flows. */
  references: number[]
  reference_flows: FlowReference[]
}

/** Summary of a persisted flow (backend `FlowSummary`). */
export interface FlowSummary {
  id: number
  campaign_id: string | null
  origin: FlowOrigin
  identifier: string
  label: string | null
  scope: string | null
  has_cycles: boolean
  provenance: FlowProvenance | null
  /** ISO timestamp. */
  created_at: string
}

/** A node of a flow graph (backend `FlowNodeView`). */
export interface FlowNodeView {
  node_id: string
  type: string
  name: string | null
  technique_id: string | null
  tactic_id: string | null
  command_ref: number | null
}

/** A directed edge of a flow graph (backend `FlowEdgeView`). */
export interface FlowEdgeView {
  source: string
  target: string
  is_back_edge: boolean
}

/** Graph, Mermaid, cycles and provenance of one flow (backend `FlowDetail`). */
export interface FlowDetail extends FlowSummary {
  description: string | null
  nodes: FlowNodeView[]
  edges: FlowEdgeView[]
  /** Ready-to-render Mermaid source. */
  mermaid: string
  /** Each cycle is the ordered list of node ids that form it. */
  cycles: string[][]
  /** `[source, target]` pairs of the edges that close a cycle. */
  back_edges: [string, string][]
}

/** `GET /api/campaigns/{campaign}/flows` payload (backend `FlowListResponse`). */
export interface FlowListResponse {
  campaign_id: string
  flows: FlowSummary[]
}

/** List the attack flows of a campaign, with provenance. */
export function getFlows(campaign: string): Promise<FlowListResponse> {
  return api.get<FlowListResponse>(`/campaigns/${encodeURIComponent(campaign)}/flows`)
}

/** Graph, Mermaid, cycles and provenance of a campaign flow. */
export function getFlow(campaign: string, flowId: number): Promise<FlowDetail> {
  return api.get<FlowDetail>(
    `/campaigns/${encodeURIComponent(campaign)}/flows/${encodeURIComponent(
      String(flowId),
    )}`,
  )
}

/** A flow by id, e.g. a corpus flow shown as a reference in a provenance badge. */
export function getAttackFlow(flowId: number): Promise<FlowDetail> {
  return api.get<FlowDetail>(`/attack-flows/${encodeURIComponent(String(flowId))}`)
}
