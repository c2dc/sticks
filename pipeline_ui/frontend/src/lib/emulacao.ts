/**
 * Typed API wrappers for the emulation preview + run endpoints, used by the
 * EmulationConfirmModal (Task 12.4, Req. 6.5, 6.6, 6.7, 6.2, 6.4, 4.6).
 *
 * Backend contracts (built in task 11.2) are the source of truth (pt-BR field
 * names, see the pipeline-ui design "Endpoints REST"):
 *
 * - `POST /api/casos/{caso}/emulacao/preview` -> {@link EmulacaoPreview}
 *     Returns the COMPLETE list of concrete commands + the target container of
 *     each, WITHOUT executing anything (Req. 6.5). Called on modal open so the
 *     researcher sees exactly what would run before any confirmation.
 *
 * - `POST /api/casos/{caso}/emulacao` with `{ confirmado: boolean }`:
 *     * 200 -> {@link EmulacaoResultado} (aggregate result). `confirmado=false`
 *       returns `iniciada=false` and runs nothing (Req. 6.6, 6.7).
 *     * 409 `contencao_recusada` -> external destination; the offending
 *       ability/command is identified (Req. 6.2).
 *     * 409 `isolamento_falhou` -> a target container is not isolated; the
 *       offending container is identified (Req. 6.4).
 *     * 503 -> Caldera unavailable within 10s (Req. 4.6).
 *
 * This module lives beside `casos.ts` / `estagios.ts` and reuses their shared
 * types (`OperationState`, `AbilityResultStatus`) without editing them.
 */
import { api, ApiError } from "@/lib/api"
import type { AbilityResultStatus, OperationState } from "@/lib/estagios"

// ---------------------------------------------------------------------------
// Preview — concrete commands + target containers, WITHOUT executing (Req. 6.5).
// ---------------------------------------------------------------------------

/**
 * One resolved destination detected inside a concrete command. `externo` flags
 * a destination outside the internal subnets (172.20/21/22.0.0/24); such rows
 * are surfaced in red and mean the Operation will be refused (Req. 6.2).
 */
export interface EmulacaoDestino {
  valor: string
  origem: string
  externo: boolean
  container: string | null
  alvo: string | null
}

/** A single concrete command of an Ability, with its resolved destinations. */
export interface EmulacaoComando {
  comando: string
  /** True when the command is purely local (no outbound destination). */
  local: boolean
  /** True when this command targets an external (non-internal) destination. */
  tem_externo: boolean
  destinos: EmulacaoDestino[]
  /** Target container(s) the command would run against (Req. 6.5). */
  alvos: string[]
}

/** The concrete commands of one Ability in the preview. */
export interface EmulacaoAbilityPreview {
  ability_id: string
  ability_name: string
  /** True when any command of this Ability targets an external destination. */
  tem_externo: boolean
  comandos: EmulacaoComando[]
}

/** Response of `POST /api/casos/{caso}/emulacao/preview` (Req. 6.5). */
export interface EmulacaoPreview {
  caso_id: string
  /** True when any command of any Ability targets an external destination. */
  tem_externo: boolean
  abilities: EmulacaoAbilityPreview[]
}

/**
 * Fetch the pre-confirmation preview for a case: the complete list of concrete
 * commands and the target container of each, WITHOUT executing (Req. 6.5).
 */
export function getEmulacaoPreview(caso: string): Promise<EmulacaoPreview> {
  return api.post<EmulacaoPreview>(
    `/casos/${encodeURIComponent(caso)}/emulacao/preview`,
  )
}

// ---------------------------------------------------------------------------
// Run — starts only with confirmado=true; pre-flight containment (Req. 6.6, 6.7).
// ---------------------------------------------------------------------------

/** Per-Ability result in the aggregate run response. */
export interface EmulacaoResultadoAbility {
  ability_id: string
  status: AbilityResultStatus
  saida_comando: string
}

/**
 * 200 response of `POST /api/casos/{caso}/emulacao`. When `confirmado=false`
 * (or no confirmation), `iniciada` is false and nothing ran (Req. 6.6, 6.7).
 * On success it carries the aggregate success/failure totals (Req. 4.5).
 */
