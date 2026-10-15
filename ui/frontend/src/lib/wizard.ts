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
 * badges e habilitação do botão "Avançar" (Property 1). Com o pipeline de seis
 * Estágios (pipeline-seis-estagios, Req. 1.1/1.2), a derivação passa a cobrir
 * 1..6 automaticamente, pois é range-agnóstica (deriva de `PIPELINE_STAGES`).
 * `wizardNavReducer`/`resetPorReselecao` adicionam a navegação Próximo/Voltar
 * com reseleção (Req. 4.2–4.6).
 */
import { api } from "@/lib/api"
import {
  PIPELINE_STAGES,
  type Caso,
  type CasoEstados,
  type NumeroEstagio,
} from "@/lib/casos"
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

const STAGES: readonly NumeroEstagio[] = PIPELINE_STAGES

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
 * fora de 1..6 são ignorados pela base `derivarEstados`; `concluidos` vazio
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

// ---------------------------------------------------------------------------
// Navegação Próximo/Voltar com reseleção (pipeline-seis-estagios, Req. 4.2–4.6).
//
// O wizard conduz 6 Estágios, uma tela por Estágio. Este módulo é a máquina de
// estado PURA da navegação (Próximo/Voltar) e da reseleção, exportada separada
// do componente para que os invariantes de navegação possam ser verificados por
// property tests sem montar o DOM (Property 12).
//
// Fonte da verdade do RESET: o Backend. O frontend não invalida `StageRun`
// localmente — ele apenas modela a INTENÇÃO (navegar, confirmar, reselecionar) e
// deriva o conjunto de Estágios concluídos que o Backend recalcula após uma
// reseleção. A regra do Backend (Req. 4.6): reselecionar um Estágio anterior K
// reseta os `StageRun` de todos os Estágios posteriores (> K) para
// `nao_iniciado`, restaurando o bloqueio sequencial a partir de K. A função
// `resetPorReselecao` espelha exatamente essa regra para que a UI possa derivar
// o próximo estado antes/depois da releitura do Backend.
// ---------------------------------------------------------------------------

/** Primeiro e último Estágios do pipeline (derivados de `STAGES`). */
export const PRIMEIRO_ESTAGIO: NumeroEstagio = STAGES[0]
export const ULTIMO_ESTAGIO: NumeroEstagio = STAGES[STAGES.length - 1]

/**
 * Estado puro da navegação do wizard de 6 Estágios.
 *
 * - `estagioAtual`: a tela de Estágio aberta no momento (1..6).
 * - `concluidos`: o conjunto (ordenado, sem duplicatas) de Estágios `concluido`
 *   conforme o Backend — a fonte da verdade do gate e do reset.
 * - `reselecionando`: `true` após acionar "Voltar", sinalizando que a próxima
 *   confirmação (via "Próximo") deve ser tratada como uma reseleção que
 *   recalcula os Estágios seguintes. Enquanto `false`, a seleção já confirmada
 *   permanece inalterada (Req. 4.4).
 */
export interface WizardNavState {
  estagioAtual: NumeroEstagio
  concluidos: NumeroEstagio[]
  reselecionando: boolean
}

/** Eventos aceitos pela navegação do wizard (nomes pt-BR do contrato). */
export type WizardNavEvent =
  /** "Próximo": confirma o Estágio atual e avança para o seguinte. */
  | { tipo: "PROXIMO" }
  /** "Voltar": retorna ao Estágio anterior, habilitando a reseleção. */
  | { tipo: "VOLTAR" }
  /**
   * Reconcilia `concluidos` com a releitura do Backend (fonte da verdade). A UI
   * dispara isto após cada ação assíncrona (concluir Estágio, selecionar, etc.).
   */
  | { tipo: "SINCRONIZAR_BACKEND"; concluidos: number[] }

/** Normaliza um conjunto de Estágios concluídos: só 1..6, único, ordenado. */
function normalizarConcluidos(concluidos: number[]): NumeroEstagio[] {
  const validos = new Set<NumeroEstagio>()
  for (const n of concluidos) {
    if (STAGES.includes(n as NumeroEstagio)) validos.add(n as NumeroEstagio)
  }
  return [...validos].sort((a, b) => a - b)
}

