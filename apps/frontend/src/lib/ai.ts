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
 *     iterations (oldest first), each carrying its per-phase Agent_Output
 *     artifacts with their 1:1 Audit_Trace and the evaluator verdict recorded
 *     for that iteration (Req. 1.4, 2.4, 13.2).
 *
 * These types mirror the backend response models in `app/api/ai.py`
 * (`AiRunResponse`, `AiIterationView`, `AiPhaseOutputView`, `AiAgentTraceView`,
 * `AiEvaluationView`, `AiRunDetailResponse`)
 * — the source of truth for the wire contract. The view (`AiRunView`) polls
 * {@link getAiRun} so each new iteration and the evaluator verdict are displayed
 * within 5s of becoming available (Req. 13.3, 13.4).
 */
import { api, ApiError } from "@/lib/api"
import { reasonCodeFrom, type ReasonCodes } from "@/lib/llm"

/** Run status — a member of the backend `AgentRunStatus` portable enum. */
export type AgentRunStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
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
   * phase + the absent-input/cause message) when the run failed; null on runs
   * that did not fail (Req. 2.6, 4.4).
   */
  error?: string | null
  /** Phase waiting for the researcher's approval; set only while `awaiting_approval`. */
  pending_phase?: AiRunPhase | null
  /** Phase running in the background; set only while `running`. */
  active_phase?: AiRunPhase | null
  /** Non-fatal notice code (`no_distinct_flow` / `regeneration_failed`). */
  notice_code?: AiNoticeCode | null
  /** Structured failure of a failed run. */
  failure?: AiRunFailure | null
  /** Phases approved so far, oldest first. */
  approvals?: AiApproval[]
  /** Attack flow proposal history; one entry is marked `current`. */
  flow_proposals?: AiFlowProposal[]
  /** Maximum number of attack flow proposals per run (same for every run). */
  flow_proposal_max: number
  /** Command proposals, environment requirements and planned coverage; null until produced. */
  operationalization_review?: AiOperationalizationReview | null
  /** Planned and achieved emulation coverage; null until the first pause. */
  coverage?: AiRunCoverage | null
}

/** Pipeline phase names used by the interactive run (backend `AgentPhase`). */
export type AiRunPhase =
  | "operationalization"
  | "flow"
  | "environment"
  | "execution"
  | "evaluation"
  | (string & {})

/** Phases the researcher can approve (`{phase}` of the approve route). */
export type ApprovableAiPhase = "operationalization" | "flow" | "environment"

/** Non-fatal notice codes of a run. */
export type AiNoticeCode = "no_distinct_flow" | "regeneration_failed" | (string & {})

/** Failure of a run: failing phase, failure code and, when known, the underlying cause code. */
export interface AiRunFailure {
  phase: AiRunPhase
  /** `phase_failed`, `execution_error`, `interrupted_by_restart` or `aborted_by_user`. */
  code: string
  cause_code?: string | null
}

/** One entry of the approval log. */
export interface AiApproval {
  phase: AiRunPhase
  approved_at: string
  /** Flow proposal approved, when the phase is `flow`. */
  proposal_id?: number | null
}

/** Why an attack flow was presented (`reason_code` of a flow proposal). */
export type AiFlowReasonCode =
  | "exact_match"
  | "adapted_above_threshold"
  | "no_candidate_above_threshold"
  | "all_candidates_excluded"
  | "low_coverage_override"
  | (string & {})

/** One attack flow presented to the researcher. */
export interface AiFlowProposal {
  proposal_id: number
  /** Documented flow id when the proposal is an exact reference; null for created flows. */
  flow_id: number | null
  /** `FlowOrigin` of the campaign `attack_flows` row that backs this proposal. */
  origin: string
  /** Identifier of that row; with `origin` it is the key the backend loads it by. */
  identifier: string
  flow_hash?: string | null
  /** Provenance kind (e.g. `exact`, `adapted`, `created`). */
  kind?: string | null
  /** Ids of the reference flows used. */
  references: number[]
  similarity?: number | null
  reason_code?: AiFlowReasonCode | null
  /** Share of the campaign techniques covered by the best reference flow. */
  coverage?: number | null
  presented_at: string
  current: boolean
  [key: string]: unknown
}

