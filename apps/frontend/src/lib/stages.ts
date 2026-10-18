/**
 * Typed API wrappers and real-time helpers for the per-stage screens
 * (Stage1View / Stage2View / Stage3View — Task 12.2, Req. 2, 3, 4, 11).
 *
 * These wrap the stage-detail endpoints and the operation WebSocket. The
 * wrapper and interface names (`getStage1`/`Stage1Result` = Modeling,
 * `getStage2`/`Stage2Result` = Operationalization, `getStage3`/`Stage3Result` =
 * Execution) describe the *role/shape of the data*, not the URL number; the URLs
 * point at the correct backend routes.
 * - `GET  /api/campaigns/{campaign}/stage/1`          -> {@link getStage1} -> {@link Stage1Result} (Structural Modeling)
 * - `POST /api/campaigns/{campaign}/stage/1/execute`  -> {@link executeStage1} -> {@link Stage1Result}
 * - `GET  /api/campaigns/{campaign}/stage/3`          -> {@link getStage2} -> {@link Stage2Result} (Operationalization)
 * - `POST /api/campaigns/{campaign}/stage/3/complete` -> {@link completeStage3} -> {@link CaseStageStates}
 * - `GET  /api/campaigns/{campaign}/stage/6`          -> {@link getStage3} -> {@link Stage3Result} (Campaign Execution)
 * - `GET  /api/campaigns/{campaign}/stage/4`          -> {@link getTopology} -> {@link TopologyResponse} (Provisioning)
 * - `WS   /ws/operation/{operation}`                  -> {@link OperationFrame}
 *
 * `src/lib/campaigns.ts` (task 12.1) owns the `GET /api/campaigns` + stage-progress
 * channel wrappers and the shared `StageState` / `StageNumber` types.
 * This module lives beside it and extends the surface with the stage-detail
 * contracts without editing the existing exports. Backend contracts are the
 * source of truth (see the pipeline-ui design).
 */
import { api } from "@/lib/api"
import type { CaseStageStates, StageState } from "@/lib/campaigns"
import type { DagView } from "@/lib/dag"
import type { EmulationPreview, EmulationAbilityResult } from "@/lib/emulation"
import type { BaseStixInfo, CampaignStix, DashboardStix } from "@/lib/stix"
import { connectWs, type WsConnection } from "@/lib/ws"

// ---------------------------------------------------------------------------
// Translation source — explicit, extensible attribute (Req. 11.1, 11.2).
// ---------------------------------------------------------------------------

/**
 * Origin of a Stage-2 translation. `"curadoria_humana"` is the value for the 8
 * curated campaigns; the union stays open (`string & {}`) so a future AI-assisted
 * origin can be shown without changing the persisted model (Req. 11.1, 11.2).
 */
export type TranslationSource = "curadoria_humana" | (string & {})

// ---------------------------------------------------------------------------
// Structural Modeling — Stage 1 (Req. 2).
// ---------------------------------------------------------------------------

/**
 * Identity block of the STIX campaign shown in the Stage 1 Input (a slice of the
 * `campaign` object of the STIX package of the selected base — Req. 1.1, 2.1).
 * Mirrors the backend `CampaignStixInput` DTO. All rich fields are
 * optional/nullable; only `found` is guaranteed.
 *
 * When `found` is `false`, `unavailable_reason` carries the fallback reason —
 * `"versao_ausente"` (base version not provided), `"bundle_indisponivel"`
 * (unreadable/invalid base) or `"campanha_nao_encontrada"` (`campaign` object
 * absent from the base). The union stays open (`string & {}`) for new reasons
 * without breaking the contract (Req. 4.2, 4.3).
 */
export interface CampaignStixInput {
  /** `true` iff the `campaign` object was located in the selected base. */
  found: boolean
  /** STIX base version used for the resolution; `null` when not provided. */
  base_version: string | null
  /** STIX id of the object (`campaign--…`); `null` when not found. */
  stix_id: string | null
  /** Canonical campaign name; preserved on fallback as the identifier. */
  name: string | null
  /** ATT&CK external id (e.g. `C0045`); preserved on fallback. */
  external_id: string | null
  /** Readable campaign description; `null` when absent. */
  description: string | null
  /** Known campaign aliases (chips in the UI); empty when absent. */
  aliases: string[]
  /** First observation (ISO); `null` when absent. */
  first_seen: string | null
  /** Last observation (ISO); `null` when absent. */
  last_seen: string | null
  /** Fallback reason when `!found`; `null` when found. */
  unavailable_reason:
    | "versao_ausente"
    | "bundle_indisponivel"
    | "campanha_nao_encontrada"
    | (string & {})
    | null
}

/**
 * Stage-1 Input: the identity of the STIX campaign of the selected base (Req. 2.1).
 *
 * The UI consumes `campaign_stix` (the slice of the `campaign` object); the
 * legacy `dag_file`/`stix_reference`/`campaign_name` fields remain in the backend
 * DTO for contract compatibility, but are no longer displayed/consumed by the UI
 * — hence optional here (Req. 1.2, 1.3).
 */
export interface Stage1Input {
  campaign_id: string
  name: string
  /** @deprecated Caldera legacy, no longer displayed by the UI. */
  dag_file?: string
  /** @deprecated Legacy, no longer consumed by the UI (replaced by `campaign_stix`). */
  stix_reference?: string
  /** @deprecated Legacy, no longer consumed by the UI. */
  campaign_name?: string
  /** Slice of the `campaign` object of the STIX package of the selected base (Req. 2.1). */
  campaign_stix?: CampaignStixInput | null
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
  type?: string
  [key: string]: unknown
}

