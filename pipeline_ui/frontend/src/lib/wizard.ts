/**
 * Wizard de Execução Guiada — wrappers de API e derivação de estado.
 *
 * Fecha as pontas que o fluxo guiado (Avançar → Avançar → Executar/Finish)
 * precisa, sem editar os wrappers já existentes em `casos.ts`/`estagios.ts`:
 * - concluir o Estágio 2 (a tradução é curada; "avançar" = "concluir");
 * - resetar a campanha para repetir a demonstração;
 * - reler o estado por estágio do caso (fonte da verdade = backend/SQLite).
 *
 * `derivarEstados` centraliza a regra do bloqueio sequencial no frontend, para
 * badges e habilitação do botão "Avançar" (spec wizard-execucao-guiada,
 * Property 1).
 */
import { api } from "@/lib/api"
import { getCasos, type Caso, type CasoEstados, type NumeroEstagio } from "@/lib/casos"

// ---------------------------------------------------------------------------
// Wrappers de API (novos endpoints do wizard).
// ---------------------------------------------------------------------------

/** Estado por estágio de um caso (contrato de `GET /api/casos/{caso}`). */
export interface CaseStageStatesResponse {
  caso_id: string
  estagios: {
    estagio: NumeroEstagio
    estado: string
    bloqueado: boolean
    iniciavel: boolean
    progresso?: number
  }[]
  concluido_total: boolean
}

/** Relê o caso selecionado (inclui `estados.estagios[]`). */
export function getCaso(caso: string): Promise<Caso> {
  return api.get<Caso>(`/casos/${encodeURIComponent(caso)}`)
}

/** Dispara a modelagem estrutural do Estágio 1 (reusa o contrato Stage1Result). */
export function executarEstagio1(caso: string): Promise<Stage1Result> {
  return api.post<Stage1Result>(
    `/casos/${encodeURIComponent(caso)}/estagio/1/executar`,
  )
}

/** Conclui o Estágio 2 (tradução curada) e destrava o 3. */
export function concluirEstagio2(caso: string): Promise<CaseStageStatesResponse> {
  return api.post<CaseStageStatesResponse>(
    `/casos/${encodeURIComponent(caso)}/estagio/2/concluir`,
  )
}

/** Reinicia o estado da campanha (para repetir a demonstração). */
export function resetCaso(caso: string): Promise<CaseStageStatesResponse> {
  return api.post<CaseStageStatesResponse>(
    `/casos/${encodeURIComponent(caso)}/reset`,
  )
}

// ---------------------------------------------------------------------------
// Derivação pura do bloqueio sequencial (Property 1).
// ---------------------------------------------------------------------------

export type WizardStageStatus = "concluido" | "disponivel" | "bloqueado"

export interface WizardDerivedStage {
  estagio: NumeroEstagio
  status: WizardStageStatus
}

export interface WizardDerivedState {
  estagios: WizardDerivedStage[]
  /** Se é possível avançar do estágio informado para o próximo. */
  podeAvancar: (de: NumeroEstagio) => boolean
}

const STAGES: readonly NumeroEstagio[] = [1, 2, 3] as const

/**
 * Deriva, a partir do conjunto de estágios concluídos, o status de cada estágio
 * e a habilitação do avanço. Regra (Req. 1.6, 5.3): o Estágio N (N>1) é
 * `bloqueado` se e somente se N-1 não está concluído; é `disponivel` quando o
 * anterior está concluído (ou é o Estágio 1); e `concluido` quando ele próprio
 * consta como concluído. "Avançar" de N para N+1 só é permitido quando N+1 não
 * está bloqueado — isto é, quando N está concluído.
 */
export function derivarEstados(concluidos: number[]): WizardDerivedState {
  const done = new Set(concluidos)

  const estagios: WizardDerivedStage[] = STAGES.map((estagio) => {
    let status: WizardStageStatus
    if (done.has(estagio)) {
      status = "concluido"
    } else if (estagio === 1 || done.has(estagio - 1)) {
      status = "disponivel"
    } else {
      status = "bloqueado"
    }
    return { estagio, status }
  })

  const statusDe = (n: number): WizardStageStatus | undefined =>
    estagios.find((e) => e.estagio === n)?.status

  const podeAvancar = (de: NumeroEstagio): boolean => {
    const proximo = de + 1
    if (proximo > 3) return false
    // Só avança se o próximo não está bloqueado, o que equivale a "o atual
    // (de) está concluído".
    return statusDe(proximo) !== "bloqueado"
  }

  return { estagios, podeAvancar }
}

/** Extrai a lista de números de estágios `concluido` de um `CasoEstados`. */
export function estagiosConcluidos(estados: CasoEstados | undefined): number[] {
  if (!estados) return []
  return estados.estagios
    .filter((e) => e.estado === "concluido")
    .map((e) => e.estagio)
}
