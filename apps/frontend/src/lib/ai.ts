/**
 * Typed API wrappers for the AI-curation run surface (Req. 13.1–13.4). Wraps the
 * two thin FastAPI endpoints implemented in task 16.1:
 *
 * - `POST /api/campaigns/{campaign}/ai/run` (optional body `{ max_iterations }`)
 *     -> {@link AiRun}. Creates the `agent_runs` row and invokes the compiled
 *     StateGraph, returning the run summary row directly (Req. 13.1).
 *
 * - `GET /api/campaigns/{campaign}/ai/runs/{id}` -> {@link AiRunDetail}. Returns
 *     the run summary plus its iterations (oldest first), each carrying the
 *     evaluator verdict recorded for that iteration (Req. 13.2).
 *
 * These types mirror the backend response models in `app/api/ai.py`
 * (`AiRunResponse`, `AiIterationView`, `AiEvaluationView`, `AiRunDetailResponse`)
 * — the source of truth for the wire contract. The view (`AiRunView`) polls
 * {@link getAiRun} so each new iteration and the evaluator verdict are displayed
 * within 5s of becoming available (Req. 13.3, 13.4).
 */
import { api } from "@/lib/api"

/** Run status — a member of the backend `AgentRunStatus` portable enum. */
export type AgentRunStatus =
  | "pendente"
  | "em_execucao"
  | "concluido"
  | "falhou"
  | "abortado"
  | (string & {})

/** Evaluator decision driving the loop (backend `EvaluatorDecision`). */
export type EvaluatorDecision = "refinar" | "finalizar" | (string & {})

/**
 * A single `agent_runs` row (backend `AiRunResponse`). Returned directly by the
 * start endpoint and nested under {@link AiRunDetail.run} by the read endpoint.
 */
export interface AiRun {
  id: number
  campaign_id: string
  status: AgentRunStatus
  /** LangGraph checkpoint thread key making the run resumable. */
  thread_id: string
  /** The run's loop guard (1..100). */
  max_iterations: number
  /** Number of plan→execute→measure→improve passes performed. */
  iterations_used: number
  /** Overall verdict summary; null until the run reaches a terminal state. */
  final_verdict_success?: boolean | null
}

/** The evaluator's verdict for an iteration (backend `AiEvaluationView`). */
export interface AiEvaluation {
  id: number
  /** Stage 6 execution report the evaluator read, if any. */
  report_id?: number | null
  /** Execution success rate read from the report, in [0, 1]. */
  success_rate: number
  /** Procedural-sufficiency metric read from the report, in [0, 1]. */
  procedural_coverage: number
  /** Whether the evaluator's verdict passed. */
  verdict_success: boolean
  /** Evaluator decision driving the loop: `refinar` / `finalizar`. */
  decision: EvaluatorDecision
  /** LLM reasoning for the decision (≤10,000 chars). */
  rationale: string
}

/** One iteration of a run with its evaluator verdict (backend `AiIterationView`). */
export interface AiIteration {
  id: number
  /** 0-based iteration number within the run. */
  index: number
  /** Stage refined into this iteration; null on the first pass. */
  refined_stage?: string | null
  /** Free-form iteration notes. */
  notes?: string | null
  /** Naive-UTC creation timestamp of the iteration. */
  created_at: string
  /** The evaluator verdict recorded for this iteration, if any (0..1). */
  evaluation?: AiEvaluation | null
}

/** `GET /api/campaigns/{campaign}/ai/runs/{id}` payload (backend `AiRunDetailResponse`). */
export interface AiRunDetail {
  run: AiRun
  /** The run's iterations with their evaluator verdicts, oldest first. */
  iterations: AiIteration[]
}

/** Request body for the start endpoint (backend `AiRunStartRequest`). */
export interface StartAiRunRequest {
  /** Optional loop guard; omitted → the configured default (3), clamped to 1..100. */
  max_iterations?: number
}

/**
 * Start an AI curation run for a campaign (Req. 13.1). Creates the `agent_runs`
 * row and invokes the compiled StateGraph, returning the run summary row.
 *
 * `maxIterations` is optional: when omitted the backend resolves the configured
 * default; when provided it is clamped by the service to the inclusive 1..100
 * range.
 */
export function startAiRun(campaign: string, maxIterations?: number): Promise<AiRun> {
  const body: StartAiRunRequest | undefined =
    maxIterations === undefined ? undefined : { max_iterations: maxIterations }
  return api.post<AiRun>(`/campaigns/${encodeURIComponent(campaign)}/ai/run`, body)
}

/**
 * Read an AI run with its iterations (oldest first) and the evaluator verdict of
 * each (Req. 13.2). The view polls this so new iterations (Req. 13.3) and the
 * evaluator verdict (Req. 13.4) surface within 5s of becoming available.
 */
export function getAiRun(campaign: string, id: number | string): Promise<AiRunDetail> {
  return api.get<AiRunDetail>(
    `/campaigns/${encodeURIComponent(campaign)}/ai/runs/${encodeURIComponent(String(id))}`,
  )
}