/**
 * Stage-1 Output: the structural elements extracted from the STIX dataset
 * (Req. 2.3). When `is_empty` is true, no structural elements were extracted
 * and the UI shows the corresponding indication (Req. 2.5).
 */
export interface Stage1Output {
  techniques: Stage1Technique[]
  relationships: Stage1Relationship[]
  indicators: unknown[]
  infrastructure: unknown[]
  malware: unknown[]
  metadata: Record<string, unknown>
  is_empty: boolean
  note?: string | null
}

/** Response of `GET /api/campaigns/{campaign}/stage/1` (Structural Modeling). */
export interface Stage1Result {
  campaign_id: string
  state: StageState
  input: Stage1Input
  /** Null until the stage actually runs — the read-only GET never fabricates it. */
  output: Stage1Output | null
  /** Failure cause when `state === "erro"` (Req. 2.6). */
  error_message?: string | null
  /** Modeling step at which the failure occurred (Req. 2.6). */
  failed_step?: string | null
}

/**
 * Fetch the Structural Modeling Input/Output for a campaign — READ-ONLY.
 *
 * Structural Modeling is **Stage 1** (the pipeline's starting point), so this
 * wrapper targets `GET /api/campaigns/{campaign}/stage/1`. The read is read-only:
 * opening the screen does not run or persist the modeling (Req. 2.5).
 *
 * When `version` (the STIX base selected in the flow) is provided, it appends
 * `?version=<encoded>` so the backend slices the `campaign` object of the correct
 * base in the Input. Backward-compatible: without `version`, the backend applies
 * the fallback (`versao_ausente`) without error (Req. 5.2, 5.3).
 */
export function getStage1(campaign: string, version?: string): Promise<Stage1Result> {
  const base = `/campaigns/${encodeURIComponent(campaign)}/stage/1`
  const url = version ? `${base}?version=${encodeURIComponent(version)}` : base
  return api.get<Stage1Result>(url)
}

/**
 * Trigger the Structural Modeling (Stage 1) for a campaign
 * (`POST /api/campaigns/{campaign}/stage/1/execute`). Persists the extracted
 * elements and marks the stage `concluido` on success (or `erro` with the
 * failing step — reported in the typed {@link Stage1Result}, not as an HTTP error).
 *
 * Accepts the same optional `version` as {@link getStage1}: when provided, it
 * appends `?version=<encoded>` to enrich the result's Input with the STIX campaign
 * slice of the selected base (without affecting the structural extraction).
 * Backward-compatible when omitted (Req. 5.2).
 */
export function executeStage1(campaign: string, version?: string): Promise<Stage1Result> {
  const base = `/campaigns/${encodeURIComponent(campaign)}/stage/1/execute`
  const url = version ? `${base}?version=${encodeURIComponent(version)}` : base
  return api.post<Stage1Result>(url)
}

// ---------------------------------------------------------------------------
// Operationalization — Stage 3 role (Req. 4, 11).
// ---------------------------------------------------------------------------

/** Stage-2 Input: the abstract ATT&CK techniques from Stage 1 (Req. 3.1). */
export interface Stage2Input {
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
  translation_source?: TranslationSource
}

/** Adversary grouping of Abilities with its atomic ordering (Req. 3.3, 3.5). */
export interface Adversary {
  id: string
  name: string
  description: string
  /** Sequential ordering / dependencies of the grouped Abilities (Req. 3.3). */
  atomic_ordering: string[]
  /** Translation origin at the Adversary level (Req. 11.1). */
  translation_source?: TranslationSource
}

/**
 * Stage-2 Output: the curated Abilities, their ordering, the Adversary grouping
 * and the explicit translation-source indicator (Req. 3.2–3.5, 11.1). Absence
 * states are conveyed by `has_abilities` / `has_adversary` (Req. 3.7, 3.8).
 */
export interface Stage2Output {
  abilities: Ability[]
  /** Sequential order / dependencies across the Abilities (Req. 3.3). */
  ordering: string[]
  adversary: Adversary | null
  /** Explicit, extensible translation-source attribute (Req. 3.4, 11.1). */
  translation_source: TranslationSource
  /** Human-curation flag associated with the Output (Req. 3.4). */
  human_curation: boolean
  /** False when the campaign has no curated Abilities (Req. 3.7). */
  has_abilities: boolean
  /** False when the campaign has no associated Adversary (Req. 3.8). */
  has_adversary: boolean
  note?: string | null
  adversary_note?: string | null
}

/** Response of `GET /api/campaigns/{campaign}/stage/3` (Operationalization). */
export interface Stage2Result {
  campaign_id: string
  input: Stage2Input
  output: Stage2Output
  /** True when the Modeling produced no techniques -> Output suppressed (Req. 3.6). */
  suppressed: boolean
}

/**
 * Fetch the Operationalization Input/Output for a campaign.
 *
 * Operationalization is **Stage 3** (abstract techniques → executable Abilities),
 * so this wrapper targets `GET /api/campaigns/{campaign}/stage/3`. The type
 * `Stage2Result` describes the *data shape* of the Operationalization; the
 * `translation_source`/`TranslationSource` vocabulary is preserved (Req. 4.3).
 */
