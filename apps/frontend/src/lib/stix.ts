/**
 * Typed API wrappers for the STIX inspection endpoints (`/api/stix/*`).
 *
 * This module is the single source of truth for the STIX-base types consumed
 * by the dashboard flow (base listing, package overview, campaign listing and
 * campaign overview). It mirrors the backend contract defined in `app/api/stix.py` and
 * `app/services/stix/stix_service.py` (identical to the JSON the backend
 * returns).
 *
 * All GET endpoints are side-effect free; the only outbound-network action is
 * `POST /api/stix/bases/{version}/download` ({@link downloadBase}). All wrappers
 * reuse the shared {@link api} client, so transport errors surface as
 * `ApiError` just like the curated-campaigns wrappers in `lib/campaigns.ts`.
 */
import { ApiError, api } from "@/lib/api"
import { getIngestionStatus } from "@/lib/ingestion"

// ===========================================================================
// STIX bases (Req. 2.1, 2.5).
// ===========================================================================

/**
 * A known STIX base version with its database-backed availability
 * (backend `BaseStixInfo`, Req. 2.1, 2.2, 2.3, 3.1, 3.2, 3.3).
 *
 * `downloaded` reflects whether the version is persisted in the database (the
 * single source of truth); `selectable` is its exact complement of
 * availability (only persisted bases can be selected). No bundle is stored on
 * disk, so there is no file path in the contract.
 */
export interface BaseStixInfo {
  version: string
  name: string
  downloaded: boolean
  selectable: boolean
  /** Legacy field; the bundle is never stored on disk, so it is absent or `null`. */
  file?: string | null
  /** `true` iff the version is persisted in the database (backend `completed`). */
  completed?: boolean
}

/** Response shape of `GET /api/stix/bases` (backend `BasesResponse`). */
export interface BasesResponse {
  bases: BaseStixInfo[]
}

// ===========================================================================
// Package overview (Req. 3.1, 6.1).
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
 * Package overview for a downloaded base — the base identification plus the
 * per-type counts and the headline totals, read entirely from the database
 * (backend `StixPackageOverview`, Req. 3.1–3.4, 6.1).
 *
 * `downloaded_at` is an ISO-8601 timestamp, or `null` when unknown. The `*_total`
 * fields are the headline totals (campaigns, actors, techniques, tactics,
 * malware and relationships) the backend derives from the persisted rows and
 * per-type counts.
 */
export interface StixPackageOverview {
  version: string
  name: string
  downloaded_at: string | null
  counts: TypeCount[]
  campaigns_total: number
  actors_total: number
  techniques_total: number
  tactics_total: number
  malware_total: number
  relationships_total: number
}

// ===========================================================================
// Campaigns (Req. 4.2, 4.3).
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

/**
 * A campaign row of a package for the clickable list
 * (backend `StixCampaignSummary`, Req. 4.3, 4.5). `campaign_key` is the stable
 * key the UI/route use to address the campaign: the `curated_slug` when the
 * campaign is curated, otherwise the `stix_id`.
 */
export interface StixCampaignSummary {
  campaign_key: string
  stix_id: string
  name: string
  external_id: string | null
  curated: boolean
  curated_slug: string | null
  /**
   * Container-emulability marking: `null`/absent when not derived yet (the
   * campaign is still listed), `true`/`false` once derived.
   */
  container_emulable?: boolean | null
  /** Technique ids that block container emulation (empty when emulable). */
  blocking_techniques?: string[]
}

/**
 * One ATT&CK technique a campaign uses
 * (backend `TechniqueElement`, Req. 4.4). All fields but the id are optional.
 */
export interface TechniqueElement {
  technique_id: string
  technique_name: string | null
  tactic: string | null
  description: string | null
}

/**
 * Overview of a single campaign of a package
 * (backend `StixCampaignOverview`, design Component 4, Req. 4.4, 4.5). Carries
 * the campaign identity, its `description` and `aliases`, the ATT&CK
 * `techniques` it uses and the `actors` attributed to it.
 */
export interface StixCampaignOverview {
  campaign_key: string
  stix_id: string
  name: string
  external_id: string | null
  description: string | null
  aliases: string[]
  curated: boolean
  curated_slug: string | null
  techniques: TechniqueElement[]
  actors: { stix_id: string; name: string }[]
}

// ===========================================================================
// Package analytics (Req. 12.1–12.8).
// ===========================================================================

/**
 * One ranked actor of a package — a threat actor with the number of campaigns
 * attributed to it (backend `ActorRank`, Req. 12.2). `top_actors` is ordered by
 * `campaigns_total`, descending.
 */
export interface ActorRank {
  stix_id: string
  name: string
  campaigns_total: number
}

/**
 * One ranked campaign of a package — a campaign with its curated marking and
 * the number of ATT&CK techniques it uses (backend `CampaignRank`, Req. 12.3).
 * `campaign_key` is the stable key (curated slug when curated, else the
 * `stix_id`); `top_campaigns` is ordered by `techniques_total`, descending.
 */
export interface CampaignRank {
  campaign_key: string
  name: string
  curated: boolean
  techniques_total: number
}

/**
 * One tactic bucket of the technique distribution — a tactic with the number of
 * techniques mapped to it (backend `TacticDistribution`, Req. 12.5).
 */
export interface TacticDistribution {
  tactic: string
  techniques_total: number
}

/**
 * One ranked technique of a package — the inverse of `CampaignRank`: for a
 * single ATT&CK technique, the number of DISTINCT campaigns that use it
 * (backend `TechniqueRank`). `top_techniques` is ordered by `campaigns_total`,
 * descending.
 */
