/**
 * Guided Execution Wizard — API wrappers and state derivation.
 *
 * The pipeline is: Stage 1 = Structural Modeling, Stage 2 = DAG generation (no
 * "execute"; completion comes from `chooseDag` in `dag.ts`), Stage 3 =
 * Operationalization, Stage 4 = Environment Provisioning, Stage 5 = Campaign
 * Execution. This module ties the ends of the guided flow by reusing the already
 * correct wrappers from `stages.ts`:
 * - trigger the Modeling (Stage 1 → `executeStage1` → POST /stage/1/execute);
 * - complete the Operationalization (Stage 3 → `completeStage3` → POST
 *   /stage/3/complete; the material is curated, so "advance" = "complete");
 * - reset the campaign to repeat the demonstration;
 * - re-read the per-stage state of the campaign (source of truth = backend).
 *
 * `deriveStates` centralizes the sequential-gate rule on the frontend, for
 * badges and the "Advance" button enablement (Property 1). With the six-stage
 * pipeline (Req. 1.1/1.2), the derivation covers 1..6 automatically, since it is
 * range-agnostic (derives from `PIPELINE_STAGES`). `wizardNavReducer`/
 * `resetByReselection` add the Next/Back navigation with reselection (Req.
 * 4.2–4.6).
 */
import { api } from "@/lib/api"
import {
  PIPELINE_STAGES,
  type Campaign,
  type CampaignStates,
  type StageNumber,
} from "@/lib/campaigns"
import { executeStage1, completeStage3 } from "@/lib/stages"

// ---------------------------------------------------------------------------
// API wrappers (the wizard's endpoints).
// ---------------------------------------------------------------------------

/** Per-stage state of a campaign (contract of `GET /api/campaigns/{campaign}`). */
export interface CaseStageStatesResponse {
  campaign_id: string
  stages: {
    stage: StageNumber
    state: string
    blocked: boolean
    startable: boolean
    progress?: number
  }[]
  fully_completed: boolean
}

/** Re-read the selected campaign (includes `states.stages[]`). */
export function getCampaign(campaign: string): Promise<Campaign> {
  return api.get<Campaign>(`/campaigns/${encodeURIComponent(campaign)}`)
}

/**
 * Trigger the Structural Modeling, which is **Stage 1** (POST /stage/1/execute),
 * the pipeline's starting point. Reuses the already-correct wrapper from
 * `stages.ts`.
 */
export const executeModeling = executeStage1

/**
 * Complete the Operationalization, which is **Stage 3** (POST /stage/3/complete),
 * unblocking Stage 4. Reuses the already-correct wrapper from `stages.ts`.
 */
export const completeTranslation = completeStage3

/** Reset the campaign state (to repeat the demonstration). */
export function resetCampaign(campaign: string): Promise<CaseStageStatesResponse> {
  return api.post<CaseStageStatesResponse>(
    `/campaigns/${encodeURIComponent(campaign)}/reset`,
  )
}

// ---------------------------------------------------------------------------
// Pure derivation of the sequential gate (Property 1).
// ---------------------------------------------------------------------------

export type WizardStageStatus = "completed" | "available" | "blocked"

export interface WizardDerivedStage {
  stage: StageNumber
  status: WizardStageStatus
}

export interface WizardDerivedState {
  stages: WizardDerivedStage[]
  /** Whether it is possible to advance from the given stage to the next. */
  canAdvance: (from: StageNumber) => boolean
}

const STAGES: readonly StageNumber[] = PIPELINE_STAGES

/**
 * Derives, from the set of completed stages, the status of each stage and the
 * advance enablement. Rule (Req. 1.6, 5.3): Stage N (N>1) is `blocked` if and
 * only if N-1 is not completed; it is `available` when the previous one is
 * completed (or it is Stage 1); and `completed` when it itself appears as
 * completed. "Advancing" from N to N+1 is only allowed when N+1 is not blocked —
 * that is, when N is completed.
 */
export function deriveStates(completed: number[]): WizardDerivedState {
  const done = new Set(completed)

  const stages: WizardDerivedStage[] = STAGES.map((stage) => {
    let status: WizardStageStatus
    if (done.has(stage)) {
      status = "completed"
    } else if (stage === 1 || done.has(stage - 1)) {
      status = "available"
    } else {
      status = "blocked"
    }
    return { stage, status }
  })

  const statusOf = (n: number): WizardStageStatus | undefined =>
    stages.find((e) => e.stage === n)?.status

  const canAdvance = (from: StageNumber): boolean => {
    const next = from + 1
    if (next > STAGES.length) return false
    // Only advance if the next one is not blocked, which is equivalent to "the
    // current one (from) is completed".
    return statusOf(next) !== "blocked"
  }

  return { stages, canAdvance }
}