export function getStage2(campaign: string): Promise<Stage2Result> {
  return api.get<Stage2Result>(`/campaigns/${encodeURIComponent(campaign)}/stage/3`)
}

/**
 * Complete the Operationalization (Stage 3) and unblock Stage 4
 * (`POST /api/campaigns/{campaign}/stage/3/complete`). Respects the sequential
 * gate (requires Stage 2 completed; otherwise the backend responds 409). Returns
 * the per-stage state ({@link CaseStageStates}) already with Stage 3 `concluido`.
 */
export function completeStage3(campaign: string): Promise<CaseStageStates> {
  return api.post<CaseStageStates>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/3/complete`,
  )
}

// ---------------------------------------------------------------------------
// Campaign Execution — Stage 5 role (Req. 6).
// ---------------------------------------------------------------------------

/** Per-Ability execution status during / after an Operation (Req. 4.3). */
export type AbilityResultStatus = "pendente" | "em_execucao" | "sucesso" | "falha"

/** Stage-3 Input: the Adversary + Abilities to emulate (Req. 6.1). */
export interface Stage3Input {
  campaign_id: string
  name: string
  adversary: Adversary | null
  abilities: Ability[]
  /** False when the campaign cannot be emulated (e.g. missing Adversary/Abilities). */
  can_emulate: boolean
  note?: string | null
}

/** Result of a single Ability within an Operation (Req. 4.4). */
export interface AbilityResult {
  ability_id: string
  status: AbilityResultStatus
  command_output: string
}

/** Overall Operation state (from `GET /api/campaigns/{campaign}/stage/6`). */
export type OperationState =
  | "nao_iniciada"
  | "em_execucao"
  | "concluida"
  | "falha"
  | (string & {})

/**
 * A view of one Operation for the campaign: its state, per-Ability results and
 * the aggregate success/failure totals (Req. 4.4, 4.5).
 */
export interface OperationResultView {
  operation_id: string
  state: OperationState
  total_success: number
  total_failure: number
  results: AbilityResult[]
}

/** Response of `GET /api/campaigns/{campaign}/stage/6` (Campaign Execution). */
export interface Stage3Result {
  input: Stage3Input
  output: OperationResultView[]
}

/**
 * Fetch the Execution Input (Adversary + Abilities) + accumulated Operation
 * results for a campaign.
 *
 * Execution, measurement and report is **Stage 6** (real emulation against the
 * environment provisioned in Stage 5), so this wrapper targets
 * `GET /api/campaigns/{campaign}/stage/6`. The type `Stage3Result` describes the
 * *data shape* of the Execution.
 */
export function getStage3(campaign: string): Promise<Stage3Result> {
  return api.get<Stage3Result>(`/campaigns/${encodeURIComponent(campaign)}/stage/6`)
}

// ---------------------------------------------------------------------------
// Operation real-time channel — `WS /ws/operation/{operation}` (Req. 4.3–4.5).
// ---------------------------------------------------------------------------

/**
 * A per-Ability status frame pushed by `WS /ws/operation/{operation}` while an
 * Operation runs (Req. 4.3, 4.4). Carries the Ability id, its new status and
 * the command output produced so far.
 */
export interface OperationAbilityFrame {
  type: "ability"
  ability_id: string
  status: AbilityResultStatus
  command_output: string
}

/**
 * The final aggregate frame pushed when the Operation reaches its terminal
 * state (Req. 4.5): success/failure totals and the Operation's final state.
 */
export interface OperationAggregateFrame {
  type: "aggregate"
  operation_id: string
  state: OperationState
  total_success: number
  total_failure: number
}

/** Any frame carried by the Operation channel. */
export type OperationFrame = OperationAbilityFrame | OperationAggregateFrame

/**
 * Subscribe to the per-Operation channel. `onFrame` receives per-Ability status
 * updates and the final aggregate (Req. 4.3, 4.4, 4.5). Reconnects on drop so
 * the UI keeps reflecting real-time status.
 */
export function connectOperation(
  operation: string,
  onFrame: (frame: OperationFrame) => void,
): WsConnection {
  return connectWs<OperationFrame>(
    `/ws/operation/${encodeURIComponent(operation)}`,
    { onMessage: onFrame },
  )
}

// ---------------------------------------------------------------------------
// Environment Provisioning — Stage 4 (read-only): contained lab topology +
// isolation state (Req. 5.4, 5.5, 5.6).
// ---------------------------------------------------------------------------

/** One lab host in the contained topology. */
export interface TopologyHost {
  name: string
  container: string
  role: "attacker" | "target" | (string & {})
  ips: string[]
}

/** One internal-only lab network and the hosts attached to it. */
export interface TopologyNetwork {
  name: string
  subnet: string
  internal: boolean
  hosts: string[]
}

/**
 * The contained lab topology for a campaign plus a ready-to-render Mermaid
 * diagram. `isolated` is true when every attack-plane network is `internal:
 * true` (no route to the host or the internet) — the attack never leaves the
 * sandbox.
 */
export interface TopologyResponse {
  campaign_id: string
  name: string
  isolated: boolean
  hosts: TopologyHost[]
  networks: TopologyNetwork[]
  mermaid: string
  compose_file?: string | null
}

/**
 * Fetch the Environment Provisioning view (Stage 4): the contained lab topology
 * (+ Mermaid) and its isolation state (Req. 5.4, 5.5, 5.6).
 *
 * The topology + isolation is served by `GET /api/campaigns/{campaign}/topology`.
 * The backend payload `Stage4ProvisioningResponse` is richer than
 * {@link TopologyResponse} (it adds `isolation_unavailable`, `reason` and
 * `states`); this wrapper exposes the subset typed here — the dedicated
 * `EnvironmentStageView` screen (task 10.2/10.3) consumes the extra fields.
 */
export function getTopology(campaign: string): Promise<TopologyResponse> {
  // The contained topology (networks/hosts/mermaid) is served by the dedicated
  // topology route, which always returns a full `TopologyResponse` with
  // `networks`. The per-stage endpoints (e.g. Stage 4 Operationalization) have
  // NO `networks` field, so targeting them would make Stage3View crash on
  // `topology.networks.map`; pointing here keeps the topology view working.
  return api.get<TopologyResponse>(
    `/campaigns/${encodeURIComponent(campaign)}/topology`,
  )
}

/**
 * Full Environment Provisioning response (Stage 4), served by
 * `GET /api/campaigns/{campaign}/stage/4` (backend `Stage4ProvisioningResponse`).
 *
 * It is a structural superset of {@link TopologyResponse}: besides the contained
 * topology (hosts, internal networks, Mermaid, compose), it carries the rich
 * isolation state the dedicated `EnvironmentStageView` screen consumes:
 *
 * - `isolated` — `true` iff **all** target containers are isolated → Stage 4
 *   auto-completes and Stage 5 is unblocked.
 * - `isolation_unavailable` — `true` when isolation **could not be verified**
 *   (Docker daemon unavailable). *Fail-closed*: the stage does **not** complete
 *   and Stage 5 stays blocked, but the topology is still shown.
 * - `reason` — reason when not isolated / not verifiable; `null` when `isolated`.
 * - `states` — the state of the stages ({@link CaseStageStates}), already
 *   reflecting the auto-completion (or not) of Stage 4.
 * - `health` — health summary of the target containers ({@link HealthSummary}),
 *   for the Stage 5 prerequisites checklist. Read-only: it comes from the
 *   persisted {@link ProvisionedEnvironment} Output, or an empty summary (not
 *   approved) when there is no Output yet — opening the screen never starts any
 *   container (Req. 6.1).
 * - `logs` — current buffer of the provisioning log lines
 *   ({@link LogLineView}, with monotonic `seq`) to recover the live terminal when
 *   mounting the screen (Req. 2.5). Empty for stages without provisioning.
 */
export interface ProvisioningResponse {
  campaign_id: string
  name: string
  /** `true` iff all target containers are isolated → ready for Stage 5. */
  isolated: boolean
  /** `true` when isolation could not be verified (fail-closed). */
  isolation_unavailable: boolean
  /** Reason when not isolated / not verifiable; `null` when isolated. */
  reason: string | null
  hosts: TopologyHost[]
  networks: TopologyNetwork[]
  mermaid: string
  compose_file?: string | null
  /** State of the stages, reflecting the auto-completion of Stage 4. */
  states: CaseStageStates
  /** Health summary of the target containers for the checklist (Req. 6.1). */
  health: HealthSummary
  /** Current buffer of the provisioning log lines (Req. 2.5). */
  logs: LogLineView[]
}

/**
 * Fetch the full Environment Provisioning response (Stage 4) — the contained
 * topology **plus** the rich isolation state (Req. 5.4, 5.5, 5.6).
 *
 * Targets `GET /api/campaigns/{campaign}/stage/4` (same endpoint as
 * {@link getTopology}), but typed for the full {@link ProvisioningResponse}
 * payload. Prefer this wrapper in the dedicated `EnvironmentStageView` screen;
 * {@link getTopology}/{@link TopologyResponse} remain for the subset used by
 * Stage3View.
 */
export function getProvisioning(campaign: string): Promise<ProvisioningResponse> {
  return api.get<ProvisioningResponse>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/4`,
  )
}

