/**
 * Typed client for the language-model profile routes (`app/api/llm.py`):
 * `GET /api/llm/profiles`, `PUT /api/llm/selection`, `POST /api/llm/preflight`.
 * The backend returns only codes and numbers; the UI translates them.
 */
import { api } from "@/lib/api"

export type LlmProfileKind = "openai_compatible" | "ollama_local"
export type MemoryFit = "fits_gpu" | "fits_ram_only" | "too_large" | "memory_unknown"

export interface LlmProfile {
  /** `work` or `ollama:<model name>`. */
  id: string
  kind: LlmProfileKind
  model_id: string
  is_default: boolean
  /** False when a required variable is missing (work profile only). */
  configured: boolean
  size_bytes: number | null
  memory_fit: MemoryFit | null
}

export interface LlmProfilesResponse {
  selected_id: string
  local_discovery: "ok" | "ollama_down"
  profiles: LlmProfile[]
}

export type PreflightStatus = "ok" | "warning" | "blocked"
export type CheckStatus = "pass" | "warn" | "fail" | "skipped"

/** Numbers of the `memory` check, in bytes (present when the model size is known). */
export interface MemoryCheckDetail {
  model_size_bytes: number
  required_bytes: number
  gpu_free_bytes: number | null
  ram_free_bytes: number | null
  headroom_factor: number
}

export interface PreflightCheck {
  name: "reachability" | "daemon" | "model" | "response" | "memory" | "embedder"
  code: string
  status: CheckStatus
  detail: MemoryCheckDetail | null
}

export interface PreflightResult {
  profile_id: string
  model_id: string
  kind: LlmProfileKind
  status: PreflightStatus
  checked_at: string
  checks: PreflightCheck[]
  blocking_codes: string[]
  warning_codes: string[]
  /** Hint codes; today only `vpn`. */
  hints: string[]
}

/** Aggregate state shown by the status icon. */
export type LlmState = "checking" | "ok" | "warning" | "blocked" | "unknown"

export function getLlmProfiles(): Promise<LlmProfilesResponse> {
  return api.get<LlmProfilesResponse>("/llm/profiles")
}

export function selectLlmProfile(profileId: string): Promise<{ selected_id: string }> {
  return api.put<{ selected_id: string }>("/llm/selection", { profile_id: profileId })
}

/** `profileId` null checks the selected profile; `force` skips the 30 s cache. */
export function runLlmPreflight(
  profileId: string | null = null,
  force = false,
): Promise<PreflightResult> {
  return api.post<PreflightResult>("/llm/preflight", { profile_id: profileId, force })
}

export interface ReasonCodes {
  /** `detail.reason`, e.g. `llm_blocked`, `run_failed`, `unknown_profile`. */
  reason: string | null
  /** `detail.codes` (blocking codes of a `llm_blocked` response). */
  codes: string[]
  /** `detail.cause_code` of a `run_failed` response. */
  causeCode: string | null
}

/** Reads the structured reason from an `ApiError` body; empty values for any other error. */
export function reasonCodeFrom(error: unknown): ReasonCodes {
  const body = (error as { body?: unknown } | null)?.body
  const detail = (body as { detail?: unknown } | null | undefined)?.detail
  const record = typeof detail === "object" && detail !== null ? (detail as Record<string, unknown>) : {}
  return {
    reason: typeof record.reason === "string" ? record.reason : null,
    codes: Array.isArray(record.codes)
      ? record.codes.filter((code): code is string => typeof code === "string")
      : [],
    causeCode: typeof record.cause_code === "string" ? record.cause_code : null,
  }
}

/** A failed or missing Preflight query is `unknown`, not `blocked`: the backend decides at run start. */
export function deriveLlmState(
  result: PreflightResult | null,
  loading: boolean,
  error: unknown,
): LlmState {
  if (loading) return "checking"
  if (error || !result) return "unknown"
  return result.status
}

const gigabyteFormat = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})

/** Formats bytes as gigabytes (1 GB = 1024^3 bytes) with one decimal place, e.g. `4.4`. */
export function formatGigabytes(bytes: number): string {
  return gigabyteFormat.format(bytes / 1024 ** 3)
}
