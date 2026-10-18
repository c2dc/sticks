/**
 * Typed API wrappers for the STIX inspection endpoints (`/api/stix/*`).
 *
 * This module is the single source of truth for the STIX-base types consumed
 * by the dashboard flow (base listing, intelligence dashboard, campaign/actor
 * selection). It mirrors the backend contract defined in `app/api/stix.py` and
 * `app/services/stix/stix_service.py` (identical to the JSON the backend
 * returns).
 *
 * All GET endpoints are side-effect free; the only outbound-network action is
 * `POST /api/stix/bases/{version}/download` ({@link downloadBase}). All wrappers
 * reuse the shared {@link api} client, so transport errors surface as
 * `ApiError` just like the curated-campaigns wrappers in `lib/campaigns.ts`.
 */
import { api } from "@/lib/api"

// ===========================================================================
// STIX bases (Req. 2.1, 2.5).
// ===========================================================================

/**
 * A known STIX base version with its on-disk availability
 * (backend `BaseStixInfo`, Req. 2.1, 2.2, 2.3).
 *
 * `downloaded` reflects live presence on disk; `selectable` is its exact
 * complement of availability (only downloaded bases can be selected).
 * `file` is populated only when the base is downloaded.
 */
export interface BaseStixInfo {
  version: string
  name: string
  downloaded: boolean
  selectable: boolean
  file: string | null
}

/** Response shape of `GET /api/stix/bases` (backend `BasesResponse`). */
export interface BasesResponse {
  bases: BaseStixInfo[]
}

// ===========================================================================
// Intelligence dashboard (Req. 3.1).
// ===========================================================================

/**
 * A per-type object count with its description
 * (backend `TypeCount`, Req. 3.2, 3.3). `label` is the display label and
 * `stix_type` the raw STIX `type` it aggregates.
 */
export interface TypeCount {
  label: string
  stix_type: string
  count: number
  description: string
}

/**
 * Intelligence dashboard for a downloaded base — per-type counts plus the base
 * identification (backend `DashboardStix`, Req. 3.1, 3.2).
 */
export interface DashboardStix {
  version: string
  name: string
  counts: TypeCount[]
}

// ===========================================================================
// Campaigns and adversaries (Req. 4.2, 4.3).
// ===========================================================================

/**
 * A STIX `campaign` with its curated-campaign marking
 * (backend `CampaignStix`, Req. 4.2, 4.5). `curated` is `true` with
 * `curated_slug` populated iff the campaign maps to one of the curated campaigns.
 */
export interface CampaignStix {
  stix_id: string
  name: string
  description: string | null
  curated: boolean
  curated_slug: string | null
}

/** Response shape of `GET /api/stix/bases/{version}/campaigns` (backend `CampaignsResponse`). */
export interface CampaignsResponse {
  version: string
  campaigns: CampaignStix[]
}

/**
 * A STIX `intrusion-set` (adversary) with its attributed campaigns
 * (backend `AdversaryStix`, Req. 4.3). Campaigns without attribution are grouped
 * under a synthetic "no associated adversary" adversary by the backend.
 */
export interface AdversaryStix {
  stix_id: string
  name: string
  campaigns: CampaignStix[]
}

/** Response shape of `GET /api/stix/bases/{version}/actors` (backend `AdversariesResponse`). */
export interface AdversariesResponse {
  version: string
  adversaries: AdversaryStix[]
}

// ===========================================================================
// Endpoint wrappers.
// ===========================================================================

/**
 * List the known STIX bases, newest first, with live download/selection flags
 * (`GET /api/stix/bases`, Req. 2.1). Side-effect free.
 */
export async function getBases(): Promise<BaseStixInfo[]> {
  const resp = await api.get<BasesResponse>("/stix/bases")
  return resp.bases
}

/**
 * Download a STIX base, returning its updated info with `downloaded=true`
 * (`POST /api/stix/bases/{version}/download`, Req. 2.5). The only
 * outbound-network action in this module; failures surface as `ApiError`.
 */
export function downloadBase(version: string): Promise<BaseStixInfo> {
  return api.post<BaseStixInfo>(
    `/stix/bases/${encodeURIComponent(version)}/download`,
  )
}

/**
 * Fetch the intelligence dashboard (per-type counts) for a downloaded base
 * (`GET /api/stix/bases/{version}/dashboard`, Req. 3.1). Side-effect free.
 */
export function getDashboard(version: string): Promise<DashboardStix> {
  return api.get<DashboardStix>(
    `/stix/bases/${encodeURIComponent(version)}/dashboard`,
  )
}

/**
 * Fetch the campaigns of a downloaded base with curated-campaign marking
 * (`GET /api/stix/bases/{version}/campaigns`, Req. 4.2). Side-effect free.
 */
export async function getCampaigns(version: string): Promise<CampaignStix[]> {
  const resp = await api.get<CampaignsResponse>(
    `/stix/bases/${encodeURIComponent(version)}/campaigns`,
  )
  return resp.campaigns
}

/**
 * Fetch the adversaries (intrusion-sets) of a downloaded base with their nested,
 * attributed campaigns (`GET /api/stix/bases/{version}/actors`, Req. 4.3).
 * Side-effect free.
 */
export async function getAdversaries(version: string): Promise<AdversaryStix[]> {
  const resp = await api.get<AdversariesResponse>(
    `/stix/bases/${encodeURIComponent(version)}/actors`,
  )
  return resp.adversaries
}
