import { describe, expect, it } from "vitest"

import {
  PRIMEIRO_ESTAGIO,
  ULTIMO_ESTAGIO,
  estadoNavInicial,
  resetPorReselecao,
  wizardNavReducer,
  type WizardNavState,
} from "@/lib/wizard"
import { type NumeroEstagio } from "@/lib/casos"

/**
 * Testes de exemplo da navegação Próximo/Voltar com reseleção do wizard de seis
 * Estágios (pipeline-seis-estagios, Req. 4.2–4.6). O reducer `wizardNavReducer`
 * e o helper `resetPorReselecao` são puros, então são exercitados diretamente,
 * sem montar o DOM (o Property test da reseleção vive em task 13.3).
 */

/** Constrói um estado de navegação a partir de campos parciais. */
function navState(parcial: Partial<WizardNavState>): WizardNavState {
  return { ...estadoNavInicial, ...parcial }
}

describe("wizard — constantes de range (1..6)", () => {
  it("primeiro é 1 e último é 6", () => {
    expect(PRIMEIRO_ESTAGIO).toBe(1)
    expect(ULTIMO_ESTAGIO).toBe(6)
  })
})

describe("wizardNavReducer — PROXIMO (Req. 4.2, bloqueio sequencial)", () => {
  it("não avança quando o Estágio atual não está concluído (no-op)", () => {
    const estado = navState({ estagioAtual: 1, concluidos: [] })
    const proximo = wizardNavReducer(estado, { tipo: "PROXIMO" })
    // Gate derivado do backend: Estágio 1 não concluído ⇒ não avança.
    expect(proximo).toEqual(estado)
  })

  it("avança quando o Estágio atual está concluído; preserva a seleção (Req. 4.4)", () => {
    const estado = navState({ estagioAtual: 1, concluidos: [1] })
    const proximo = wizardNavReducer(estado, { tipo: "PROXIMO" })
    expect(proximo.estagioAtual).toBe(2)
    // Enquanto não houve "Voltar", a seleção confirmada permanece inalterada.
    expect(proximo.concluidos).toEqual([1])
    expect(proximo.reselecionando).toBe(false)
  })

  it("nunca avança além do último Estágio (6)", () => {
    const estado = navState({
      estagioAtual: 6,
      concluidos: [1, 2, 3, 4, 5, 6],
    })
    const proximo = wizardNavReducer(estado, { tipo: "PROXIMO" })
    expect(proximo).toEqual(estado) // no-op: 6 é o último
  })
})

describe("wizardNavReducer — VOLTAR (Req. 4.3)", () => {
  it("retorna ao Estágio anterior e entra em modo de reseleção", () => {
    const estado = navState({ estagioAtual: 3, concluidos: [1, 2, 3] })
    const voltado = wizardNavReducer(estado, { tipo: "VOLTAR" })
    expect(voltado.estagioAtual).toBe(2)
    expect(voltado.reselecionando).toBe(true)
    // "Voltar" por si só não reseta nada — o reset só ocorre ao reconfirmar.
    expect(voltado.concluidos).toEqual([1, 2, 3])
  })

  it("no primeiro Estágio é piso: não recua, mas habilita reseleção", () => {
    const estado = navState({ estagioAtual: 1, concluidos: [1] })
    const voltado = wizardNavReducer(estado, { tipo: "VOLTAR" })
    expect(voltado.estagioAtual).toBe(1)
    expect(voltado.reselecionando).toBe(true)
  })
})

describe("wizardNavReducer — reseleção Voltar + alterar + Próximo (Req. 4.5, 4.6)", () => {
  it("ao reconfirmar após Voltar, adota a nova seleção e reseta os posteriores", () => {
    // Pesquisador tinha 1..4 concluídos, estava no Estágio 4.
    let estado: WizardNavState = navState({
      estagioAtual: 4,
      concluidos: [1, 2, 3, 4],
    })
    // Volta para o Estágio 2 (duas vezes "Voltar").
    estado = wizardNavReducer(estado, { tipo: "VOLTAR" }) // -> 3
    estado = wizardNavReducer(estado, { tipo: "VOLTAR" }) // -> 2
    expect(estado.estagioAtual).toBe(2)
    expect(estado.reselecionando).toBe(true)

    // Altera a seleção do Estágio 2 e aciona "Próximo": adota a nova seleção e
    // o backend reseta os Estágios posteriores (> 2) para nao_iniciado.
    const reconfirmado = wizardNavReducer(estado, { tipo: "PROXIMO" })
    expect(reconfirmado.estagioAtual).toBe(3)
    expect(reconfirmado.concluidos).toEqual([1, 2]) // 3 e 4 resetados
    expect(reconfirmado.reselecionando).toBe(false)
  })

  it("após o reset, o avanço volta a respeitar o bloqueio sequencial", () => {
    // Depois de reselecionar no Estágio 2 (concluidos=[1,2], atual=3), não há
    // como avançar do Estágio 3 porque ele ainda não foi concluído.
    const estado = navState({ estagioAtual: 3, concluidos: [1, 2] })
    const semAvancar = wizardNavReducer(estado, { tipo: "PROXIMO" })
    expect(semAvancar).toEqual(estado) // no-op: Estágio 3 não concluído
  })
})

describe("wizardNavReducer — SINCRONIZAR_BACKEND (fonte da verdade)", () => {
  it("substitui concluidos pela releitura do backend, normalizada", () => {
    const estado = navState({ estagioAtual: 3, concluidos: [1] })
    const sinc = wizardNavReducer(estado, {
      tipo: "SINCRONIZAR_BACKEND",
      // Fora de ordem, com duplicata e número inválido (7 e 0 descartados).
      concluidos: [3, 1, 2, 2, 7, 0],
    })
    expect(sinc.concluidos).toEqual([1, 2, 3])
    // Navegação intacta.
    expect(sinc.estagioAtual).toBe(3)
    expect(sinc.reselecionando).toBe(false)
  })
})

describe("resetPorReselecao — espelha o reset do backend (Req. 4.6)", () => {
  it("remove todos os Estágios posteriores ao alterado; mantém anteriores", () => {
    expect(resetPorReselecao([1, 2, 3, 4, 5, 6], 2)).toEqual([1, 2])
    expect(resetPorReselecao([1, 2, 3], 3)).toEqual([1, 2, 3]) // nada posterior
    expect(resetPorReselecao([1, 2, 3, 4], 1)).toEqual([1])
  })

  it("é pura: não muta a entrada e normaliza (único, ordenado, 1..6)", () => {
    const entrada = [4, 2, 1, 2, 9, 0]
    const copia = [...entrada]
    const saida = resetPorReselecao(entrada, 3)
    expect(saida).toEqual([1, 2]) // 4 posterior removido; 9/0 descartados
    expect(entrada).toEqual(copia) // sem mutação
  })
})
