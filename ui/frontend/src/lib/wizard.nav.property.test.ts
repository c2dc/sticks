import { describe, expect, it } from "vitest"
import fc from "fast-check"

import {
  PIPELINE_STAGES,
  type NumeroEstagio,
} from "@/lib/casos"
import {
  PRIMEIRO_ESTAGIO,
  derivarEstados,
  resetPorReselecao,
  wizardNavReducer,
  type WizardNavState,
} from "@/lib/wizard"

/**
 * Property test da reseleção ao Voltar do wizard de seis Estágios
 * (pipeline-seis-estagios, Req. 4.4, 4.5, 4.6). O reducer `wizardNavReducer` e o
 * helper `resetPorReselecao` são puros e determinísticos, então são exercitados
 * diretamente sobre muitas entradas geradas (fast-check, >= 100 iterações), sem
 * montar o DOM. Os módulos sob teste NÃO são modificados por este teste.
 *
 * Property 12 (design.md): "Para qualquer campanha com Estágios concluídos, se a
 * seleção de um Estágio anterior é alterada após 'Voltar', então os `StageRun` de
 * todos os Estágios posteriores são resetados para `nao_iniciado`, restaurando o
 * bloqueio sequencial a partir do Estágio alterado."
 */

const STAGES: readonly NumeroEstagio[] = PIPELINE_STAGES

/** Arbitrário de um único Estágio válido (1..6). */
const estagioArb: fc.Arbitrary<NumeroEstagio> = fc.constantFrom(...STAGES)

/**
 * Arbitrário de um conjunto de Estágios concluídos "sequencialmente válido":
 * um prefixo 1..k (0 <= k <= 6). O gate sequencial (Req. 3) só admite estados em
 * que, se N > 1 está concluído, N-1 também está — então geramos exatamente esses
 * prefixos, que são os estados realmente alcançáveis pelo pipeline.
 */
const prefixoConcluidosArb: fc.Arbitrary<NumeroEstagio[]> = fc
  .integer({ min: 0, max: STAGES.length })
  .map((k) => STAGES.slice(0, k) as NumeroEstagio[])