export interface TechniqueRank {
  technique_id: string
  technique_name: string | null
  campaigns_total: number
}

/**
 * Analytic aggregations of a downloaded base — curated vs. uncurated campaign
 * totals, the top actors and campaigns, the technique distribution per tactic
 * and the campaigns-per-technique ranking (`top_techniques`) — the inverse of
 * `top_campaigns` (backend `StixPackageAnalytics`, design Component 5).
 *
 * Every field is derived entirely from the persisted rows of the version; no
 * bundle reprocessing and no network.
 */
export interface StixPackageAnalytics {
  version: string
  curated_total: number
  uncurated_total: number
  top_actors: ActorRank[]
  top_campaigns: CampaignRank[]
  tactics: TacticDistribution[]
  top_techniques: TechniqueRank[]
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

/** Body of `POST /api/stix/bases/{version}/download` (backend `StixPackagePersistedResult`). */
interface StixDownloadResult {
  version: string
  downloaded: boolean
  persisted: boolean
}

/** Polling knobs of {@link downloadBase}; the defaults suit the real backend. */
export interface DownloadBaseOptions {
  /** Milliseconds between two checks of the database state. */
  intervalMs?: number
  /** Give up (with `ApiError` 504) after this many milliseconds. */
  timeoutMs?: number
}

const DEFAULT_DOWNLOAD_POLL_MS = 1500
const DEFAULT_DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000

async function findIngestedBase(version: string): Promise<BaseStixInfo | undefined> {
  const bases = await getBases()
  return bases.find((base) => base.version === version && base.downloaded)
}

/**
 * Ingest a STIX base into the database and resolve once it is available
 * (`POST /api/stix/bases/{version}/download`, Req. 2.5, 7.1).
 *
 * The backend runs the ingestion in the background (`202`): the bundle is
 * downloaded in memory and persisted straight into the database. This wrapper
 * keeps the promise contract of the old synchronous download: it follows
 * `GET /api/stix/bases` until the version is `downloaded`, and rejects with
 * `ApiError` when the `stix_packages` ingestion source fails (or times out).
 */
export async function downloadBase(
  version: string,
  options: DownloadBaseOptions = {},
): Promise<BaseStixInfo> {
  const accepted = await api.post<StixDownloadResult | BaseStixInfo>(
    `/stix/bases/${encodeURIComponent(version)}/download`,
  )
  if (accepted && "downloaded" in accepted && accepted.downloaded) {
    return accepted as BaseStixInfo
  }

  const intervalMs = options.intervalMs ?? DEFAULT_DOWNLOAD_POLL_MS
  const deadline = Date.now() + (options.timeoutMs ?? DEFAULT_DOWNLOAD_TIMEOUT_MS)
  for (;;) {
    const ready = await findIngestedBase(version)
    if (ready) return ready

    const source = (await getIngestionStatus()).find(
      (item) => item.source === "stix_packages",
    )
    if (source?.status === "failed") {
      throw new ApiError(502, source.error ?? "STIX ingestion failed")
    }
    if (source?.status === "ready") {
      // Re-check once: the ingestion may have finished between the two reads.
      const late = await findIngestedBase(version)
      if (late) return late
      throw new ApiError(502, `STIX version ${version} was not ingested`)
    }
    if (Date.now() >= deadline) {
      throw new ApiError(504, `Timed out waiting for STIX version ${version}`)
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }
}

/**
 * Fetch the package overview (identification, per-type counts and headline
 * totals) for a downloaded base
 * (`GET /api/stix/bases/{version}/overview`, Req. 3.1, 6.1). Read from the
 * database only; side-effect free.
 */
export function getPackageOverview(
  version: string,
): Promise<StixPackageOverview> {
  return api.get<StixPackageOverview>(
    `/stix/bases/${encodeURIComponent(version)}/overview`,
  )
}

/**
 * Fetch the campaigns of a downloaded base with curated-campaign marking
 * (`GET /api/stix/bases/{version}/campaigns`, Req. 4.3). The endpoint returns a
 * bare array of {@link StixCampaignSummary}, returned as-is. Side-effect free.
 */
export function getCampaigns(version: string): Promise<StixCampaignSummary[]> {
  return api.get<StixCampaignSummary[]>(
    `/stix/bases/${encodeURIComponent(version)}/campaigns`,
  )
}

/**
 * Fetch the overview of a single campaign of a downloaded base — its identity,
 * description, aliases, ATT&CK techniques and attributed actors
 * (`GET /api/stix/bases/{version}/campaigns/{campaign_key}/overview`, Req. 6.2).
 * Read from the database only; side-effect free.
 */
export function getCampaignOverview(
  version: string,
  campaignKey: string,
): Promise<StixCampaignOverview> {
  return api.get<StixCampaignOverview>(
    `/stix/bases/${encodeURIComponent(version)}/campaigns/${encodeURIComponent(
      campaignKey,
    )}/overview`,
  )
}

/**
 * Fetch the analytic aggregations (curated/uncurated totals, top actors, top
 * campaigns and the per-tactic technique distribution) for a downloaded base
 * (`GET /api/stix/bases/{version}/analytics`, Req. 12.8). Read from the
 * database only; side-effect free.
 */
export function getPackageAnalytics(
  version: string,
): Promise<StixPackageAnalytics> {
  return api.get<StixPackageAnalytics>(
    `/stix/bases/${encodeURIComponent(version)}/analytics`,
  )
}
