/**
 * Typed API wrappers and real-time helpers for the curated campaigns pipeline
 * (Req. 1, 5). Wraps `GET /api/campaigns` and the `WS /ws/stages/{campaign}`
 * channel, and carries the campaign-selector view of the same
 * `GET /api/campaigns` contract.
 *
 * This module is the single source of truth for the curated-campaign types. It
 * models two projections of the same backend contract (`app/api/campaigns.py`):
 *
 *  - The pipeline-overview subset — {@link Campaign}, {@link StageStateOverview},
 *    {@link CampaignStates}, {@link AggregateProgress}, {@link CampaignsResponse} —
 *    consumed by `PipelineOverview` via {@link getCampaigns}.
 *  - The wider campaign-selector view — {@link CaseListItem},
 *    {@link StageStateView}, {@link CaseStageStates}, {@link AggregateProgress},
 *    {@link CasesResponse} plus the per-case load-error and translation-source
 *    types — consumed by `CaseSelector` via {@link fetchCases}. This view
 *    exposes the extra fields (`startable`, `translation_source`, per-case `erro`,
 *    `campaign_id`, …) the selector needs and is a structural superset of the
 *    overview subset.
 *
 * The backend contracts are defined in the pipeline-ui design (REST endpoints
 * and WebSocket/SSE channels).
 */
import { api } from "@/lib/api"
import { connectWs, type WsConnection } from "@/lib/ws"

/** Per-stage state — exactly one value (Req. 1.4, 1.6, 5.3). */
export type StageState =
  | "nao_iniciado"
  | "em_andamento"
  | "concluido"
  | "erro"

export const STAGE_STATES: readonly StageState[] = [
  "nao_iniciado",
  "em_andamento",
  "concluido",
  "erro",
] as const

/**
 * One of the six pipeline stages (scheme 1..6 — Req. 1.1, 1.2, 1.7).
 * 1 = STIX base selection, 2 = Campaign selection, 3 = DAG generation,
 * 4 = Operationalization, 5 = Simulation environment creation,
 * 6 = Execution, measurement and report.
 */
export type StageNumber = 1 | 2 | 3 | 4 | 5 | 6

/** State of a single stage within a campaign (from `GET /api/campaigns`). */
export interface StageStateOverview {
  stage: StageNumber
  state: StageState
  /** Processing progress 0–100 while `em_andamento` (Req. 1.5). */
  progress: number
  /** True when the previous stage is not yet `concluido` (Req. 5.4). */
  blocked: boolean
  /** Optional error cause shown for the `erro` state (Req. 1.6). */
  error_message?: string | null
}

/** Aggregated stage states for a campaign. */
export interface CampaignStates {
  stages: StageStateOverview[]
  /** True when all six stages are `concluido`. */
  fully_completed: boolean
}

/** A curated campaign with its per-stage state (from `GET /api/campaigns`). */
export interface Campaign {
  id: string
  name: string
  states: CampaignStates
}

/** Response shape of `GET /api/campaigns`. */
export interface CampaignsResponse {
  campaigns: Campaign[]
  progress: AggregateProgress
}

/** Fetch the curated campaigns with per-stage state and aggregate progress. */
export function getCampaigns(): Promise<CampaignsResponse> {
  return api.get<CampaignsResponse>("/campaigns")
}

/**
 * One provisioning log line carried on a stage-progress frame (Stage 5,
 * Req. 2.2, 2.6). This is the structural shape of the backend `LogLineView`
 * (`StageProgressEvent.logs`).
 *
 * It is declared here — and NOT imported from `@/lib/stages` — on purpose:
 * `stages.ts` already imports from this module (`CaseStageStates`,
 * `StageState`), so importing `LogLineView` back from `stages.ts` would
 * create an import cycle. Keeping a local, structurally-identical shape lets
 * `stages.ts`'s `LogLineView` stay the single documented source of truth
 * while remaining assignable to this field without a cross-module dependency.
 */
export interface LogLineFrame {
  /** Monotonic sequence per campaign (for dedupe/reorder on reconnect). */
  seq: number
  /** Pipeline step that emitted the line (e.g. `"subir_conteineres"`). */
  step: string
  /** Line stream; open union for new values (Req. 2.6). */
  stream: "comando" | "stdout" | "stderr" | "info" | (string & {})
  /** Log line text. */
  text: string
  /** ISO timestamp (naive-UTC on the backend) of the line. */
  at: string
}

/**
 * A real-time stage-progress frame pushed by `WS /ws/stages/{campaign}`
 * (Req. 1.4, 1.5). Carries the stage number, its new state, its progress and
 * the previous state for transition awareness.
 *
 * For Stage 5 (active provisioning), the frame also carries `logs` — the
 * **new** log lines since the last frame (and, on connect, the full buffer),
 * which the `ProvisioningConsole` accumulates by `seq` (Req. 2.2, 2.5, 2.7).
 * Empty/absent for stages without provisioning.
 */
export interface StageProgressFrame {
  stage: StageNumber
  state: StageState
  progress: number
  previous_state?: StageState
  error_message?: string | null
  /** New log lines since the last frame (Stage 5, Req. 2.2, 2.5). */
  logs?: LogLineFrame[]
}