/** Where a command parameter value came from. */
export type ParameterSource = "agent_inferred" | "atomic_default" | "unresolved" | (string & {})

export interface AiCommandParameter {
  name: string
  value: string | null
  source: ParameterSource
}

export interface AiCommandProposal {
  command_ref: number | null
  template: string
  resolved_command: string
  parameters: AiCommandParameter[]
}

export interface AiEnvironmentRequirements {
  hosts: Array<{ role: string; os: string; image_hint?: string | null }>
  ports: number[]
  services: string[]
  files: string[]
}

/** Per-technique emulability in the review. */
export type AiEmulabilityStatus = "planned_emulable" | "not_emulable" | (string & {})

export interface AiOperationalizedTechnique {
  technique_id: string
  /** Readable ATT&CK technique name, when known. */
  technique_name?: string | null
  commands: AiCommandProposal[]
  environment_requirements?: AiEnvironmentRequirements | null
  emulability_status?: AiEmulabilityStatus | null
  /** `no_command`, `port_conflict`, `image_unavailable`, `os_unsupported`, `requires_vm`. */
  emulability_reason_code?: string | null
  [key: string]: unknown
}

export interface AiOperationalizationReview {
  techniques: AiOperationalizedTechnique[]
  planned_coverage?: number | null
  [key: string]: unknown
}

/** Coverage of one technique (backend `AiTechniqueCoverage`). */
export interface AiTechniqueCoverage {
  technique_id: string
  /** Planned emulability: `planned_emulable` / `not_emulable`. */
  status: AiEmulabilityStatus
  reason_code?: string | null
  /** `executed_success` / `executed_failure` / `not_executed`; null before execution. */
  achieved_status?: string | null
}

/** Planned and achieved coverage of a run (backend `AiRunCoverage`). */
export interface AiRunCoverage {
  planned_coverage?: number | null
  achieved_coverage?: number | null
  techniques: AiTechniqueCoverage[]
}

/**
 * The Audit_Trace of a phase output (backend `AiAgentTraceView`): the data the
 * agent received and what it performed. `agent_input` carries the prior-phase
 * artifacts, the Selected_Campaign context, the retrieved cross-campaign
 * experience, and the ATT&CK/STIX attack context (Req. 1.2). `processing`
 * carries the LLM prompt and response, reasoning, tools invoked, and repair
 * attempts (Req. 1.3). Any null/absent processing element is the literal
 * `"not_available"` sentinel rather than omitted (Req. 1.6).
 */
export interface AiAgentTrace {
  /** Agent_Input the agent received for the phase. */
  agent_input: Record<string, unknown>
  /** Agent_Processing the agent performed; absent elements are `"not_available"`. */
  processing: Record<string, unknown>
}

/**
 * One `agent_phase_outputs` row with its 1:1 Audit_Trace (backend
 * `AiPhaseOutputView`). The `payload` is the Agent_Output artifact produced for
 * the `phase`, stamped with the AI-generated `origin`, paired with the `trace`
 * carrying that agent's Agent_Input + Agent_Processing (Req. 1.1, 1.4, 2.4).
 */
export interface AiPhaseOutput {
  id: number
  /** English `AgentPhase` value the output belongs to. */
  phase: string
  /** Origin of the artifact; always `ai_generated` on the AI path. */
  origin: string
  /** The serialized Agent_Output artifact produced for the phase. */
  payload: Record<string, unknown>
  /** The Agent_Input + Agent_Processing Audit_Trace for the output. */
  trace: AiAgentTrace
}

/** The evaluator's verdict for an iteration (backend `AiEvaluationView`). */
export interface AiEvaluation {
  id: number
  /** Execution phase report the evaluator read, if any. */
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
  /** Phase refined into this iteration; null on the first pass. */
  refined_phase?: string | null
  /** Free-form iteration notes. */
  notes?: string | null
  /** Naive-UTC creation timestamp of the iteration. */
  created_at: string
  /**
   * The iteration's per-phase Agent_Output artifacts, each with its 1:1
   * Audit_Trace, ordered by phase (Req. 1.1, 1.4, 2.4).
   */
  phase_outputs: AiPhaseOutput[]
  /**
   * Identity of the failing Phase/Agent captured on a failed Run; null unless
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
   * The run's iterations with their phase outputs + Audit_Trace and evaluator
   * verdicts, oldest first.
   */
  iterations: AiIteration[]
}