// ===========================================================================
// SIX-stage pipeline — typed wrappers of the endpoints (task 13.1).
//
// In the six-stage scheme (Req. 1.1, 1.2, 1.7): 1 = STIX base selection,
// 2 = Campaign selection, 3 = DAG generation, 4 = Operationalization,
// 5 = Simulation environment creation, 6 = Execution, measurement and report.
//
// These wrappers and types mirror the backend contracts (source of truth):
// `app/api/campaigns.py` + the typed DTOs in `app/services/stix/*` and
// `app/services/environment/*`. They coexist with the historical wrappers above
// (`getStage1`/`getStage2`/`getStage3`) without editing them.
// ===========================================================================

// ---------------------------------------------------------------------------
// Stage 1 — STIX base selection (Req. 5.1, 5.2, 5.7, 5.8, 2.2).
// ---------------------------------------------------------------------------

/**
 * A STIX base in the Stage 1 listing, with the curated-version flag
 * (backend `BaseStixListada`, Req. 5.5, 5.6). Extends {@link BaseStixInfo} with
 * `curated`: `true` only for the version that carries the 8 curated campaigns.
 */
export interface ListedBaseStix extends BaseStixInfo {
  /** `true` iff the version is the curated version (carries the 8 campaigns). */
  curated: boolean
}

/**
 * Typed result of Stage 1 "STIX base selection"
 * (backend `Stage1ResultStix`, Req. 5.8, 5.10, 2.2).
 *
 * `completed` is `true` **only when** the version bundle is on disk and the
 * dashboard was assembled; `false` on any failure (keeping the stage not
 * completed, with `error_message`). The Output (bundle on disk + dashboard +
 * campaigns) is the Stage 2 Input (Req. 2.2).
 */
