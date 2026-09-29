/**
 * Typed API wrappers and real-time helpers for the curated cases pipeline
 * (Req. 1, 5). Wraps `GET /api/casos` and the `WS /ws/estagios/{caso}` channel,
 * and carries the case-selector view of the same `GET /api/casos` contract.
 *
 * This module is the single source of truth for the curated-cases types. It
 * models two projections of the same backend contract (`app/api/casos.py`,
 * pt-BR field names):
 *
 *  - The pipeline-overview subset — {@link Caso}, {@link EstagioEstado},
 *    {@link CasoEstados}, {@link ProgressoAgregado}, {@link CasosResponse} —
 *    consumed by `PipelineOverview` via {@link getCasos}.
 *  - The wider case-selector view — {@link CaseListItem},
 *    {@link StageStateView}, {@link CaseStageStates}, {@link AggregateProgress},
 *    {@link CasesResponse} plus the per-case load-error and translation-source
 *    types — consumed by `CaseSelector` via {@link fetchCases}. This view
 *    exposes the extra fields (`iniciavel`, `origem_traducao`, per-case `erro`,
 *    `caso_id`, …) the selector needs and is a structural superset of the
 *    overview subset.
 *
 * The backend contracts are defined in the pipeline-ui design (Endpoints REST
 * and Canais WebSocket/SSE, in pt-BR).
 */
import { api } from "@/lib/api"
import { connectWs, type WsConnection } from "@/lib/ws"

/** Per-stage state — exactly one value (Req. 1.4, 1.6, 5.3). */
export type EstadoEstagio =
  | "nao_iniciado"
  | "em_andamento"
  | "concluido"
  | "erro"

export const ESTADOS_ESTAGIO: readonly EstadoEstagio[] = [
  "nao_iniciado",
  "em_andamento",
  "concluido",
  "erro",
] as const

/** One of the three pipeline stages. */
export type NumeroEstagio = 1 | 2 | 3

/** State of a single stage within a case (from `GET /api/casos`). */
export interface EstagioEstado {
  estagio: NumeroEstagio
  estado: EstadoEstagio
  /** Processing progress 0–100 while `em_andamento` (Req. 1.5). */
  progresso: number
  /** True when the previous stage is not yet `concluido` (Req. 5.4). */
  bloqueado: boolean
  /** Optional error cause shown for the `erro` state (Req. 1.6). */
  erro?: string | null
}

/** Aggregated stage states for a case. */
export interface CasoEstados {
  estagios: EstagioEstado[]
  /** True when all three stages are `concluido`. */
  concluido_total: boolean
}

/** A curated case with its per-stage state (from `GET /api/casos`). */
export interface Caso {
  id: string
  nome: string
  estados: CasoEstados
}

/** Aggregate replication progress across the 8 cases (Req. 5.5). */
export interface ProgressoAgregado {
  casos_concluidos: number
  total_casos: number
  percentual: number
}

/** Response shape of `GET /api/casos`. */
export interface CasosResponse {
  casos: Caso[]
  progresso: ProgressoAgregado
}

/** Fetch the curated cases with per-stage state and aggregate progress. */
export function getCasos(): Promise<CasosResponse> {
  return api.get<CasosResponse>("/casos")
}

/**
 * A real-time stage-progress frame pushed by `WS /ws/estagios/{caso}`
 * (Req. 1.4, 1.5). Carries the stage number, its new state, its progress and
 * the previous state for transition awareness.
 */
export interface EstagioProgressoFrame {
  estagio: NumeroEstagio
  estado: EstadoEstagio
  progresso: number
  estado_anterior?: EstadoEstagio
  erro?: string | null
}

/**
 * Subscribe to the stage-progress channel for a case. Reconnects on drop so the
 * UI keeps reflecting real-time updates (Req. 1.4, 1.5).
 */
export function connectEstagios(
  caso: string,
  estagio: NumeroEstagio,
  onFrame: (frame: EstagioProgressoFrame) => void,
): WsConnection {
  return connectWs<EstagioProgressoFrame>(
    `/ws/estagios/${encodeURIComponent(caso)}?estagio=${estagio}`,
    { onMessage: onFrame },
  )
}