/** Extract the list of `concluido` stage numbers from a `CampaignStates`. */
export function completedStages(states: CampaignStates | undefined): number[] {
  if (!states) return []
  return states.stages
    .filter((e) => e.state === "concluido")
    .map((e) => e.stage)
}

// ---------------------------------------------------------------------------
// Pure derivation of the progress presentation (wizard-progress-stages).
//
// Presentation layer only: it does NOT govern the sequential gate nor the
// "Advance" enablement (that stays the responsibility of `deriveStates`). This
// derivation reuses the `status` of `deriveStates(completed)` as a base and
// combines it with the researcher's advance (`maxVisitedStage`) to produce the
// Presented_State of each stage, plus a pre-completed origin signal.
// ---------------------------------------------------------------------------

/** How a stage badge should be presented (presentation layer). */
export type PresentedState =
  | "completed" // reached and completed on the backend
  | "available" // ready/reachable, not yet visually "completed"
  | "blocked" // predecessor not completed

export interface PresentedStage {
  stage: StageNumber
  presented: PresentedState
  /** True when the badge should mark "pre-completed (curated)". */
  preCompleted: boolean
}

export interface DerivedPresentation {
  stages: PresentedStage[]
}

/**
 * Derives the Presented_State of each stage from the Real_State (`completed`),
 * the researcher's advance (`maxVisitedStage`) and the currently open window
 * (`currentStage`).
 *
 * Reuses `deriveStates(completed)` for the base `status` (never reimplements the
 * gate), guaranteeing the presentation never contradicts the gate: a stage
 * `blocked` in the gate is always `blocked` in the presentation.
 *
 * Rule per stage N (let `status` = deriveStates(completed).status):
 *   - status === "completed" AND N <= maxVisitedStage  → "completed"
 *   - status === "completed" AND N  > maxVisitedStage  → "available"
 *       (real completion not yet revealed — gradual reveal, option A)
 *   - otherwise                                        → status
 *       ("available" or "blocked", passed through from deriveStates)
 *
 * `preCompleted(N)` ≙ `presented(N) === "completed"` ∧ `N > 1` ∧
 * `completed.includes(N)` ∧ `completed.includes(N-1)` ∧ `currentStage <= N`.
 * Marks, in the presentation only, a stage completed by the system (curation)
 * without the researcher having acted within window N.
 *
 * Pure and deterministic, side-effect free. Defensive: `completed` with numbers
 * outside 1..6 are ignored by the base `deriveStates`; an empty `completed`
 * produces a presentation all `available`/`blocked` per the gate.
 */
export function derivePresentation(
  completed: number[],
  maxVisitedStage: StageNumber,
  currentStage: StageNumber,
): DerivedPresentation {
  const done = new Set(completed)
  const base = deriveStates(completed)

  const stages: PresentedStage[] = base.stages.map(
    ({ stage, status }) => {
      let presented: PresentedState
      if (status === "completed" && stage <= maxVisitedStage) {
        presented = "completed"
      } else if (status === "completed") {
        // Completed for real, but not yet reached: gradual reveal.
        presented = "available"
      } else {
        // "available" or "blocked", passed through from the gate.
        presented = status
      }

      const preCompleted =
        presented === "completed" &&
        stage > 1 &&
        done.has(stage) &&
        done.has(stage - 1) &&
        currentStage <= stage

      return { stage, presented, preCompleted }
    },
  )

  return { stages }
}

// ---------------------------------------------------------------------------
// Next/Back navigation with reselection (six-stage pipeline, Req. 4.2–4.6).
//
// The wizard drives 6 stages, one screen per stage. This module is the PURE
// state machine of the navigation (Next/Back) and the reselection, exported
// separately from the component so the navigation invariants can be checked by
// property tests without mounting the DOM (Property 12).
//
// Source of truth of the RESET: the Backend. The frontend does not invalidate
// `StageRun` locally — it only models the INTENT (navigate, confirm, reselect)
// and derives the set of completed stages the Backend recomputes after a
// reselection. The Backend rule (Req. 4.6): reselecting an earlier stage K
// resets the `StageRun` of all later stages (> K) to `nao_iniciado`, restoring
// the sequential gate from K. The `resetByReselection` function mirrors exactly
// that rule so the UI can derive the next state before/after the Backend re-read.
// ---------------------------------------------------------------------------

/** First and last stages of the pipeline (derived from `STAGES`). */
export const FIRST_STAGE: StageNumber = STAGES[0]
export const LAST_STAGE: StageNumber = STAGES[STAGES.length - 1]

/**
 * Pure state of the six-stage wizard navigation.
 *
 * - `currentStage`: the stage screen currently open (1..6).
 * - `completed`: the (ordered, deduplicated) set of `concluido` stages per the
 *   Backend — the source of truth of the gate and the reset.
 * - `reselecting`: `true` after triggering "Back", signalling that the next
 *   confirmation (via "Next") should be treated as a reselection that recomputes
 *   the following stages. While `false`, the already-confirmed selection stays
 *   unchanged (Req. 4.4).
 */