export interface Stage1ResultStix {
  version: string
  completed: boolean
  bundle_on_disk: boolean
  dashboard: DashboardStix | null
  campaigns: CampaignStix[]
  curated: boolean
  error_message?: string | null
}

/**
 * Download the STIX package of `version` when absent — the only network egress
 * of Stage 1 (`POST /api/stix/bases/{version}/download`, Req. 5.2, 5.3). Returns
 * the updated base info (`downloaded=true`); failures surface as `ApiError`
 * (422 unknown version, 502 network/content/write failure — Req. 5.9).
 */
export function downloadBaseStix(version: string): Promise<BaseStixInfo> {
  return api.post<BaseStixInfo>(
    `/stix/bases/${encodeURIComponent(version)}/download`,
  )
}

/**
 * Stage 1 result (STIX base selection): bundle dashboard + completion verdict
 * (`GET /api/campaigns/{campaign}/stage/1`, Req. 5.1, 5.7, 5.8).
 *
 * When `version` is provided, it appends `?version=<encoded>`; omitted, the
 * backend applies the default curated version (Req. 5.5). Read-only.
 */
export function getStageBaseStix(
  campaign: string,
  version?: string,
): Promise<Stage1ResultStix> {
  const base = `/campaigns/${encodeURIComponent(campaign)}/stage/1`
  const url = version ? `${base}?version=${encodeURIComponent(version)}` : base
  return api.get<Stage1ResultStix>(url)
}

// ---------------------------------------------------------------------------
// Stage 2 — Campaign selection (Req. 6.1–6.7, 2.3).
// ---------------------------------------------------------------------------

/**
 * A bundle campaign in the Stage 2 listing, with the selection flag
 * (backend `ListedCampaign`, Req. 6.2, 6.5). Extends {@link CampaignStix} with
 * `selectable`: `true` only for curated campaigns (which depend on curated
 * artifacts on disk).
 */
export interface ListedCampaign extends CampaignStix {
  /** `true` iff the campaign is selectable (curated, with artifacts on disk). */
  selectable: boolean
}

/**
 * An ATT&CK technique extracted from the selected campaign
 * (backend `TechniqueElement`, Req. 6.4, 2.3).
 */
export interface TechniqueElement {
  technique_id: string
  technique_name?: string | null
  tactic?: string | null
  description?: string | null
  node_id?: string | null
}

/**
 * Typed result of selecting a campaign in Stage 2
 * (backend `CampaignSelectionResult`, Req. 6.3–6.7).
 *
 * `completed` is `true` **only when** the attribute extraction succeeded
 * (identity + techniques); `false` on any failure (keeping the stage not
 * completed, with `error_message`/`failed_step`). The Output (identity +
 * techniques) is the Stage 3 Input (Req. 2.3). `identity` reuses the historical
 * {@link CampaignStixInput} block.
 */
export interface CampaignSelectionResult {
  campaign_id: string
  version?: string | null
  completed: boolean
  identity: CampaignStixInput | null
  techniques: TechniqueElement[]
  error_message?: string | null
  failed_step?: string | null
}

/**
 * Stage 2 "Campaign selection" view: bundle campaigns + the read-only current
 * selection (`GET /api/campaigns/{campaign}/stage/2`, backend `Stage2Response`,
 * Req. 6.2, 6.4). Read-only: opening the screen never executes.
 */
export interface Stage2Response {
  campaign_id: string
  version?: string | null
  campaigns: ListedCampaign[]
  selection: CampaignSelectionResult
}

/**
 * List the bundle campaigns + the current Stage 2 selection
 * (`GET /api/campaigns/{campaign}/stage/2`, Req. 6.2, 6.3, 6.4). With `version`
 * it builds `?version=<encoded>`; omitted, the backend applies the default
 * curated version.
 */
export function getStageCampaign(
  campaign: string,
  version?: string,
): Promise<Stage2Response> {
  const base = `/campaigns/${encodeURIComponent(campaign)}/stage/2`
  const url = version ? `${base}?version=${encodeURIComponent(version)}` : base
  return api.get<Stage2Response>(url)
}

/** Body of `POST /api/campaigns/{campaign}/stage/2/select` (Req. 6.3). */
export interface SelectCampaignRequest {
  slug: string
  version?: string | null
}

/**
 * Select a campaign and extract its attributes — completes Stage 2 when the
 * extraction succeeds (`POST /api/campaigns/{campaign}/stage/2/select`,
 * Req. 6.3, 6.4, 6.6). An extraction failure surfaces in the typed result
 * (`completed=false`), not as an HTTP error (Req. 6.7).
 */