describe("wizard — Property 12: Reseleção ao Voltar recalcula os Estágios seguintes", () => {
  // Feature: pipeline-seis-estagios, Property 12: Reseleção ao Voltar recalcula
  // os Estágios seguintes.
  // Validates: Requirements 4.4, 4.5, 4.6
  it("Voltar + alterar + Próximo adota a nova seleção e reseta os posteriores a nao_iniciado", () => {
    fc.assert(
      fc.property(prefixoConcluidosArb, estagioArb, (concluidos, destino) => {
        // Começa com o pipeline avançado até `concluidos` e a tela aberta no
        // último Estágio concluído (ou no primeiro, se nada foi concluído).
        const atualInicial = (concluidos.length > 0
          ? concluidos[concluidos.length - 1]
          : PRIMEIRO_ESTAGIO) as NumeroEstagio

        let estado: WizardNavState = {
          estagioAtual: atualInicial,
          concluidos: [...concluidos],
          reselecionando: false,
        }

        // Volta até um Estágio anterior `destino` (<= atual). Cada "Voltar" recua
        // uma tela e liga o modo de reseleção; nunca abaixo do primeiro Estágio.
        // Garante ao menos um "Voltar" (o cenário é "após 'Voltar'"): mesmo no
        // piso (Estágio 1) o "Voltar" habilita a reseleção sem recuar a tela.
        const alvo = Math.min(destino, atualInicial) as NumeroEstagio
        estado = wizardNavReducer(estado, { tipo: "VOLTAR" })
        while (estado.estagioAtual > alvo) {
          estado = wizardNavReducer(estado, { tipo: "VOLTAR" })
        }
        // "Voltar" por si só não reseta conclusões — apenas habilita a reseleção.
        expect(estado.reselecionando).toBe(true)
        expect(estado.concluidos).toEqual(concluidos)

        const estagioAlterado = estado.estagioAtual

        // Altera a seleção do Estágio atual e aciona "Próximo". O avanço só
        // ocorre quando o Estágio alterado está concluído (gate do Backend).
        const reconfirmado = wizardNavReducer(estado, { tipo: "PROXIMO" })

        const podiaAvancar = derivarEstados(estado.concluidos).podeAvancar(
          estagioAlterado,
        )

        if (!podiaAvancar) {
          // Sem conclusão do Estágio alterado, "Próximo" é no-op: nada muda.
          expect(reconfirmado).toEqual(estado)
          return
        }

        // Req. 4.5/4.6: adota a nova seleção e espelha o reset do Backend sobre
        // os Estágios posteriores (> estagioAlterado), que voltam a
        // `nao_iniciado` (saem de `concluidos`). O Estágio alterado e os
        // anteriores (<= estagioAlterado) são preservados.
        const esperado = concluidos.filter((n) => n <= estagioAlterado)
        expect(reconfirmado.concluidos).toEqual(esperado)
        // Todos os posteriores foram de fato removidos (resetados).
        for (const n of reconfirmado.concluidos) {
          expect(n).toBeLessThanOrEqual(estagioAlterado)
        }
        // A tela avança e o modo de reseleção é encerrado.
        expect(reconfirmado.estagioAtual).toBe(estagioAlterado + 1)
        expect(reconfirmado.reselecionando).toBe(false)
      }),
      { numRuns: 100 },
    )
  })

  // Feature: pipeline-seis-estagios, Property 12: Reseleção ao Voltar recalcula
  // os Estágios seguintes.
  // Validates: Requirement 4.4
  it("enquanto não há reseleção, 'Próximo' preserva a seleção confirmada (apenas avança a tela)", () => {
    fc.assert(
      fc.property(prefixoConcluidosArb, (concluidos) => {
        // Avança no Estágio atual sem jamais acionar "Voltar" (reselecionando=false).
        const atual = (concluidos.length > 0
          ? concluidos[concluidos.length - 1]
          : PRIMEIRO_ESTAGIO) as NumeroEstagio

        const estado: WizardNavState = {
          estagioAtual: atual,
          concluidos: [...concluidos],
          reselecionando: false,
        }

        const proximo = wizardNavReducer(estado, { tipo: "PROXIMO" })

        // Req. 4.4: a seleção confirmada permanece inalterada em qualquer caso —
        // tanto no no-op (gate bloqueia) quanto no avanço de tela.
        expect(proximo.concluidos).toEqual(concluidos)
        expect(proximo.reselecionando).toBe(false)

        if (derivarEstados(concluidos).podeAvancar(atual)) {
          expect(proximo.estagioAtual).toBe(atual + 1)
        } else {
          expect(proximo.estagioAtual).toBe(atual)
        }
      }),
      { numRuns: 100 },
    )
  })

  // Feature: pipeline-seis-estagios, Property 12: Reseleção ao Voltar recalcula
  // os Estágios seguintes.
  // Validates: Requirement 4.6
  it("resetPorReselecao mantém os Estágios <= K e descarta todos os > K", () => {
    fc.assert(
      fc.property(
        // `concluidos` arbitrário (inclui ruído fora de 1..6 e duplicatas) para
        // exercer a normalização defensiva do helper.
        fc.array(fc.integer({ min: -3, max: 9 }), { maxLength: 20 }),
        estagioArb,
        (concluidos, k) => {
          const resultado = resetPorReselecao(concluidos, k)

          // Todo Estágio sobrevivente é válido (1..6) e <= K.
          for (const n of resultado) {
            expect(STAGES).toContain(n)
            expect(n).toBeLessThanOrEqual(k)
          }

          // Nenhum Estágio válido <= K da entrada foi perdido.
          const esperados = [...new Set(concluidos)]
            .filter((n) => STAGES.includes(n as NumeroEstagio) && n <= k)
            .sort((a, b) => a - b)
          expect(resultado).toEqual(esperados)

          // É único e ordenado (invariante da normalização).
          expect(resultado).toEqual([...resultado].sort((a, b) => a - b))
          expect(new Set(resultado).size).toBe(resultado.length)
        },
      ),
      { numRuns: 100 },
    )
  })
})
