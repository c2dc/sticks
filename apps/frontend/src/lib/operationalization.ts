/**
 * Typed API wrapper for the operationalization listing: the techniques of a
 * campaign with their exact Atomic Red Team commands.
 *
 * - `GET /api/stix/bases/{version}/campaigns/{campaign_key}/operationalization`
 *   -> {@link getOperationalization} -> {@link OperationalizationListing}
 *
 * Mirrors `app/api/operationalization.py` and
 * `app/services/operationalization_listing.py`. Read-only; the backend answers
 * 404 (`campanha_nao_encontrada`) for an unknown campaign and 422
 * (`bundle_indisponivel`) for a version that is not available.
 */
import { api } from "@/lib/api"
import type { IngestStatus } from "@/lib/ingestion"

/** One candidate command of a technique (backend `OperationalizationCommand`). */
export interface OperationalizationCommand {
  id: number | null
  command: string
  executor: string
  platform: string
  source: string
}

/** A campaign technique with its (possibly empty) commands (backend `OperationalizationTechnique`). */
export interface OperationalizationTechnique {
  technique_id: string
  technique_name: string | null
  commands: OperationalizationCommand[]
}

/** Ingestion state of the Atomic Red Team source (backend `AtomicStatus`). */
export interface AtomicStatus {
  status: IngestStatus
  loaded_count: number | null
  expected_count: number | null
}

/** Response of the operationalization listing (backend `OperationalizationListing`). */
export interface OperationalizationListing {
  campaign_key: string
  atomic_status: AtomicStatus
  techniques: OperationalizationTechnique[]
}

/** Fetch the techniques of a campaign with their exact commands. */
export function getOperationalization(
  version: string,
  campaignKey: string,
): Promise<OperationalizationListing> {
  return api.get<OperationalizationListing>(
    `/stix/bases/${encodeURIComponent(version)}/campaigns/${encodeURIComponent(
      campaignKey,
    )}/operationalization`,
  )
}