export function selectCampaign(
  campaign: string,
  slug: string,
  version?: string,
): Promise<CampaignSelectionResult> {
  const body: SelectCampaignRequest = { slug }
  if (version !== undefined) body.version = version
  return api.post<CampaignSelectionResult>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/2/select`,
    body,
  )
}

// ---------------------------------------------------------------------------
// Stage 3 — DAG generation (Req. 7.2, 7.3).
// ---------------------------------------------------------------------------

/**
 * Stage 3 "DAG generation" view: structural graph + Mermaid of the curated DAG
 * (`GET /api/campaigns/{campaign}/stage/3`, backend `DagView`, Req. 7.2, 7.3).
 * Read-only — no network/AI on any path (Req. 7.4, 11.1).
 */
export function getStageDag(campaign: string): Promise<DagView> {
  return api.get<DagView>(`/campaigns/${encodeURIComponent(campaign)}/stage/3`)
}

// ---------------------------------------------------------------------------
// Stage 4 — Operationalization: Abilities + EnvironmentParameters (Req. 8.3,
// 8.4, 8.7, 17.1, 17.2).
// ---------------------------------------------------------------------------

/**
 * Environment platform — container today; VM admitted without redesign
 * (backend `EnvironmentPlatform`, Req. 17.1, 17.2). The union stays open
 * (`string & {}`) for new values without breaking the contract (Req. 17.2).
 */
export type EnvironmentPlatform = "conteiner" | "maquina_virtual" | (string & {})

/**
 * A target host derived from the curated DAG commands/nodes
 * (backend `TargetHost`, Req. 8.3). `address`/`container` are resolved from the
 * `attacker_commands` destinations; `vulnerabilities` come from the node's
 * `target_vuln_host_*`.
 */
export interface TargetHost {
  label: string
  address?: string | null
  container?: string | null
  host_type?: string | null
  vulnerabilities: string[]
}

/**
 * A service/vulnerability to provision, inferred from the commands
 * (backend `RequiredService`, Req. 8.3).
 */
export interface RequiredService {
  description: string
  origin_technique?: string | null
}

/**
 * Environment parameters — 2nd Output of Stage 4 (Operationalization), destined
 * for Stage 5 (backend `EnvironmentParameters`, Req. 8.3, 8.4, 8.7). Derived from
 * the curated DAG nodes (hosts/services/`network_context`); the `origin` is
 * explicit and extensible ({@link TranslationSource}, Req. 16.2, 16.4).
 */
export interface EnvironmentParameters {
  campaign_id: string
  platform: EnvironmentPlatform
  hosts: TargetHost[]
  services: RequiredService[]
  network_context: string[]
  origin: TranslationSource
}

/**
 * Stage 4 "Operationalization" view: the two separate typed artifacts — ordered
 * Abilities ({@link Stage2Output}) + {@link EnvironmentParameters}
 * (`GET /api/campaigns/{campaign}/stage/4`, backend `Stage4Response`, Req. 8.4,
 * 8.7). `abilities` is `null` when the operationalization is suppressed (no
 * upstream technique — Req. 8.1). `both_produced` is the completion criterion
 * (Req. 8.8); the completion itself is {@link completeOperationalization}.
 * Read-only.
 */
export interface Stage4Response {
  campaign_id: string
  abilities: Stage2Output | null
  parameters: EnvironmentParameters
  origin: TranslationSource
  both_produced: boolean
  states: CaseStageStates
}

/**
 * Stage 4 (Operationalization) view: ordered Abilities + `EnvironmentParameters`
 * (`GET /api/campaigns/{campaign}/stage/4`, Req. 8.4, 8.7). Read-only — opening
 * the screen never completes the stage.
 */
export function getStageOperationalization(
  campaign: string,
): Promise<Stage4Response> {
  return api.get<Stage4Response>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/4`,
  )
}

/**
 * Complete the Operationalization (Stage 4) when both artifacts — ordered
 * Abilities + `EnvironmentParameters` — have been produced
 * (`POST /api/campaigns/{campaign}/stage/4/complete`, Req. 8.7, 8.8). Respects
 * the sequential gate (requires Stage 3 completed; otherwise 409). Returns the
 * per-stage state ({@link CaseStageStates}) already with Stage 4 `concluido`.
 */