/** Request body for the start endpoint (backend `AiRunStartRequest`). */
export interface StartAiRunRequest {
  /** Optional loop guard; omitted → the configured default (3), clamped to 1..100. */
  max_iterations?: number
}

/**
 * Codes of `detail.reason` returned by the interactive run routes. 409 and 404 errors
 * carry one of these; read it with {@link aiRunReasonFrom}.
 */
export type AiRunReason =
  | "run_already_active"
  | "phase_running"
  | "wrong_phase"
  | "run_not_awaiting"
  | "invalid_phase"
  | "run_not_found"
  | "no_active_run"
  | "flow_proposal_not_found"
  | "flow_proposal_limit_reached"
  | "profile_unavailable"
  | "llm_blocked"
  | "run_failed"
  | "dependency_unavailable"

/**
 * Structured reason of an `ApiError` from the run routes. Reuses the LLM reason reader
 * and adds `run_id` (present on `run_already_active` and transition errors). `reason`
 * is null for errors without a structured body.
 */
export function aiRunReasonFrom(error: unknown): ReasonCodes & {
  reason: AiRunReason | null
  runId: number | null
} {
  const base = reasonCodeFrom(error)
  const body = (error as { body?: { detail?: { run_id?: unknown } } } | null)?.body
  const runId = body?.detail?.run_id
  return {
    ...base,
    reason: base.reason as AiRunReason | null,
    runId: typeof runId === "number" ? runId : null,
  }
}

/**
 * Start an AI curation run for a campaign (Req. 13.1). The backend answers 202
 * right after creating the `agent_runs` row (`status=running`,
 * `active_phase=operationalization`); the phases run in the background and the
 * caller polls {@link getAiRun} or {@link getActiveAiRun}.
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

function runPath(campaign: string, id: number | string): string {
  return `/campaigns/${encodeURIComponent(campaign)}/ai/runs/${encodeURIComponent(String(id))}`
}

/**
 * The run of the campaign that is running or awaiting approval, or null when there
 * is none (the backend answers 404 `no_active_run`). Used to resume after a reload.
 */
export async function getActiveAiRun(campaign: string): Promise<AiRun | null> {
  try {
    return await api.get<AiRun>(`/campaigns/${encodeURIComponent(campaign)}/ai/runs/active`)
  } catch (error) {
    if (error instanceof ApiError && aiRunReasonFrom(error).reason === "no_active_run") {
      return null
    }
    throw error
  }
}

/**
 * Approve the pending phase and start the next one in the background (202).
 * Errors: 404 `run_not_found`; 409 `wrong_phase`, `phase_running`, `run_not_awaiting`,
 * `llm_blocked`, `profile_unavailable`; 422 `invalid_phase`.
 */
export function approveAiPhase(
  campaign: string,
  id: number | string,
  phase: ApprovableAiPhase,
): Promise<AiRun> {
  return api.post<AiRun>(`${runPath(campaign, id)}/phases/${encodeURIComponent(phase)}/approve`)
}

/**
 * Generate another attack flow, excluding the ones already presented (202). Same
 * errors as {@link approveAiPhase}.
 */
export function regenerateAiFlow(campaign: string, id: number | string): Promise<AiRun> {
  return api.post<AiRun>(`${runPath(campaign, id)}/phases/flow/regenerate`)
}

/**
 * Make an earlier flow proposal the current one (200). Errors: 404 `run_not_found`,
 * `flow_proposal_not_found`; 409 `wrong_phase`, `phase_running`, `run_not_awaiting`.
 */
export function selectAiFlowProposal(
  campaign: string,
  id: number | string,
  proposalId: number,
): Promise<AiRun> {
  return api.post<AiRun>(`${runPath(campaign, id)}/phases/flow/select`, {
    proposal_id: proposalId,
  })
}

/**
 * Abort a run that is awaiting approval (200). Errors: 404 `run_not_found`;
 * 409 `phase_running`, `run_not_awaiting`.
 */
export function abortAiRun(campaign: string, id: number | string): Promise<AiRun> {
  return api.post<AiRun>(`${runPath(campaign, id)}/abort`)
}