export interface EmulacaoResultado {
  iniciada: boolean
  resultado: string
  estado: OperationState
  operacao_id: string | null
  total_sucesso: number
  total_falha: number
  resultados: EmulacaoResultadoAbility[]
  mensagem: string
}

/** Request body for the run endpoint. */
export interface EmulacaoRequest {
  confirmado: boolean
}

/**
 * Run the emulation. Only ever called on an EXPLICIT confirmation with
 * `confirmado=true` (Req. 6.6). Cancelling the modal must NOT call this
 * (Req. 6.7). Non-2xx outcomes surface as {@link ApiError}; use
 * {@link classifyEmulacaoError} to map them to typed containment / isolation /
 * unavailability outcomes (Req. 6.2, 6.4, 4.6).
 */
export function runEmulacao(
  caso: string,
  confirmado: boolean,
): Promise<EmulacaoResultado> {
  return api.post<EmulacaoResultado>(
    `/casos/${encodeURIComponent(caso)}/emulacao`,
    { confirmado } satisfies EmulacaoRequest,
  )
}

// ---------------------------------------------------------------------------
// Typed run outcomes — 409 containment / isolation, 503 unavailable.
// ---------------------------------------------------------------------------

/** A single containment violation (external-destination) entry (Req. 6.2). */
export interface ContencaoViolacao {
  ability_id?: string
  ability_name?: string
  comando?: string
  destino?: string
  [key: string]: unknown
}

/**
 * 409 `contencao_recusada` — a command targets an external destination; the
 * Operation is refused and the offending ability/command is identified
 * (Req. 6.2).
 */
export interface ContencaoRecusadaOutcome {
  kind: "contencao_recusada"
  mensagem?: string
  violacoes: ContencaoViolacao[]
}

/**
 * 409 `isolamento_falhou` — a target container is not isolated; the Operation
 * is aborted and the offending container is identified (Req. 6.4).
 */
export interface IsolamentoFalhouOutcome {
  kind: "isolamento_falhou"
  mensagem?: string
  containers: string[]
}

/** 503 — Caldera did not respond within 10s (Req. 4.6). */
export interface CalderaIndisponivelOutcome {
  kind: "caldera_indisponivel"
  mensagem?: string
}

/** Any other/unknown failure. */
export interface ErroDesconhecidoOutcome {
  kind: "desconhecido"
  status?: number
  mensagem?: string
}

/** Discriminated union of the classified run-error outcomes. */
export type EmulacaoErroOutcome =
  | ContencaoRecusadaOutcome
  | IsolamentoFalhouOutcome
  | CalderaIndisponivelOutcome
  | ErroDesconhecidoOutcome

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
 * Map a thrown error from {@link runEmulacao} to a typed outcome so the modal
 * can render the right message: containment violation (409, Req. 6.2),
 * isolation failure (409, Req. 6.4) or Caldera unavailable (503, Req. 4.6).
 */
export function classifyEmulacaoError(error: unknown): EmulacaoErroOutcome {
  if (error instanceof ApiError) {
    const detail = detailOf(error)
    const motivo = detail && typeof detail.motivo === "string" ? detail.motivo : undefined
    const mensagem =
      detail && typeof detail.mensagem === "string" ? detail.mensagem : error.message

    if (error.status === 409 && motivo === "contencao_recusada") {
      const violacoes =
        detail && Array.isArray(detail.violacoes)
          ? (detail.violacoes as ContencaoViolacao[])
          : []
      return { kind: "contencao_recusada", mensagem, violacoes }
    }

    if (error.status === 409 && motivo === "isolamento_falhou") {
      const containers =
        detail && Array.isArray(detail.containers)
          ? (detail.containers as unknown[]).map((c) => String(c))
          : []
      return { kind: "isolamento_falhou", mensagem, containers }
    }

    if (error.status === 503) {
      return { kind: "caldera_indisponivel", mensagem }
    }

    return { kind: "desconhecido", status: error.status, mensagem }
  }

  return {
    kind: "desconhecido",
    mensagem: error instanceof Error ? error.message : String(error),
  }
}
