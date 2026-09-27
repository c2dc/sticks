/**
 * Typed API wrappers and real-time helpers for the per-stage screens
 * (Stage1View / Stage2View / Stage3View — Task 12.2, Req. 2, 3, 4, 11).
 *
 * These wrap the three stage-detail endpoints and the operation WebSocket:
 * - `GET /api/casos/{caso}/estagio/1` -> {@link Stage1Result}
 * - `GET /api/casos/{caso}/estagio/2` -> {@link Stage2Result}
 * - `GET /api/casos/{caso}/estagio/3` -> {@link Stage3Result}
 * - `WS  /ws/operacao/{operacao}`     -> {@link OperacaoFrame}
 *
 * `src/lib/casos.ts` (task 12.1) owns the `GET /api/casos` + stage-progress
 * channel wrappers and the shared `EstadoEstagio` / `NumeroEstagio` types.
 * This module lives beside it and extends the surface with the stage-detail
 * contracts without editing the existing exports. Backend contracts are the
 * source of truth (pt-BR field names, see the pipeline-ui design).
 */
import { api } from "@/lib/api"
import type { EstadoEstagio } from "@/lib/casos"
import { connectWs, type WsConnection } from "@/lib/ws"

// ---------------------------------------------------------------------------
// Translation source — explicit, extensible attribute (Req. 11.1, 11.2).
// ---------------------------------------------------------------------------

/**
 * Origin of a Stage-2 translation. `"curadoria_humana"` is the value for the 8
 * curated cases; the union stays open (`string & {}`) so a future AI-assisted
 * origin can be shown without changing the persisted model (Req. 11.1, 11.2).
 */
export type OrigemTraducao = "curadoria_humana" | (string & {})

// ---------------------------------------------------------------------------
// Stage 1 — Structural Modeling (Req. 2).
// ---------------------------------------------------------------------------

/** Stage-1 Entrada: the source STIX / DAG reference for the case (Req. 2.1). */
export interface Stage1Entrada {
  caso_id: string
  nome: string
  dag_file: string
  stix_reference: string
  campaign_name: string
}

/** A single extracted ATT&CK technique (Req. 2.3). */
export interface Stage1Technique {
  technique_id?: string
  name?: string
  tactic?: string
  description?: string
  [key: string]: unknown
}

/** A relationship between extracted actions (Req. 2.3). */
export interface Stage1Relationship {
  source?: string
  target?: string
  tipo?: string
  [key: string]: unknown
}

/**
 * Stage-1 Saída: the structural elements extracted from the STIX dataset
 * (Req. 2.3). When `is_empty` is true, no structural elements were extracted
 * and the UI shows the corresponding indication (Req. 2.5).
 */
export interface Stage1Saida {
  techniques: Stage1Technique[]
  relationships: Stage1Relationship[]
  indicators: unknown[]
  infrastructure: unknown[]
  malware: unknown[]
  metadata: Record<string, unknown>
  is_empty: boolean
  note?: string | null
}

/** Response of `GET /api/casos/{caso}/estagio/1`. */
export interface Stage1Result {
  caso_id: string
  estado: EstadoEstagio
  entrada: Stage1Entrada
  saida: Stage1Saida
  /** Failure cause when `estado === "erro"` (Req. 2.6). */
  mensagem_erro?: string | null
  /** Modeling step at which the failure occurred (Req. 2.6). */
  etapa_falha?: string | null
}

/** Fetch the Stage-1 Entrada/Saída for a case. */
export function getEstagio1(caso: string): Promise<Stage1Result> {
  return api.get<Stage1Result>(`/casos/${encodeURIComponent(caso)}/estagio/1`)
}

// ---------------------------------------------------------------------------
// Stage 2 — Curated Translation (Req. 3, 11).
// ---------------------------------------------------------------------------

/** Stage-2 Entrada: the abstract ATT&CK techniques from Stage 1 (Req. 3.1). */
export interface Stage2Entrada {
  techniques: Stage1Technique[]
  /** False when Stage 1 produced no techniques -> no Stage-2 input (Req. 3.6). */
  has_input: boolean
  note?: string | null
}

/** One concrete executor of an Ability (name / platform / command) (Req. 3.2). */
export interface AbilityExecutor {
  name: string
  platform: string
  command: string
}

/** A curated Ability derived from a technique (Req. 3.2). */
export interface Ability {
  ability_id: string
  name: string
  technique_id: string
  tactic: string
  description: string
  executors: AbilityExecutor[]
  /** Translation origin at the Ability level (Req. 11.1). */
  origem_traducao?: OrigemTraducao
}

/** Adversary grouping of Abilities with its atomic ordering (Req. 3.3, 3.5). */
export interface Adversary {
  id: string
  name: string
  description: string
  /** Sequential ordering / dependencies of the grouped Abilities (Req. 3.3). */
  atomic_ordering: string[]
  /** Translation origin at the Adversary level (Req. 11.1). */
  origem_traducao?: OrigemTraducao
}