// ===========================================================================
// Case-selector view (Req. 5.1–5.6, 11.1–11.2).
//
// The wider projection of the same `GET /api/casos` contract used by the case
// selector. It surfaces the extra fields the selector needs (`iniciavel`,
// `origem_traducao`, per-case `erro`, `caso_id`, …) and is a structural
// superset of the overview subset above.
//
// Backend contract (source of truth, pt-BR strings):
// - `CaseListItem`   -> id, nome, origem_traducao?, estados?, erro?
// - `StageStateView` -> estagio, estado, progresso, bloqueado, iniciavel,
//                        mensagem_erro?, etapa_falha?
// - `CaseStageStates`-> caso_id, estagios[], concluido_total
// - `AggregateProgress` -> casos_concluidos, total_casos, percentual,
//                          casos_concluidos_ids[]
// - `CaseLoadError`  -> slug, nome, error_type, arquivo?, detail
// ===========================================================================

/** Stage state alias with the wider name used across the selector. */
export type StageState = EstadoEstagio
export type StageNumber = NumeroEstagio

/** The three pipeline stages, in order (Req. 5.2). */
export const PIPELINE_STAGES: readonly StageNumber[] = [1, 2, 3] as const

/** Total number of curated cases (Req. 5.1, 5.5). */
export const TOTAL_CURATED_CASES = 8

/** Translation source — explicit, extensible attribute (Req. 11.1, 11.2). */
export type TranslationSource = "curadoria_humana" | (string & {})

/** Kind of per-case load failure (backend `CaseLoadErrorType`, Req. 5.6). */
export type CaseLoadErrorType =
  | "arquivo_ausente"
  | "json_malformado"
  | "estrutura_invalida"

/**
 * State of a single stage of a case (backend `StageStateView`, Req. 5.3, 1.4).
 *
 * `bloqueado` is `true` when the stage cannot be started because its preceding
 * stage is not `concluido` (Req. 5.4); `iniciavel` is its exact complement.
 * Stage 1 is never blocked.
 */
export interface StageStateView {
  estagio: number
  estado: StageState
  progresso: number
  bloqueado: boolean
  iniciavel: boolean
  mensagem_erro?: string | null
  etapa_falha?: string | null
}

/**
 * Per-stage state of a case plus whether it is fully replicated
 * (backend `CaseStageStates`). `concluido_total` is `true` iff all three stages
 * are `concluido` — the unit aggregate progress counts (Req. 5.5).
 */
export interface CaseStageStates {
  caso_id: string
  estagios: StageStateView[]
  concluido_total: boolean
}

/** A descriptive, per-case load failure (backend `CaseLoadError`, Req. 5.6). */
export interface CaseLoadError {
  slug: string
  nome: string
  error_type: CaseLoadErrorType
  arquivo?: string | null
  detail: string
}

/** One curated case in `GET /api/casos` (backend `CaseListItem`, Req. 5.1, 5.6). */
export interface CaseListItem {
  id: string
  nome: string
  origem_traducao?: TranslationSource | null
  estados?: CaseStageStates | null
  erro?: CaseLoadError | null
}

/**
 * Aggregate replication progress across the 8 curated cases
 * (backend `AggregateProgress`, Req. 5.5).
 */
export interface AggregateProgress {
  casos_concluidos: number
  total_casos: number
  percentual: number
  casos_concluidos_ids?: string[]
}

/** `GET /api/casos` payload — the 8 cases + aggregate progress (Req. 5.1). */
export interface CasesResponse {
  casos: CaseListItem[]
  progresso: AggregateProgress
}

/**
 * Fetch the 8 curated cases with per-stage state and aggregate progress, typed
 * for the selector's full field set. Uses the same `/casos` endpoint as
 * {@link getCasos}; the wider response type is a structural superset.
 */
export function fetchCases(options?: RequestInit): Promise<CasesResponse> {
  return api.get<CasesResponse>("/casos", options)
}

// ---------------------------------------------------------------------------
// Presentational helpers (pure).
// ---------------------------------------------------------------------------

/** `true` when the case failed to load (per-case `erro` present, Req. 5.6). */
export function hasLoadError(
  caso: CaseListItem,
): caso is CaseListItem & { erro: CaseLoadError } {
  return caso.erro != null
}

/** Find the state of a specific stage in a case, if available. */
export function stageStateOf(
  caso: CaseListItem,
  estagio: StageNumber,
): StageStateView | undefined {
  return caso.estados?.estagios.find((s) => s.estagio === estagio)
}

/**
 * `true` when a stage may be started in the UI (Req. 5.4). A blocked, running
 * or already-completed stage is never startable, and a case that failed to
 * load is never startable.
 */
export function isStageStartable(
  caso: CaseListItem,
  estagio: StageNumber,
): boolean {
  if (hasLoadError(caso)) return false
  const state = stageStateOf(caso, estagio)
  if (!state) return false
  return state.iniciavel && !state.bloqueado && state.estado === "nao_iniciado"
}
