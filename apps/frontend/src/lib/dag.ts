/**
 * Typed API wrapper for the explicit DAG Generation/Visualization phase
 * (DagView screen — Req. 5, 6).
 *
 * Wraps the single DAG endpoint:
 * - `GET /api/campaigns/{campaign}/dag` -> {@link DagView}
 *
 * The backend contract is the source of truth (see
 * `ui/backend/app/services/dag/dag_service.py`). The view covers the
 * three states of the contract:
 * - Curated valid case: `curated=true`, `can_advance=true`, with `nodes`/`edges`
 *   filled and `mermaid` assembled (handed straight to the MermaidDiagram).
 * - Non-curated case ("passa-direto"): `curated=false`, `can_advance=false`,
 *   `unavailable=true` and a `message` — no AI is ever invoked (Req. 5.4, 5.5).
 * - Curated but missing/malformed DAG: typed error (`curated=true`,
 *   `can_advance=false`, `error`) (Req. 5.3, 6.4).
 *
 * This module lives beside `src/lib/stages.ts` and mirrors its style.
 *
 * In the six-stage scheme, DAG Generation is **Stage 3** (derived from the
 * Structural Modeling of Stage 1) and DAGs are persisted, multi-origin entities
 * (Req. 3). Besides the legacy `getDag` (read-only DagView), this module adds
 * the types/wrappers for the persisted-DAG endpoints (Req. 3.7, 8.7):
 * - `GET  /api/campaigns/{campaign}/dags`              -> {@link DagSummary}[] (list)
 * - `GET  /api/campaigns/{campaign}/dags/{dag_id}`     -> {@link DagDetail} (graph)
 * - `POST /api/campaigns/{campaign}/stage/3/choose-dag` -> {@link CaseStageStates}
 */
import { api } from "@/lib/api"
import type { CaseStageStates } from "@/lib/campaigns"

// ---------------------------------------------------------------------------
// DAG view — `GET /api/campaigns/{campaign}/dag` (Req. 5, 6).
// ---------------------------------------------------------------------------

/**
 * One node of the DAG as shown in the payload (the `nodes` entries): the
 * structural node's `node_id` plus its ATT&CK technique identity (Req. 5.2).
 */
export interface DagNodeView {
  node_id: string
  technique_id: string | null
  technique_name: string | null
  tactic: string | null
}

/**
 * One directed edge of the DAG (an `edges` entry): `source` is the source
 * node_id, `target` the target node_id (Req. 5.2, 6.1).
 */
export interface DagEdgeView {
  source: string
  target: string
}

/**
 * The DAG view returned by `GET /api/campaigns/{campaign}/dag` (Req. 5, 6).
 * `nodes`, `edges` and `mermaid` are filled only for a curated valid case; the
 * stub / error fields (`unavailable`, `message`, `error`) are populated only in
 * the non-curated or error states.
 */
export interface DagView {
  campaign_id: string
  name: string
  curated: boolean
  can_advance: boolean
  nodes: DagNodeView[]
  edges: DagEdgeView[]
  mermaid: string
  unavailable: boolean
  message: string | null
  error: string | null
}

/**
 * Fetch the DAG view (nodes / edges / Mermaid, or stub / error) for a campaign.
 *
 * Legacy endpoint `GET /api/campaigns/{campaign}/dag`: kept for compatibility.
 * In the persisted scheme (Req. 4), it resolves the chosen DAG (or, when none
 * was chosen, the single curated DAG) via `get_persisted_dag`; the
 * {@link DagView} response shape stays the same.
 */
export function getDag(campaign: string): Promise<DagView> {
  return api.get<DagView>(`/campaigns/${encodeURIComponent(campaign)}/dag`)
}

// ---------------------------------------------------------------------------
// Persisted, multi-origin DAG — Stage 3 (Req. 3). Backend DTOs: see
// `ui/backend/app/services/dag/dag_service.py` and `app/api/campaigns.py`.
// ---------------------------------------------------------------------------

