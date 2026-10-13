/**
 * In-memory view state machine for the new linear STIX flow (Req. 1.3, 3.1,
 * 3.2; design "Composição do fluxo/wizard no frontend").
 *
 * The flow is strictly linear with Avançar/Voltar, so instead of pulling in a
 * router we keep a single `useReducer` encapsulated by {@link useAppFlow}. The
 * reducer is pure and deterministic — exported on its own so the navigation
 * invariants can be checked by property tests without mounting the DOM
 * (Correctness Properties 10 and 11).
 *
 * The standalone DAG view was removed: the DAG generation/visualization is no
 * longer a separate phase before the wizard — it is now **Estágio 1** inside
 * the `ExecutionWizard` (4-stage scheme 1..4). The "pode avançar do Estágio 1"
 * gating moved into the wizard, derived from the backend (there is a
 * `dag_escolhido_id` ⇒ Estágio 1 concluded ⇒ Estágio 2 released), so it no
 * longer lives at the flow level.
 *
 * State diagram (see design.md):
 *   landing  --INICIAR-->               bases
 *   bases    --SELECIONAR_BASE(v)-->    dashboard   (store versaoSelecionada)
 *   dashboard--IR_PARA_SELECAO-->       selecao
 *   selecao  --SELECIONAR_CAMPANHA(c)-->wizard      (store campanha)
 *   wizard   --VOLTAR-->                selecao     (preserve campanha)
 *   selecao  --VOLTAR-->                dashboard
 *   dashboard--VOLTAR-->                bases
 *   bases    --VOLTAR-->                landing
 *   landing  --VOLTAR-->                landing     (no-op)
 *
 * Any event that is not valid for the current view is a no-op: the reducer
 * returns the state object unchanged.
 */
import { useReducer } from "react"

/** The five linear screens of the flow. */
export type View = "landing" | "bases" | "dashboard" | "selecao" | "wizard"

/**
 * Navigation + shared selection state. `versaoSelecionada` is set when a base
 * is chosen; `campanhaSelecionada` when a campaign is chosen (carrying the
 * resolved curated slug, Req. 4.5).
 */
export interface FlowState {
  view: View
  versaoSelecionada?: string
  campanhaSelecionada?: {
    stix_id: string
    nome: string
    slug_curado?: string | null
  }
}

/** All events the flow accepts (pt-BR names, matching the design contract). */
export type FlowEvent =
  | { tipo: "INICIAR" }
  | { tipo: "SELECIONAR_BASE"; versao: string }
  | { tipo: "IR_PARA_SELECAO" }
  | { tipo: "SELECIONAR_CAMPANHA"; campanha: FlowState["campanhaSelecionada"] }
  | { tipo: "VOLTAR" }

/** Initial state: the Landing screen. */
export const estadoInicial: FlowState = { view: "landing" }

/**
 * Pure, deterministic reducer implementing exactly the state diagram above.
 *
 * Events not valid for the current view are no-ops (the same state object is
 * returned). `VOLTAR` steps back one screen along the linear path, preserving
 * `campanhaSelecionada` on the wizard→selecao return (Req. 7.4) and being a
 * no-op at `landing`.
 */
export function flowReducer(estado: FlowState, evento: FlowEvent): FlowState {
  switch (evento.tipo) {
    case "INICIAR":
      // landing -> bases (Req. 1.3). Invalid anywhere else.
      return estado.view === "landing" ? { ...estado, view: "bases" } : estado

    case "SELECIONAR_BASE":
      // bases -> dashboard, storing the chosen STIX version.
      return estado.view === "bases"
        ? { ...estado, view: "dashboard", versaoSelecionada: evento.versao }
        : estado

    case "IR_PARA_SELECAO":
      // dashboard -> selecao.
      return estado.view === "dashboard" ? { ...estado, view: "selecao" } : estado

    case "SELECIONAR_CAMPANHA":
      // selecao -> wizard, storing the campaign. The wizard now begins at
      // Estágio 1 (Geração do DAG); advance gating lives inside the wizard,
      // derived from the backend (Req. 3.2, 6.3).
      return estado.view === "selecao"
        ? {
            ...estado,
            view: "wizard",
            campanhaSelecionada: evento.campanha,
          }
        : estado

    case "VOLTAR":
      // Step back one screen along the linear path. campanhaSelecionada is kept
      // across every transition (so wizard -> selecao preserves it, Req. 7.4);
      // landing is the floor and VOLTAR there is a no-op.
      switch (estado.view) {
        case "wizard":
          return { ...estado, view: "selecao" }
        case "selecao":
          return { ...estado, view: "dashboard" }
        case "dashboard":
          return { ...estado, view: "bases" }
        case "bases":
          return { ...estado, view: "landing" }
        case "landing":
          return estado
        default:
          return estado
      }

    default:
      return estado
  }
}

/**
 * Hook wrapping `useReducer(flowReducer, estadoInicial)`. Returns the current
 * `estado` and `enviar` (the dispatch function) for the view layer.
 */
export function useAppFlow(): { estado: FlowState; enviar: (evento: FlowEvent) => void } {
  const [estado, enviar] = useReducer(flowReducer, estadoInicial)
  return { estado, enviar }
}