/**
 * Espelha a regra de reset do Backend (Req. 4.6): ao reselecionar/alterar o
 * Estágio `estagioAlterado`, todos os Estágios POSTERIORES (> `estagioAlterado`)
 * têm seus `StageRun` resetados para `nao_iniciado`, logo saem do conjunto de
 * concluídos. O próprio Estágio alterado e os anteriores são preservados.
 *
 * Pura e determinística; não muta a entrada. Defensiva: números fora de 1..6 em
 * `concluidos` são descartados pela normalização.
 */
export function resetPorReselecao(
  concluidos: number[],
  estagioAlterado: NumeroEstagio,
): NumeroEstagio[] {
  return normalizarConcluidos(concluidos).filter((n) => n <= estagioAlterado)
}

/** Estado inicial da navegação: Estágio 1 aberto, nada concluído. */
export const estadoNavInicial: WizardNavState = {
  estagioAtual: PRIMEIRO_ESTAGIO,
  concluidos: [],
  reselecionando: false,
}

/**
 * Reducer puro e determinístico da navegação Próximo/Voltar com reseleção.
 *
 * Regras (Req. 4.2–4.6):
 *
 * - `PROXIMO` (Req. 4.2): avança `estagioAtual` para o seguinte **apenas** se o
 *   gate derivado do Backend permite (`derivarEstados(concluidos).podeAvancar`),
 *   isto é, o Estágio atual está concluído e não é o último. Caso contrário é
 *   no-op (não avança sem concluir).
 *   - Quando NÃO está reselecionando (Req. 4.4): a seleção já confirmada
 *     permanece — `concluidos` fica inalterado; apenas a tela avança.
 *   - Quando ESTÁ reselecionando (Req. 4.5, 4.6): adota a nova seleção do
 *     Estágio atual e aplica o reset do Backend sobre os posteriores
 *     (`resetPorReselecao(concluidos, estagioAtual)`), encerrando o modo de
 *     reseleção. Os Estágios seguintes voltam a `nao_iniciado` (bloqueio
 *     sequencial restaurado), a serem recalculados a partir da nova seleção.
 *
 * - `VOLTAR` (Req. 4.3): retorna ao Estágio anterior (piso em
 *   {@link PRIMEIRO_ESTAGIO}, onde é no-op) e entra em modo de reseleção, de
 *   forma que uma eventual alteração + "Próximo" seja adotada como nova seleção.
 *
 * - `SINCRONIZAR_BACKEND`: substitui `concluidos` pela releitura do Backend
 *   (normalizada). Não mexe em `estagioAtual` nem em `reselecionando`.
 *
 * O reducer nunca inventa conclusões: `concluidos` só muda por reset de
 * reseleção (removendo posteriores) ou por sincronização com o Backend.
 */
export function wizardNavReducer(
  estado: WizardNavState,
  evento: WizardNavEvent,
): WizardNavState {
  switch (evento.tipo) {
    case "PROXIMO": {
      const { podeAvancar } = derivarEstados(estado.concluidos)
      // Gate derivado do Backend: só avança se o Estágio atual está concluído
      // (e não é o último). Sem isso, "Próximo" é no-op (Req. 4.2, bloqueio).
      if (!podeAvancar(estado.estagioAtual)) return estado

      const proximo = (estado.estagioAtual + 1) as NumeroEstagio

      if (!estado.reselecionando) {
        // Req. 4.4: enquanto não houve "Voltar", a seleção confirmada persiste;
        // apenas avança a tela.
        return { ...estado, estagioAtual: proximo }
      }

      // Req. 4.5/4.6: reseleção confirmada — adota a nova seleção do Estágio
      // atual e espelha o reset do Backend sobre os Estágios posteriores.
      return {
        estagioAtual: proximo,
        concluidos: resetPorReselecao(estado.concluidos, estado.estagioAtual),
        reselecionando: false,
      }
    }

    case "VOLTAR": {
      // Req. 4.3: retorna ao Estágio anterior (piso no primeiro) e habilita a
      // reseleção para a próxima confirmação.
      if (estado.estagioAtual <= PRIMEIRO_ESTAGIO) {
        return { ...estado, reselecionando: true }
      }
      return {
        ...estado,
        estagioAtual: (estado.estagioAtual - 1) as NumeroEstagio,
        reselecionando: true,
      }
    }

    case "SINCRONIZAR_BACKEND":
      // Reconcilia com a fonte da verdade (Backend). Não altera a navegação.
      return { ...estado, concluidos: normalizarConcluidos(evento.concluidos) }

    default:
      return estado
  }
}
