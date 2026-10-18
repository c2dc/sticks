/**
 * Typed API wrappers for the emulation preview + run endpoints, used by the
 * EmulationConfirmModal (Task 12.4, Req. 6.5, 6.6, 6.7, 6.2, 6.4, 4.6).
 *
 * Backend contracts (built in task 11.2) are the source of truth (see the
 * pipeline-ui design "REST endpoints"):
 *
 * - `POST /api/campaigns/{campaign}/stage/5/emulation/preview` -> {@link EmulationPreview}
 *     Returns the COMPLETE list of concrete commands + the target container of
 *     each, WITHOUT executing anything (Req. 6.5). Called on modal open so the
 *     researcher sees exactly what would run before any confirmation.
 *
 * - `POST /api/campaigns/{campaign}/stage/5/emulation` with `{ confirmed: boolean }`:
 *     * 200 -> {@link EmulationResult} (aggregate result). `confirmed=false`
 *       returns `started=false` and runs nothing (Req. 6.6, 6.7).
 *     * 409 `contencao_recusada` -> external destination; the offending
 *       ability/command is identified (Req. 6.2).
 *     * 409 `isolamento_falhou` -> a target container is not isolated; the
 *       offending container is identified (Req. 6.4).
 *     * 503 -> Caldera unavailable within 10s (Req. 4.6).
 *
 * This module lives beside `campaigns.ts` / `stages.ts` and reuses their shared
 * types (`OperationState`, `AbilityResultStatus`) without editing them.
 */
import { api, ApiError } from "@/lib/api"
import type { AbilityResultStatus, OperationState } from "@/lib/stages"

// ---------------------------------------------------------------------------
// Preview — concrete commands + target containers, WITHOUT executing (Req. 6.5).
// ---------------------------------------------------------------------------

/**
 * One resolved destination detected inside a concrete command. `external` flags
 * a destination outside the internal subnets (172.20/21/22.0.0/24); such rows
 * are surfaced in red and mean the Operation will be refused (Req. 6.2).
 */
export interface EmulationDestination {
  value: string
  origin: string
  external: boolean
  container: string | null
  target: string | null
}

/** A single concrete command of an Ability, with its resolved destinations. */
export interface EmulationCommand {
  command: string
  /** True when the command is purely local (no outbound destination). */
  local: boolean
  /** True when this command targets an external (non-internal) destination. */
  has_external: boolean
  destinations: EmulationDestination[]
  /** Target container(s) the command would run against (Req. 6.5). */
  targets: string[]
}

/** The concrete commands of one Ability in the preview. */
export interface EmulationAbilityPreview {
  ability_id: string
  ability_name: string
  /** True when any command of this Ability targets an external destination. */
  has_external: boolean
  commands: EmulationCommand[]
}

/** Response of `POST /api/campaigns/{campaign}/stage/5/emulation/preview` (Req. 6.5). */
export interface EmulationPreview {
  campaign_id: string
  /** True when any command of any Ability targets an external destination. */
  has_external: boolean
  abilities: EmulationAbilityPreview[]
}

/**
 * Fetch the pre-confirmation preview for a campaign: the complete list of
 * concrete commands and the target container of each, WITHOUT executing
 * (Req. 6.5).
 */