/**
 * Stage-2 Saída: the curated Abilities, their ordering, the Adversary grouping
 * and the explicit translation-source indicator (Req. 3.2–3.5, 11.1). Absence
 * states are conveyed by `has_abilities` / `has_adversary` (Req. 3.7, 3.8).
 */
export interface Stage2Saida {
  abilities: Ability[]
  /** Sequential order / dependencies across the Abilities (Req. 3.3). */
  ordering: string[]
  adversary: Adversary | null
  /** Explicit, extensible translation-source attribute (Req. 3.4, 11.1). */
  origem_traducao: OrigemTraducao
  /** Human-curation flag associated with the Saída (Req. 3.4). */
  human_curation: boolean
  /** False when the case has no curated Abilities (Req. 3.7). */
  has_abilities: boolean
  /** False when the case has no associated Adversary (Req. 3.8). */
  has_adversary: boolean
  note?: string | null
  adversary_note?: string | null
}

/** Response of `GET /api/casos/{caso}/estagio/2`. */
export interface Stage2Result {
  caso_id: string
  entrada: Stage2Entrada
  saida: Stage2Saida
  /** True when Stage 1 produced no techniques -> Saída suppressed (Req. 3.6). */
  suppressed: boolean
}

/** Fetch the Stage-2 Entrada/Saída for a case. */
export function getEstagio2(caso: string): Promise<Stage2Result> {
  return api.get<Stage2Result>(`/casos/${encodeURIComponent(caso)}/estagio/2`)
}

// ---------------------------------------------------------------------------
// Stage 3 — Adversary Emulation (Req. 4).
// ---------------------------------------------------------------------------

/** Per-Ability execution status during / after an Operation (Req. 4.3). */
export type AbilityResultStatus = "pendente" | "em_execucao" | "sucesso" | "falha"

/** Stage-3 Entrada: the Adversary + Abilities to emulate (Req. 4.1). */
export interface Stage3Entrada {
  caso_id: string
  nome: string
  adversary: Adversary | null
  abilities: Ability[]
  /** False when the case cannot be emulated (e.g. missing Adversary/Abilities). */
  can_emulate: boolean
  note?: string | null
}

/** Result of a single Ability within an Operation (Req. 4.4). */
export interface AbilityResult {
  ability_id: string
  status: AbilityResultStatus
  saida_comando: string
}

/** Overall Operation state (from `GET /api/casos/{caso}/estagio/3`). */
export type OperationState =
  | "nao_iniciada"
  | "em_execucao"
  | "concluida"
  | "falha"
  | (string & {})

/**
 * A view of one Operation for the case: its state, per-Ability results and the
 * aggregate success/failure totals (Req. 4.4, 4.5).
 */
export interface OperationResultView {
  operacao_id: string
  estado: OperationState
  total_sucesso: number
  total_falha: number
  resultados: AbilityResult[]
}

/** Response of `GET /api/casos/{caso}/estagio/3`. */
export interface Stage3Result {
  entrada: Stage3Entrada
  saida: OperationResultView[]
}

/** Fetch the Stage-3 Entrada + accumulated Operation results for a case. */
export function getEstagio3(caso: string): Promise<Stage3Result> {
  return api.get<Stage3Result>(`/casos/${encodeURIComponent(caso)}/estagio/3`)
}

// ---------------------------------------------------------------------------
// Operation real-time channel — `WS /ws/operacao/{operacao}` (Req. 4.3–4.5).
// ---------------------------------------------------------------------------

/**
 * A per-Ability status frame pushed by `WS /ws/operacao/{operacao}` while an
 * Operation runs (Req. 4.3, 4.4). Carries the Ability id, its new status and
 * the command output produced so far.
 */
export interface OperacaoAbilityFrame {
  tipo: "ability"
  ability_id: string
  status: AbilityResultStatus
  saida_comando: string
}

/**
 * The final aggregate frame pushed when the Operation reaches its terminal
 * state (Req. 4.5): success/failure totals and the Operation's final state.
 */
export interface OperacaoAgregadoFrame {
  tipo: "agregado"
  operacao_id: string
  estado: OperationState
  total_sucesso: number
  total_falha: number
}

/** Any frame carried by the Operation channel. */
export type OperacaoFrame = OperacaoAbilityFrame | OperacaoAgregadoFrame

/**
 * Subscribe to the per-Operation channel. `onFrame` receives per-Ability status
 * updates and the final aggregate (Req. 4.3, 4.4, 4.5). Reconnects on drop so
 * the UI keeps reflecting real-time status.
 */
export function connectOperacao(
  operacao: string,
  onFrame: (frame: OperacaoFrame) => void,
): WsConnection {
  return connectWs<OperacaoFrame>(
    `/ws/operacao/${encodeURIComponent(operacao)}`,
    { onMessage: onFrame },
  )
}
