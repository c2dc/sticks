/**
 * Typed API wrappers for the knowledge-source ingestion (`/api/ingestion/*`).
 *
 * - `GET  /api/ingestion/status`         -> {@link getIngestionStatus}
 * - `POST /api/ingestion/verify`         -> {@link verifyIngestion}
 * - `POST /api/ingestion/{source}/start` -> {@link startIngestion}
 *
 * Mirrors the backend contract in `app/api/ingestion.py` (`IngestionSourceStatus`)
 * and the enums in `app/models/enums.py` (`IngestSource`, `IngestStatus`).
 * Transport errors surface as `ApiError` (e.g. 409 when the source is already
 * running, 404 for an unknown source).
 */
import { api } from "@/lib/api"

/** Knowledge sources loaded by the ingestion orchestrator (backend `IngestSource`). */
export type IngestSource = "atomic" | "docker_catalog" | "attack_flow" | "stix_packages" | "curated_seed"

/** Lifecycle of the ingestion of one source (backend `IngestStatus`). */
export type IngestStatus = "pending" | "running" | "ready" | "failed"

/** Persisted ingestion state of one source (backend `IngestionSourceStatus`). */
export interface IngestionSourceStatus {
  source: IngestSource
  status: IngestStatus
  loaded_count: number | null
  embedded_count: number | null
  expected_count: number | null
  error: string | null
  skipped_count: number
  /** ISO timestamp of the last state change; `null` when never updated. */
  updated_at: string | null
}

/** `true` while at least one source is still being prepared (`pending`/`running`). */
export function isIngestionInProgress(statuses: IngestionSourceStatus[]): boolean {
  return statuses.some((item) => item.status === "pending" || item.status === "running")
}

/** Persisted state of every source (`GET /api/ingestion/status`). */
export function getIngestionStatus(): Promise<IngestionSourceStatus[]> {
  return api.get<IngestionSourceStatus[]>("/ingestion/status")
}

/**
 * Recompute the plans and start in the background whatever is missing
 * (`POST /api/ingestion/verify`). Returns the same body as the status route.
 */
export function verifyIngestion(): Promise<IngestionSourceStatus[]> {
  return api.post<IngestionSourceStatus[]>("/ingestion/verify")
}

/**
 * Start the ingestion of one source (`POST /api/ingestion/{source}/start`, 202).
 * Rejects with `ApiError` 409 when the source is already running.
 */
export function startIngestion(source: IngestSource): Promise<IngestionSourceStatus> {
  return api.post<IngestionSourceStatus>(
    `/ingestion/${encodeURIComponent(source)}/start`,
  )
}