export function getEmulationPreview(campaign: string): Promise<EmulationPreview> {
  return api.post<EmulationPreview>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/5/emulation/preview`,
  )
}

// ---------------------------------------------------------------------------
// Run — starts only with confirmed=true; pre-flight containment (Req. 6.6, 6.7).
// ---------------------------------------------------------------------------

/** Per-Ability result in the aggregate run response. */
export interface EmulationAbilityResult {
  ability_id: string
  status: AbilityResultStatus
  command_output: string
}

/**
 * 200 response of `POST /api/campaigns/{campaign}/stage/5/emulation`. When
 * `confirmed=false` (or no confirmation), `started` is false and nothing ran
 * (Req. 6.6, 6.7). On success it carries the aggregate success/failure totals
 * (Req. 4.5).
 */
export interface EmulationResult {
  started: boolean
  result: string
  state: OperationState
  operation_id: string | null
  total_success: number
  total_failure: number
  results: EmulationAbilityResult[]
  message: string
}

/** Request body for the run endpoint. */
export interface EmulationRequest {
  confirmed: boolean
}

/**
 * Run the emulation. Only ever called on an EXPLICIT confirmation with
 * `confirmed=true` (Req. 6.6). Cancelling the modal must NOT call this
 * (Req. 6.7). Non-2xx outcomes surface as {@link ApiError}; use
 * {@link classifyEmulationError} to map them to typed containment / isolation /
 * unavailability outcomes (Req. 6.2, 6.4, 4.6).
 */
export function runEmulation(
  campaign: string,
  confirmed: boolean,
): Promise<EmulationResult> {
  return api.post<EmulationResult>(
    `/campaigns/${encodeURIComponent(campaign)}/stage/5/emulation`,
    { confirmed } satisfies EmulationRequest,
  )
}

// ---------------------------------------------------------------------------
// Typed run outcomes — 409 containment / isolation, 503 unavailable.
// ---------------------------------------------------------------------------

/** A single containment violation (external-destination) entry (Req. 6.2). */
export interface ContainmentViolation {
  ability_id?: string
  ability_name?: string
  command?: string
  destination?: string
  [key: string]: unknown
}

/**
 * 409 `contencao_recusada` — a command targets an external destination; the
 * Operation is refused and the offending ability/command is identified
 * (Req. 6.2).
 */
export interface ContainmentRefusedOutcome {
  kind: "contencao_recusada"
  message?: string
  violations: ContainmentViolation[]
}

/**
 * 409 `isolamento_falhou` — a target container is not isolated; the Operation
 * is aborted and the offending container is identified (Req. 6.4).
 */
export interface IsolationFailedOutcome {
  kind: "isolamento_falhou"
  message?: string
  containers: string[]
}

/** 503 — Caldera did not respond within 10s (Req. 4.6). */
export interface CalderaUnavailableOutcome {
  kind: "caldera_indisponivel"
  message?: string
}

/** Any other/unknown failure. */
export interface UnknownErrorOutcome {
  kind: "desconhecido"
  status?: number
  message?: string
}

/** Discriminated union of the classified run-error outcomes. */
export type EmulationErrorOutcome =
  | ContainmentRefusedOutcome
  | IsolationFailedOutcome
  | CalderaUnavailableOutcome
  | UnknownErrorOutcome

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

/** Extract the `detail` object from an ApiError body, when present. */
function detailOf(error: ApiError): Record<string, unknown> | undefined {
  if (isRecord(error.body) && isRecord(error.body.detail)) {
    return error.body.detail as Record<string, unknown>
  }
  return undefined
}

/**
 * Map a thrown error from {@link runEmulation} to a typed outcome so the modal
 * can render the right message: containment violation (409, Req. 6.2),
 * isolation failure (409, Req. 6.4) or Caldera unavailable (503, Req. 4.6).
 */
export function classifyEmulationError(error: unknown): EmulationErrorOutcome {
  if (error instanceof ApiError) {
    const detail = detailOf(error)
    // The emulation route's 409/503 error body uses the backend's keys
    // `motivo` / `mensagem` / `violacoes`; read them verbatim to match the wire.
    const reason = detail && typeof detail.motivo === "string" ? detail.motivo : undefined
    const message =
      detail && typeof detail.mensagem === "string" ? detail.mensagem : error.message

    if (error.status === 409 && reason === "contencao_recusada") {
      const violations =
        detail && Array.isArray(detail.violacoes)
          ? (detail.violacoes as ContainmentViolation[])
          : []
      return { kind: "contencao_recusada", message, violations }
    }

    if (error.status === 409 && reason === "isolamento_falhou") {
      const containers =
        detail && Array.isArray(detail.containers)
          ? (detail.containers as unknown[]).map((c) => String(c))
          : []
      return { kind: "isolamento_falhou", message, containers }
    }

    if (error.status === 503) {
      return { kind: "caldera_indisponivel", message }
    }

    return { kind: "desconhecido", status: error.status, message }
  }

  return {
    kind: "desconhecido",
    message: error instanceof Error ? error.message : String(error),
  }
}
