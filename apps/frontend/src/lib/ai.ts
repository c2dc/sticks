/**
 * Typed API wrappers for the AI-curation run surface (Req. 13.1–13.4). Wraps the
 * two thin FastAPI endpoints implemented in task 16.1:
 *
 * - `POST /api/campaigns/{campaign}/ai/run` (optional body `{ max_iterations }`)
 *     -> {@link AiRun}. Creates the `agent_runs` row and invokes the compiled
 *     StateGraph, returning the run summary row directly (Req. 13.1).
 *
 * - `GET /api/campaigns/{campaign}/ai/runs/{id}` -> {@link AiRunDetail}. Returns
 *     the run summary (with its `error` cause when the run failed) plus its
 *     iterations (oldest first), each carrying its per-stage Agent_Output
 *     artifacts with their 1:1 Audit_Trace and the evaluator verdict recorded
 *     for that iteration (Req. 1.4, 2.4, 13.2).
 *
 * These types mirror the backend response models in `app/api/ai.py`
 * (`AiRunResponse`, `AiIterationView`, `AiStageOutputView`, `AiAgentTraceView`,
 * `AiEvaluationView`, `AiRunDetailResponse`)
 * — the source of truth for the wire contract. The view (`AiRunView`) polls
 * {@link getAiRun} so each new iteration and the evaluator verdict are displayed
 * within 5s of becoming available (Req. 13.3, 13.4).
 */
import { api } from "@/lib/api"

/** Run status — a member of the backend `AgentRunStatus` portable enum. */
export type AgentRunStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "aborted"
  | (string & {})

/** Evaluator decision driving the loop (backend `EvaluatorDecision`). */
export type EvaluatorDecision = "refine" | "finish" | (string & {})

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
  /**
   * Structured failure cause surfaced from `agent_runs.error` (the failing
   * stage + the absent-input/cause message) when the run failed; null on runs
   * that did not fail (Req. 2.6, 4.4).
   */
  error?: string | null
}

/**
 * The Audit_Trace of a stage output (backend `AiAgentTraceView`): the data the
 * agent received and what it performed. `agent_input` carries the prior-stage
 * artifacts, the Selected_Campaign context, the retrieved cross-campaign
 * experience, and the ATT&CK/STIX attack context (Req. 1.2). `processing`
 * carries the LLM prompt and response, reasoning, tools invoked, and repair
 * attempts (Req. 1.3). Any null/absent processing element is the literal
 * `"not_available"` sentinel rather than omitted (Req. 1.6).
 */
export interface AiAgentTrace {
  /** Agent_Input the agent received for the stage. */
  agent_input: Record<string, unknown>
  /** Agent_Processing the agent performed; absent elements are `"not_available"`. */
  processing: Record<string, unknown>
}

/**
 * One `agent_stage_outputs` row with its 1:1 Audit_Trace (backend
 * `AiStageOutputView`). The `payload` is the Agent_Output artifact produced for
 * the `stage`, stamped with the AI-generated `origin`, paired with the `trace`
 * carrying that agent's Agent_Input + Agent_Processing (Req. 1.1, 1.4, 2.4).
 */
export interface AiStageOutput {
  id: number
  /** English `AgentStage` value the output belongs to. */
  stage: string
  /** Origin of the artifact; always `ai_generated` on the AI path. */
  origin: string
  /** The serialized Agent_Output artifact produced for the stage. */
  payload: Record<string, unknown>
  /** The Agent_Input + Agent_Processing Audit_Trace for the output. */
  trace: AiAgentTrace
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
  /** Evaluator decision driving the loop: `refine` / `finish`. */
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
  /**
   * The iteration's per-stage Agent_Output artifacts, each with its 1:1
   * Audit_Trace, ordered by stage (Req. 1.1, 1.4, 2.4).
   */
  stage_outputs: AiStageOutput[]
  /**
   * Identity of the failing Stage/Agent captured on a failed Run; null unless
   * this iteration carries the failure anchor (Req. 2.6).
   */
  failing_agent?: string | null
  /** The evaluator verdict recorded for this iteration, if any (0..1). */
  evaluation?: AiEvaluation | null
}

/** `GET /api/campaigns/{campaign}/ai/runs/{id}` payload (backend `AiRunDetailResponse`). */
export interface AiRunDetail {
  run: AiRun
  /**
   * The run's iterations with their stage outputs + Audit_Trace and evaluator
   * verdicts, oldest first.
   */
  iterations: AiIteration[]
}

/**
 * One stage's AI-output-vs-human-curation comparison (backend
 * `GroundTruthStageComparison`). The symmetric set diff of the identifiers the
 * AI Run produced vs. the ones the human curation carries for this stage.
 */
export interface GroundTruthStageComparison {
  /** English `AgentStage` value this comparison belongs to. */
  stage: string
  /** The identifiers the AI Run produced for this stage (sorted). */
  ai_items: string[]
  /** The identifiers the human curation carries for this stage (sorted). */
  curated_items: string[]
  /** Identifiers present in both the AI output and the curation. */
  matched: string[]
  /** Curated identifiers the AI output did not produce. */
  missing: string[]
  /** AI-produced identifiers absent from the curation. */
  extra: string[]
}

/**
 * `GET /api/campaigns/{campaign}/ai/runs/{id}/ground-truth` payload (backend
 * `GroundTruthComparisonResponse`). The on-demand Ground_Truth_Comparison of a
 * completed Run's AI output against the human curation of a Curated_Campaign
 * (Req. 5.3). Curation is read only as the comparison baseline (Req. 5.4).
 */
export interface GroundTruthComparison {
  /** The Curated_Campaign slug compared (server-side curated). */
  campaign_id: string
  /** The completed Run compared. */
  run_id: number
  /** Per-stage AI-output-vs-curation comparisons. */
  stages: GroundTruthStageComparison[]
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
  // Always send a JSON body: the backend's `AiRunStartRequest` body parameter is
  // required, so a bodyless POST is rejected with 422. When no `maxIterations`
  // is provided we send an empty object `{}` and the backend resolves the
  // configured default (clamped to 1..100).
  const body: StartAiRunRequest =
    maxIterations === undefined ? {} : { max_iterations: maxIterations }
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

/**
 * Compute the on-demand Ground_Truth_Comparison of a completed Run against the
 * human curation of a Curated_Campaign (Req. 5.1–5.4). Curated-ness is enforced
 * server-side: the backend returns a "not a curated campaign" error for any
 * non-curated campaign, so this is only called for the eight Curated_Campaigns.
 * Curation is read only as the comparison baseline — it never drives a Run.
 */
export function compareAiRunWithCuration(
  campaign: string,
  id: number | string,
): Promise<GroundTruthComparison> {
  return api.get<GroundTruthComparison>(
    `/campaigns/${encodeURIComponent(campaign)}/ai/runs/${encodeURIComponent(
      String(id),
    )}/ground-truth`,
  )
}