export function completeOperationalization(
  campaign: string,
): Promise<CaseStageStates> {
  return api.post<CaseStageStates>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/4/complete`,
  )
}

// ---------------------------------------------------------------------------
// Stage 5 — Environment creation: topology + isolation + health + logs
// (Req. 2.5, 6.1, 9.6–9.9, 14.1–14.4, 16.3).
// ---------------------------------------------------------------------------

/**
 * Health verdict of **one** target container
 * (backend `HealthVerdict`, Req. 5.1, 6.1). `running` is `true` iff the container
 * is in the `running` state; `healthy` is `true` when the `healthcheck` reports
 * `healthy`, `false` when `unhealthy`/`starting`, and `null` when the service
 * does **not** define a `healthcheck` (no health to require — does not fail by
 * itself). `reason` carries the readable cause when the container is not healthy;
 * `null` when approved.
 */
export interface HealthVerdict {
  container: string
  /** `true` iff the container is in the `running` state (Req. 5.1). */
  running: boolean
  /** `true` = `healthy`; `false` = `unhealthy`/`starting`; `null` = no healthcheck. */
  healthy: boolean | null
  /** Cause when not healthy (not `running`/`unhealthy`); `null` when approved. */
  reason: string | null
}

/**
 * Typed health summary — per-container + overall verdict
 * (backend `HealthSummary`, Req. 5.1, 6.1). `approved` is `true` iff **all**
 * target containers are healthy (fail-closed: an empty list fails). Composes the
 * Stage 5 prerequisites checklist.
 */
export interface HealthSummary {
  approved: boolean
  verdicts: HealthVerdict[]
}

/**
 * A provisioning log line of Stage 5
 * (backend `LogLineView`, Req. 2.5, 2.6). Each line carries a monotonic `seq`
 * per campaign (for dedupe/reorder on reconnect), the pipeline `step` that
 * emitted it, the `stream` it belongs to, the `text` and `at` — an ISO timestamp.
 *
 * `stream` stays open (`string & {}`) for new values without breaking the
 * contract; the known ones are `comando` | `stdout` | `stderr` | `info`.
 */
export interface LogLineView {
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
 * Isolation verdict of **one** target container
 * (backend `ContainerVerdict`, Req. 9.8, 14.3, 14.4). `isolated` is `true` iff the
 * container passed all checks; `reasons` explains the failure.
 */
export interface ContainerVerdict {
  container: string
  isolated: boolean
  reasons: string[]
  external_networks: string[]
  external_dns: string[]
}

/**
 * Typed isolation summary — per-container + overall verdict
 * (backend `IsolationSummary`, Req. 9.8, 14.3). `approved` is `true` iff **all**
 * target containers are isolated (fail-closed: an empty list fails).
 */
export interface IsolationSummary {
  approved: boolean
  verdicts: ContainerVerdict[]
}

/**
 * Stage 5 Output — environment ready and isolated
 * (backend `ProvisionedEnvironment`, Req. 6.1, 9.8). Stage 5 only completes when
 * `health.approved`, `isolation.approved` **and** `loaded_abilities` are `true`
 * (Req. 9.6, 9.9); otherwise Stage 6 stays blocked. The `origin` is extensible
 * (Req. 16.3, 16.4).
 *
 * When the pipeline fails, `failed_step` names the step that failed
 * (`"subir_conteineres"` | `"aguardar_saude"` | …) and `error_message` carries
 * the readable cause (consistent with the screen's console/checklist — Req. 7.5).
 */
export interface ProvisionedEnvironment {
  campaign_id: string
  platform: EnvironmentPlatform
  target_containers: string[]
  compose_file?: string | null
  /** Health summary of the target containers (`running` + `healthy`) (Req. 6.1). */
  health: HealthSummary
  isolation: IsolationSummary
  loaded_abilities: boolean
  origin: TranslationSource
  /** Pipeline step that failed (`null` when completed without failure). */
  failed_step?: string | null
  /** Readable failure cause (`null` when completed without failure). */
  error_message?: string | null
}

/**
 * Stage 5 "Environment creation" view: contained topology + isolation state
 * (`GET /api/campaigns/{campaign}/stage/5`, backend `Stage4ProvisioningResponse`,
 * Req. 9.6, 9.7). Reuses the shape of {@link ProvisioningResponse}, now under
 * number 5 of the six-stage scheme.
 */
export function getStageEnvironment(
  campaign: string,
): Promise<ProvisioningResponse> {
  return api.get<ProvisioningResponse>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/5`,
  )
}

/**
 * Response of the current provisioning log buffer of Stage 5
 * (`GET /api/campaigns/{campaign}/stage/5/logs`, backend `Stage5LogsResponse`,
 * Req. 2.5). Returns the **current** buffer of {@link LogLineView} in `seq` order
 * — ephemeral by design (live follow + reconnect), not a persistent history.
 */
export interface Stage5LogsResponse {
  campaign_id: string
  /** Current log lines (`seq`, `step`, `stream`, `text`, `at`) by `seq`. */
  logs: LogLineView[]
}

/**
 * Fetch the current provisioning log-line buffer of Stage 5 — READ-ONLY
 * (`GET /api/campaigns/{campaign}/stage/5/logs`, Req. 2.5, 2.7).
 *
 * The `ProvisioningConsole` calls this wrapper on mount to fill the terminal
 * history before the `WS /ws/stages/{campaign}` channel continues live. Aligned
 * with {@link getStageEnvironment}, whose payload already embeds the same buffer
 * in `logs` for the screen's initial read.
 */
export function getStage5Logs(campaign: string): Promise<Stage5LogsResponse> {
  return api.get<Stage5LogsResponse>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/5/logs`,
  )
}

/**
 * Provision/verify the curated environment of Stage 5: resolve the target
 * containers, load the ordered Abilities into Caldera and **verify** isolation
 * (read-only) before completing
 * (`POST /api/campaigns/{campaign}/stage/5/provision`, Req. 9.6, 9.8, 9.9). Does
 * not complete without approved isolation + loaded Abilities.
 */
export function provisionEnvironment(
  campaign: string,
): Promise<ProvisionedEnvironment> {
  return api.post<ProvisionedEnvironment>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/5/provision`,
  )
}

// ---------------------------------------------------------------------------
// Stage 6 — Execution, measurement and report (Req. 10.x, 12, 13, 15.x).
// ---------------------------------------------------------------------------

/**
 * Typed result of the environment teardown at the end of Stage 6
 * (backend `DeprovisioningResult`, Req. 10.8, 12.1, 12.3). The teardown is
 * **always** invoked; on failure `remaining_resources` identifies what remained.
 */
export interface DeprovisioningResult {
  deprovisioned: boolean
  remaining_resources: string[]
  origin: TranslationSource
  message: string
}

/**
 * Per-technique/DAG-node measurement (backend `TechniqueMetric`, Req. 15.2, 15.4,
 * 15.5). `status` reuses {@link AbilityResultStatus}; `failed_step` carries the
 * DAG step where it failed; `telemetry` are the observed signals.
 */
export interface TechniqueMetric {
  technique_id?: string | null
  node_id?: string | null
  status: AbilityResultStatus
  failed_step?: string | null
  execution_time_s?: number | null
  telemetry: string[]
}

