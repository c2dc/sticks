/**
 * Wizard de Execução Guiada — wrappers de API e derivação de estado.
 *
 * No esquema de cinco Estágios (pipeline-cinco-estagios, Req. 1.x/8.x), o
 * pipeline é: Estágio 1 = Modelagem Estrutural, Estágio 2 = Geração do DAG (sem
 * "executar"; a conclusão vem de `escolherDag` em `dag.ts`), Estágio 3 =
 * Operacionalização, Estágio 4 = Provisionamento do Ambiente, Estágio 5 =
 * Execução da Campanha. Este módulo fecha as pontas do fluxo guiado
 * reaproveitando os wrappers já corretos de `estagios.ts`:
 * - disparar a Modelagem (Estágio 1 → `executarEstagio1` → POST /estagio/1/executar);
 * - concluir a Operacionalização (Estágio 3 → `concluirEstagio3` → POST /estagio/3/concluir;
 *   o material é curado, então "avançar" = "concluir");
 * - resetar a campanha para repetir a demonstração;
 * - reler o estado por estágio do caso (fonte da verdade = backend).
 *
 * `derivarEstados` centraliza a regra do bloqueio sequencial no frontend, para
 * badges e habilitação do botão "Avançar" (Property 1), agora sobre 1..5.
 */
import { api } from "@/lib/api"
import { type Caso, type CasoEstados, type NumeroEstagio } from "@/lib/casos"
import { executarEstagio1, concluirEstagio3 } from "@/lib/estagios"

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

/**
 * Dispara a Modelagem Estrutural, que no esquema de cinco Estágios é o
 * **Estágio 1** (POST /estagio/1/executar), ponto de partida do pipeline.
 * Reaproveita o wrapper já correto de `estagios.ts`.
 */
export const executarModelagem = executarEstagio1

/**
 * Conclui a Operacionalização, que no esquema de cinco Estágios é o
 * **Estágio 3** (POST /estagio/3/concluir), destravando o Estágio 4. Reaproveita
 * o wrapper já correto de `estagios.ts`.
 */
export const concluirTraducao = concluirEstagio3

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

const STAGES: readonly NumeroEstagio[] = [1, 2, 3, 4, 5] as const

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
    if (proximo > STAGES.length) return false
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

// ---------------------------------------------------------------------------
// Derivação pura da apresentação de progresso (wizard-progresso-estagios).
//
// Camada de apresentação apenas: NÃO governa o gate sequencial nem a habilitação
// de "Avançar" (isso continua sendo responsabilidade de `derivarEstados`). Esta
// derivação reutiliza o `status` de `derivarEstados(concluidos)` como base e o
// combina com o avanço do pesquisador (`maxEstagioVisitado`) para produzir o
// Estado_Apresentado de cada estágio, além de sinalizar origem pré-concluída.
// ---------------------------------------------------------------------------

/** Como a badge de um estágio deve ser apresentada (camada de apresentação). */
export type EstadoApresentado =
  | "concluido" // alcançado e concluído no backend
  | "disponivel" // pronto/alcançável, ainda não "concluído" visualmente
  | "bloqueado" // predecessor não concluído

export interface EstagioApresentado {
  estagio: NumeroEstagio
  apresentado: EstadoApresentado
  /** True quando a badge deve marcar "pré-concluído (curado)". */
  preConcluido: boolean
}

export interface ApresentacaoDerivada {
  estagios: EstagioApresentado[]
}

/**
 * Deriva o Estado_Apresentado de cada estágio a partir do Estado_Real
 * (`concluidos`), do avanço do pesquisador (`maxEstagioVisitado`) e da janela
 * atualmente aberta (`estagioAtual`).
 *
 * Reutiliza `derivarEstados(concluidos)` para o `status` base (nunca
 * reimplementa o gate), garantindo que a apresentação jamais contradiga o gate:
 * um estágio `bloqueado` no gate é sempre `bloqueado` na apresentação.
 *
 * Regra por estágio N (seja `status` = derivarEstados(concluidos).status):
 *   - status === "concluido" E N <= maxEstagioVisitado  → "concluido"
 *   - status === "concluido" E N  > maxEstagioVisitado  → "disponivel"
 *       (conclusão real ainda não revelada — revelação gradual, opção A)
 *   - caso contrário                                    → status
 *       ("disponivel" ou "bloqueado", repassado de derivarEstados)
 *
 * `preConcluido(N)` ≙ `apresentado(N) === "concluido"` ∧ `N > 1` ∧
 * `concluidos.includes(N)` ∧ `concluidos.includes(N-1)` ∧ `estagioAtual <= N`.
 * Marca, apenas na apresentação, um estágio concluído pelo sistema (curadoria)
 * sem que o pesquisador tenha agido dentro da janela N.
 *
 * Função pura e determinística, sem efeitos. Defensiva: `concluidos` com números
 * fora de 1..5 são ignorados pela base `derivarEstados`; `concluidos` vazio
 * produz apresentação toda `disponivel`/`bloqueado` conforme o gate.
 */
export function derivarApresentacao(
  concluidos: number[],
  maxEstagioVisitado: NumeroEstagio,
  estagioAtual: NumeroEstagio,
): ApresentacaoDerivada {
  const done = new Set(concluidos)
  const base = derivarEstados(concluidos)

  const estagios: EstagioApresentado[] = base.estagios.map(
    ({ estagio, status }) => {
      let apresentado: EstadoApresentado
      if (status === "concluido" && estagio <= maxEstagioVisitado) {
        apresentado = "concluido"
      } else if (status === "concluido") {
        // Concluído no real, porém ainda não alcançado: revelação gradual.
        apresentado = "disponivel"
      } else {
        // "disponivel" ou "bloqueado", repassado do gate.
        apresentado = status
      }

      const preConcluido =
        apresentado === "concluido" &&
        estagio > 1 &&
        done.has(estagio) &&
        done.has(estagio - 1) &&
        estagioAtual <= estagio

      return { estagio, apresentado, preConcluido }
    },
  )

  return { estagios }
}
