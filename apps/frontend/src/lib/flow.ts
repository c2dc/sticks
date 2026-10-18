/**
 * In-memory view state machine for the guided STICKS flow.
 *
 * The flow is intentionally minimal: the Landing screen and the
 * `ExecutionWizard`. Clicking "Start" on the Landing goes straight into the
 * wizard at Stage 1 — base selection (Stage 1) and campaign selection (Stage 2)
 * happen INSIDE the wizard, one screen per stage, so there are no standalone
 * base/dashboard/selection screens before it anymore.
 *
 * The reducer is pure and deterministic — exported on its own so the navigation
 * invariants can be checked by property tests without mounting the DOM.
 *
 * Terminology: a STIX `campaign` (the adversary campaign being reproduced) is
 * chosen in Stage 2 of the wizard. The chosen campaign's curated slug becomes
 * the `campaignId` that drives Stages 3..6 — that selection lives in the wizard,
 * not here, so this machine carries no campaign state.
 *
 * State diagram:
 *   landing  --START-->  wizard
 *   wizard   --BACK-->   landing
 *   landing  --BACK-->   landing   (no-op, floor)
 *
 * Any event that is not valid for the current view is a no-op: the reducer
 * returns the same state object unchanged.
 */
import { useReducer } from "react"

/** The two screens of the flow. */
export type View = "landing" | "wizard"

/** Navigation state. The wizard owns base/campaign selection internally. */
export interface FlowState {
  view: View
}

/** All events the flow accepts. */
export type FlowEvent = { type: "START" } | { type: "BACK" }

/** Initial state: the Landing screen. */
export const initialState: FlowState = { view: "landing" }

/**
 * Pure, deterministic reducer implementing exactly the state diagram above.
 *
 * `START` enters the wizard from the Landing; `BACK` returns to the Landing from
 * the wizard and is a no-op on the Landing (the floor). Events not valid for the
 * current view are no-ops (the same state object is returned).
 */
export function flowReducer(state: FlowState, event: FlowEvent): FlowState {
  switch (event.type) {
    case "START":
      return state.view === "landing" ? { view: "wizard" } : state

    case "BACK":
      return state.view === "wizard" ? { view: "landing" } : state

    default:
      return state
  }
}

/**
 * Hook wrapping `useReducer(flowReducer, initialState)`. Returns the current
 * `state` and `dispatch` (the dispatch function) for the view layer.
 */
export function useAppFlow(): {
  state: FlowState
  dispatch: (event: FlowEvent) => void
} {
  const [state, dispatch] = useReducer(flowReducer, initialState)
  return { state, dispatch }
}