/**
 * Procedural-sufficiency metrics — initial proposal
 * (backend `SufficiencyMetrics`, Req. 15.1–15.6). `success_rate` and
 * `procedural_coverage` ∈ [0, 1]; `failed_nodes` are the nodes/techniques that
 * failed; `success_verdict` is the overall verdict.
 */
export interface SufficiencyMetrics {
  total_dag_techniques: number
  successful_techniques: number
  success_rate: number
  procedural_coverage: number
  total_time_s?: number | null
  per_technique: TechniqueMetric[]
  failed_nodes: string[]
  success_verdict: boolean
}

/** Per-Ability result reused in the report (backend `Stage3AbilityResult`). */
export type ReportAbilityResult = AbilityResult

/**
 * Stage 6 Output — consolidated report (backend `ExecutionReport`, Req. 10.6,
 * 10.7, 15.7): the {@link SufficiencyMetrics}, the per-Ability results, the
 * deprovisioning flag (Req. 12) and the generation instant.
 */
export interface ExecutionReport {
  campaign_id: string
  metrics: SufficiencyMetrics
  results: ReportAbilityResult[]
  environment_deprovisioned: boolean
  generated_at: string
}

/**
 * Per-Ability result of a Stage 6 execution
 * (backend `ExecutionAbilityResult`, Req. 10.4). `target_container` is the label
 * of the audited target; `output` is the collected output.
 */
export interface ExecutionAbilityResult {
  ability_id?: string | null
  status: AbilityResultStatus
  command?: string | null
  target_container: string
  output?: string | null
}

/**
 * Why the runner's execution ended the way it did
 * (backend `RunOutcome`, Req. 13). Maps the confirmation/containment/
 * isolation/availability/execution paths.
 */
export type RunOutcome =
  | "nao_confirmada"
  | "contencao_recusada"
  | "isolamento_falhou"
  | "caldera_indisponivel"
  | "concluida"
  | "abortada"
  | (string & {})

/**
 * Typed result of the Stage 6 execution (backend `ExecutionResult`,
 * Req. 10.x, 13). `started` indicates whether the Operation actually ran;
 * `result` is the {@link RunOutcome}; `violations`/`non_isolated_containers`
 * identify the offenders when refused by containment/isolation.
 */
export interface ExecutionResult {
  campaign_id: string
  started: boolean
  result: RunOutcome
  state: OperationState
  operation_id?: number | null
  caldera_operation_id?: string | null
  total_success: number
  total_failure: number
  results: ExecutionAbilityResult[]
  completed: boolean
  completion_persisted: boolean
  violations: string[]
  non_isolated_containers: string[]
  message: string
}

/**
 * Typed result of the Stage 6 completion — execution + report + teardown
 * (backend `CompletionStage6Result`, Req. 10.6–10.9, 12). `completed` is `true`
 * iff the execution completed, the environment was deprovisioned and the report
 * was persisted.
 */
export interface CompletionStage6Result {
  campaign_id: string
  execution: ExecutionResult
  report: ExecutionReport
  deprovisioning: DeprovisioningResult
  report_persisted: boolean
  completed: boolean
}

/** Body of `POST /api/campaigns/{campaign}/stage/6/execution` (Req. 13.2). */
export interface ExecutionRequest {
  confirmed: boolean
}

/**
 * Stage 6 preview — complete list of concrete commands + target container of
 * each + external-destination flag, **without executing** nor creating an
 * Operation (`POST /api/campaigns/{campaign}/stage/6/execution/preview`,
 * Req. 13.6, 13.7). It is the visible precondition before any confirmation. The
 * shape reuses {@link EmulationPreview} (same `EmulationPreviewResponse` contract).
 */
export function previewExecution(campaign: string): Promise<EmulationPreview> {
  return api.post<EmulationPreview>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/6/execution/preview`,
  )
}

/**
 * Execute Stage 6 with the four gates (confirmation → containment → isolation →
 * availability), measure, generate the report and **always** deprovision
 * (`POST /api/campaigns/{campaign}/stage/6/execution`, Req. 10.1–10.9, 12, 13).
 *
 * Only called on an **explicit** confirmation (`confirmed=true`). The
 * refusal/unavailability paths surface as `ApiError` (409 containment/isolation,
 * 503 Caldera unavailable, 502 API error); `confirmed=false` and
 * `COMPLETED`/`ABORTED` return 200 with the {@link CompletionStage6Result}.
 */
export function executeStage6(
  campaign: string,
  confirmed: boolean,
): Promise<CompletionStage6Result> {
  const body: ExecutionRequest = { confirmed }
  return api.post<CompletionStage6Result>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/6/execution`,
    body,
  )
}

// ---------------------------------------------------------------------------
// Consolidated report (Req. 10.7, 15.7).
// ---------------------------------------------------------------------------

/**
 * Consolidated report + procedural-sufficiency metrics of Stage 6
 * (`GET /api/campaigns/{campaign}/report`, backend `ExecutionReport`, Req. 10.7,
 * 15.7). The metrics are recomputed on each read from the persisted results + the
 * DAG; a Campaign without execution returns zeroed metrics.
 */
export function getReport(campaign: string): Promise<ExecutionReport> {
  return api.get<ExecutionReport>(
    `/campaigns/${encodeURIComponent(campaign)}/report`,
  )
}

// Re-export for consumers of the per-Ability execution result via this module
// (keeps the dependency on `emulation.ts` explicit and tree-shakeable).
export type { EmulationPreview, EmulationAbilityResult }
