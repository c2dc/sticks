/**
 * Typed API wrappers for the STIX inspection endpoints (`/api/stix/*`).
 *
 * This module is the single source of truth for the STIX-base types consumed
 * by the dashboard flow (base listing, intelligence dashboard, campaign/actor
 * selection). It mirrors the backend contract defined in `app/api/stix.py` and
 * `app/services/stix/stix_service.py` (pt-BR field names, identical to the JSON
 * the backend returns).
 *
 * All GET endpoints are side-effect free; the only outbound-network action is
 * `POST /api/stix/bases/{versao}/download` ({@link baixarBase}). All wrappers
 * reuse the shared {@link api} client, so transport errors surface as
 * `ApiError` just like the curated-cases wrappers in `lib/casos.ts`.
 */
import { api } from "@/lib/api"

// ===========================================================================
// Bases STIX (Req. 2.1, 2.5).
// ===========================================================================

/**
 * A known STIX base version with its on-disk availability
 * (backend `BaseStixInfo`, Req. 2.1, 2.2, 2.3).
 *
 * `baixada` reflects live presence on disk; `selecionavel` is its exact
 * complement of availability (only downloaded bases can be selected).
 * `arquivo` is populated only when the base is downloaded.
 */
export interface BaseStixInfo {
  versao: string
  nome: string
  baixada: boolean
  selecionavel: boolean
  arquivo: string | null
}

/** Response shape of `GET /api/stix/bases` (backend `BasesResponse`). */
export interface BasesResponse {
  bases: BaseStixInfo[]
}

// ===========================================================================
// Dashboard de inteligência (Req. 3.1).
// ===========================================================================

/**
 * A per-type object count with its pt-BR description
 * (backend `ContagemTipo`, Req. 3.2, 3.3). `tipo` is the display label and
 * `stix_type` the raw STIX `type` it aggregates.
 */
export interface ContagemTipo {
  tipo: string
  stix_type: string
  quantidade: number
  descricao: string
}

/**
 * Intelligence dashboard for a downloaded base — per-type counts plus the base
 * identification (backend `DashboardStix`, Req. 3.1, 3.2).
 */
export interface DashboardStix {
  versao: string
  nome: string
  contagens: ContagemTipo[]
}

// ===========================================================================
// Campanhas e atores (Req. 4.2, 4.3).
// ===========================================================================

/**
 * A STIX `campaign` with its curated-case marking
 * (backend `CampanhaStix`, Req. 4.2, 4.5). `curada` is `true` with
 * `slug_curado` populated iff the campaign maps to one of the curated cases.
 */
export interface CampanhaStix {
  stix_id: string
  nome: string
  descricao: string | null
  curada: boolean
  slug_curado: string | null
}

/** Response shape of `GET /api/stix/bases/{versao}/campanhas` (backend `CampanhasResponse`). */
export interface CampanhasResponse {
  versao: string
  campanhas: CampanhaStix[]
}

/**
 * A STIX `intrusion-set` (actor) with its attributed campaigns
 * (backend `AtorStix`, Req. 4.3). Campaigns without attribution are grouped
 * under a synthetic "sem ator associado" actor by the backend.
 */
export interface AtorStix {
  stix_id: string
  nome: string
  campanhas: CampanhaStix[]
}

/** Response shape of `GET /api/stix/bases/{versao}/atores` (backend `AtoresResponse`). */
export interface AtoresResponse {
  versao: string
  atores: AtorStix[]
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
 * Download a STIX base, returning its updated info with `baixada=true`
 * (`POST /api/stix/bases/{versao}/download`, Req. 2.5). The only
 * outbound-network action in this module; failures surface as `ApiError`.
 */
export function baixarBase(versao: string): Promise<BaseStixInfo> {
  return api.post<BaseStixInfo>(
    `/stix/bases/${encodeURIComponent(versao)}/download`,
  )
}

/**
 * Fetch the intelligence dashboard (per-type counts) for a downloaded base
 * (`GET /api/stix/bases/{versao}/dashboard`, Req. 3.1). Side-effect free.
 */
export function getDashboard(versao: string): Promise<DashboardStix> {
  return api.get<DashboardStix>(
    `/stix/bases/${encodeURIComponent(versao)}/dashboard`,
  )
}

/**
 * Fetch the campaigns of a downloaded base with curated-case marking
 * (`GET /api/stix/bases/{versao}/campanhas`, Req. 4.2). Side-effect free.
 */
export async function getCampanhas(versao: string): Promise<CampanhaStix[]> {
  const resp = await api.get<CampanhasResponse>(
    `/stix/bases/${encodeURIComponent(versao)}/campanhas`,
  )
  return resp.campanhas
}

/**
 * Fetch the actors (intrusion-sets) of a downloaded base with their nested,
 * attributed campaigns (`GET /api/stix/bases/{versao}/atores`, Req. 4.3).
 * Side-effect free.
 */
export async function getAtores(versao: string): Promise<AtorStix[]> {
  const resp = await api.get<AtoresResponse>(
    `/stix/bases/${encodeURIComponent(versao)}/atores`,
  )
  return resp.atores
}