/**
 * Origin of a DAG — an explicit, extensible attribute (Req. 3.7, 10.4).
 *
 * Follows exactly the `TranslationSource` precedent: `"curadoria_humana"` is the
 * value of the curated DAGs; `"gerado_ia"` is already accepted by the schema
 * (CHECK) for the pluggable AI implementation later (Req. 9). The union stays
 * open (`string & {}`) for future values without a contract change on the
 * frontend.
 */
export type DagOrigin = "curadoria_humana" | "gerado_ia" | (string & {})

/**
 * Summary of a persisted DAG, for a Campaign's listing
 * (`GET /api/campaigns/{campaign}/dags`, backend `DagSummary` — Req. 3.7). Carries
 * the identity columns — without the graph `content`, obtained on demand via
 * {@link getDagById}.
 */
export interface DagSummary {
  id: number
  campaign_id: string
  origin: DagOrigin
  identifier: string
  label?: string | null
  /** ISO-8601 timestamp (backend `created_at`). */
  created_at: string
}

/**
 * `GET /api/campaigns/{campaign}/dags` payload — the Campaign's persisted DAGs
 * (backend `DagListResponse`, Req. 3.7). The listing is preceded by an
 * idempotent backfill of the curated DAGs, so a curated case always shows at
 * least its curation DAG.
 */
export interface DagListResponse {
  campaign_id: string
  dags: DagSummary[]
}

/**
 * The graph + Mermaid of a persisted DAG
 * (`GET /api/campaigns/{campaign}/dags/{dag_id}`, Req. 4.4). Reuses the graph
 * shape of {@link DagView} (`nodes`/`edges`/`mermaid`, handed straight to the
 * `MermaidDiagram`) and adds the persisted DAG identity (`dag_id`, `origin`,
 * `identifier`).
 */
export interface DagDetail {
  campaign_id: string
  dag_id: number
  origin: DagOrigin
  identifier: string
  label?: string | null
  nodes: DagNodeView[]
  edges: DagEdgeView[]
  mermaid: string
}

/**
 * List the persisted DAGs of a Campaign (Stage 3 — Req. 3.7, 8.7). A curated
 * case always returns at least its curation DAG (idempotent backfill on the
 * backend).
 */
export function getDags(campaign: string): Promise<DagListResponse> {
  return api.get<DagListResponse>(
    `/campaigns/${encodeURIComponent(campaign)}/dags`,
  )
}

/**
 * Get the graph + Mermaid of a persisted DAG by its `dagId` (Req. 4.4). Responds
 * 404 when the `dagId` does not exist or does not belong to the Campaign
 * (propagated as {@link ApiError} by the HTTP client).
 */
export function getDagById(campaign: string, dagId: number): Promise<DagDetail> {
  return api.get<DagDetail>(
    `/campaigns/${encodeURIComponent(campaign)}/dags/${encodeURIComponent(dagId)}`,
  )
}

/**
 * Choose a DAG for the execution, completing Stage 3 (DAG Generation) and
 * unblocking Stage 4 (`POST /api/campaigns/{campaign}/stage/3/choose-dag`,
 * Req. 8.7). Returns the per-stage state ({@link CaseStageStates}) already with
 * Stage 3 `completed` and Stage 4 unblocked.
 *
 * Typed backend failures (propagated as {@link ApiError} with the `detail`
 * `{motivo, mensagem}`):
 * - **409 Conflict** — the Campaign has no persisted DAG;
 * - **404 Not Found** — `dagId` does not exist or belongs to another Campaign;
 * - **500** — persist failure (the previous state is preserved).
 */
export function chooseDag(
  campaign: string,
  dagId: number,
): Promise<CaseStageStates> {
  return api.post<CaseStageStates>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/3/choose-dag`,
    { dag_id: dagId },
  )
}