/**
 * Subscribe to the stage-progress channel for a campaign. Accepts any of the
 * six stages (`StageNumber = 1 | 2 | 3 | 4 | 5 | 6`, Req. 1.2, 1.7). Reconnects
 * on drop so the UI keeps reflecting real-time updates (Req. 1.4, 1.5).
 */
export function connectStages(
  campaign: string,
  stage: StageNumber,
  onFrame: (frame: StageProgressFrame) => void,
): WsConnection {
  return connectWs<StageProgressFrame>(
    `/ws/stages/${encodeURIComponent(campaign)}?stage=${stage}`,
    { onMessage: onFrame },
  )
}

// ===========================================================================
// Campaign-selector view (Req. 5.1–5.6, 11.1–11.2).
//
// The wider projection of the same `GET /api/campaigns` contract used by the
// campaign selector. It surfaces the extra fields the selector needs
// (`startable`, `translation_source`, per-case `erro`, `campaign_id`, …) and is
// a structural superset of the overview subset above.
//
// Backend contract (source of truth):
// - `CaseListItem`   -> id, name, translation_source?, states?, error?
// - `StageStateView` -> stage, state, progress, blocked, startable,
//                        error_message?, failed_step?
// - `CaseStageStates`-> campaign_id, stages[], fully_completed
// - `AggregateProgress` -> completed_campaigns, total_campaigns, percentage,
//                          completed_campaign_ids[]
// - `CaseLoadError`  -> slug, name, error_type, file?, detail
// ===========================================================================

/** The six pipeline stages, in order (Req. 1.1, 1.2, 1.7, 5.2). */
export const PIPELINE_STAGES: readonly StageNumber[] = [1, 2, 3, 4, 5, 6] as const

/** Total number of curated campaigns (Req. 5.1, 5.5). */
export const TOTAL_CURATED_CASES = 8

/** Translation source — explicit, extensible attribute (Req. 11.1, 11.2). */
export type TranslationSource = "curadoria_humana" | (string & {})

/** Kind of per-case load failure (backend `CaseLoadErrorType`, Req. 5.6). */
export type CaseLoadErrorType =
  | "arquivo_ausente"
  | "json_malformado"
  | "estrutura_invalida"

/**
 * State of a single stage of a campaign (backend `StageStateView`, Req. 5.3,
 * 1.4).
 *
 * `blocked` is `true` when the stage cannot be started because its preceding
 * stage is not `concluido` (Req. 5.4); `startable` is its exact complement.
 * Stage 1 is never blocked.
 */
export interface StageStateView {
  stage: number
  state: StageState
  progress: number
  blocked: boolean
  startable: boolean
  error_message?: string | null
  failed_step?: string | null
}

/**
 * Per-stage state of a campaign plus whether it is fully replicated
 * (backend `CaseStageStates`). `fully_completed` is `true` iff all six stages
 * are `concluido` — the unit aggregate progress counts (Req. 5.5).
 */
export interface CaseStageStates {
  campaign_id: string
  stages: StageStateView[]
  fully_completed: boolean
}

/** A descriptive, per-case load failure (backend `CaseLoadError`, Req. 5.6). */
export interface CaseLoadError {
  slug: string
  name: string
  error_type: CaseLoadErrorType
  file?: string | null
  detail: string
}

/** One curated campaign in `GET /api/campaigns` (backend `CaseListItem`, Req. 5.1, 5.6). */
export interface CaseListItem {
  id: string
  name: string
  translation_source?: TranslationSource | null
  states?: CaseStageStates | null
  error?: CaseLoadError | null
}

/**
 * Aggregate replication progress across the 8 curated campaigns
 * (backend `AggregateProgress`, Req. 5.5).
 */
export interface AggregateProgress {
  completed_campaigns: number
  total_campaigns: number
  percentage: number
  completed_campaign_ids?: string[]
}

/** `GET /api/campaigns` payload — the 8 campaigns + aggregate progress (Req. 5.1). */
export interface CasesResponse {
  campaigns: CaseListItem[]
  progress: AggregateProgress
}

/**
 * Fetch the 8 curated campaigns with per-stage state and aggregate progress,
 * typed for the selector's full field set. Uses the same `/campaigns` endpoint
 * as {@link getCampaigns}; the wider response type is a structural superset.
 */
export function fetchCases(options?: RequestInit): Promise<CasesResponse> {
  return api.get<CasesResponse>("/campaigns", options)
}

// ---------------------------------------------------------------------------
// Presentational helpers (pure).
// ---------------------------------------------------------------------------

/** `true` when the campaign failed to load (per-case `error` present, Req. 5.6). */
export function hasLoadError(
  campaign: CaseListItem,
): campaign is CaseListItem & { error: CaseLoadError } {
  return campaign.error != null
}

/** Find the state of a specific stage in a campaign, if available. */
export function stageStateOf(
  campaign: CaseListItem,
  stage: StageNumber,
): StageStateView | undefined {
  return campaign.states?.stages.find((s) => s.stage === stage)
}

/**
 * `true` when a stage may be started in the UI (Req. 5.4). A blocked, running
 * or already-completed stage is never startable, and a campaign that failed to
 * load is never startable.
 */
export function isStageStartable(
  campaign: CaseListItem,
  stage: StageNumber,
): boolean {
  if (hasLoadError(campaign)) return false
  const state = stageStateOf(campaign, stage)
  if (!state) return false
  return state.startable && !state.blocked && state.state === "nao_iniciado"
}