export interface WizardNavState {
  currentStage: StageNumber
  completed: StageNumber[]
  reselecting: boolean
}

/** Events accepted by the wizard navigation. */
export type WizardNavEvent =
  /** "Next": confirms the current stage and advances to the following one. */
  | { type: "NEXT" }
  /** "Back": returns to the previous stage, enabling the reselection. */
  | { type: "BACK" }
  /**
   * Reconciles `completed` with the Backend re-read (source of truth). The UI
   * dispatches this after each async action (complete stage, select, etc.).
   */
  | { type: "SYNC_BACKEND"; completed: number[] }

/** Normalizes a set of completed stages: only 1..6, unique, ordered. */
function normalizeCompleted(completed: number[]): StageNumber[] {
  const valid = new Set<StageNumber>()
  for (const n of completed) {
    if (STAGES.includes(n as StageNumber)) valid.add(n as StageNumber)
  }
  return [...valid].sort((a, b) => a - b)
}

/**
 * Mirrors the Backend reset rule (Req. 4.6): when reselecting/changing the stage
 * `changedStage`, all LATER stages (> `changedStage`) have their `StageRun` reset
 * to `nao_iniciado`, so they leave the set of completed. The changed stage itself
 * and the earlier ones are preserved.
 *
 * Pure and deterministic; does not mutate the input. Defensive: numbers outside
 * 1..6 in `completed` are discarded by normalization.
 */
export function resetByReselection(
  completed: number[],
  changedStage: StageNumber,
): StageNumber[] {
  return normalizeCompleted(completed).filter((n) => n <= changedStage)
}

/** Initial navigation state: Stage 1 open, nothing completed. */
export const initialNavState: WizardNavState = {
  currentStage: FIRST_STAGE,
  completed: [],
  reselecting: false,
}

/**
 * Pure, deterministic reducer of the Next/Back navigation with reselection.
 *
 * Rules (Req. 4.2–4.6):
 *
 * - `NEXT` (Req. 4.2): advances `currentStage` to the next one **only if** the
 *   Backend-derived gate allows it (`deriveStates(completed).canAdvance`), that
 *   is, the current stage is completed and is not the last. Otherwise it is a
 *   no-op (does not advance without completing).
 *   - When NOT reselecting (Req. 4.4): the already-confirmed selection stays —
 *     `completed` is unchanged; only the screen advances.
 *   - When reselecting (Req. 4.5, 4.6): adopts the new selection of the current
 *     stage and applies the Backend reset over the later ones
 *     (`resetByReselection(completed, currentStage)`), ending the reselection
 *     mode. The following stages go back to `nao_iniciado` (sequential gate
 *     restored), to be recomputed from the new selection.
 *
 * - `BACK` (Req. 4.3): returns to the previous stage (floor at {@link FIRST_STAGE},
 *   where it is a no-op) and enters reselection mode, so an eventual change +
 *   "Next" is adopted as a new selection.
 *
 * - `SYNC_BACKEND`: replaces `completed` with the Backend re-read (normalized).
 *   Does not touch `currentStage` nor `reselecting`.
 *
 * The reducer never invents completions: `completed` only changes by a
 * reselection reset (removing later ones) or by syncing with the Backend.
 */
export function wizardNavReducer(
  state: WizardNavState,
  event: WizardNavEvent,
): WizardNavState {
  switch (event.type) {
    case "NEXT": {
      const { canAdvance } = deriveStates(state.completed)
      // Backend-derived gate: only advance if the current stage is completed
      // (and is not the last). Without it, "Next" is a no-op (Req. 4.2, gate).
      if (!canAdvance(state.currentStage)) return state

      const next = (state.currentStage + 1) as StageNumber

      if (!state.reselecting) {
        // Req. 4.4: while there was no "Back", the confirmed selection persists;
        // only advance the screen.
        return { ...state, currentStage: next }
      }

      // Req. 4.5/4.6: reselection confirmed — adopt the new selection of the
      // current stage and mirror the Backend reset over the later stages.
      return {
        currentStage: next,
        completed: resetByReselection(state.completed, state.currentStage),
        reselecting: false,
      }
    }

    case "BACK": {
      // Req. 4.3: return to the previous stage (floor at the first) and enable
      // the reselection for the next confirmation.
      if (state.currentStage <= FIRST_STAGE) {
        return { ...state, reselecting: true }
      }
      return {
        ...state,
        currentStage: (state.currentStage - 1) as StageNumber,
        reselecting: true,
      }
    }

    case "SYNC_BACKEND":
      // Reconcile with the source of truth (Backend). Does not change navigation.
      return { ...state, completed: normalizeCompleted(event.completed) }

    default:
      return state
  }
}
