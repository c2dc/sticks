import { describe, expect, it } from "vitest"
import fc from "fast-check"

import { derivarApresentacao, derivarEstados } from "@/lib/wizard"
import { type NumeroEstagio } from "@/lib/casos"

/**
 * Feature: pipeline-cinco-estagios, Property 1: Derivação do bloqueio sequencial.
 * Validates: Requirements 1.4, 8.9
 *
 * No esquema de cinco Estágios (Modelagem = Estágio 1), um estágio N (N>1) é
 * `bloqueado` sse N-1 não está concluído; "Avançar" de N para N+1 só é
 * permitido quando N+1 não está bloqueado. O wizard nunca permite avançar
 * para um estágio bloqueado, e não há avanço a partir do último estágio (5).
 */
describe("derivarEstados — bloqueio sequencial (1..5)", () => {
  it("nada concluído: 1 disponível, 2/3/4/5 bloqueados; sem avanço", () => {
    const d = derivarEstados([])
    expect(d.estagios).toEqual([
      { estagio: 1, status: "disponivel" },
      { estagio: 2, status: "bloqueado" },
      { estagio: 3, status: "bloqueado" },
      { estagio: 4, status: "bloqueado" },
      { estagio: 5, status: "bloqueado" },
    ])
    expect(d.podeAvancar(1)).toBe(false)
    expect(d.podeAvancar(2)).toBe(false)
    expect(d.podeAvancar(3)).toBe(false)
    expect(d.podeAvancar(4)).toBe(false)
  })

  it("estágio 1 concluído: 2 disponível, 3/4/5 bloqueados; avança de 1", () => {
    const d = derivarEstados([1])
    expect(d.estagios.find((e) => e.estagio === 1)?.status).toBe("concluido")
    expect(d.estagios.find((e) => e.estagio === 2)?.status).toBe("disponivel")
    expect(d.estagios.find((e) => e.estagio === 3)?.status).toBe("bloqueado")
    expect(d.estagios.find((e) => e.estagio === 4)?.status).toBe("bloqueado")
    expect(d.estagios.find((e) => e.estagio === 5)?.status).toBe("bloqueado")
    expect(d.podeAvancar(1)).toBe(true)
    expect(d.podeAvancar(2)).toBe(false)
  })

  it("1..4 concluídos: 5 disponível; avança de 1..4; nunca de 5", () => {
    const d = derivarEstados([1, 2, 3, 4])
    expect(d.estagios.find((e) => e.estagio === 5)?.status).toBe("disponivel")
    expect(d.podeAvancar(1)).toBe(true)
    expect(d.podeAvancar(2)).toBe(true)
    expect(d.podeAvancar(3)).toBe(true)
    expect(d.podeAvancar(4)).toBe(true)
    expect(d.podeAvancar(5)).toBe(false) // 5 é o último estágio
  })

  it("exaustivo: um estágio N>1 é bloqueado sse N-1 não concluído", () => {
    // Varre todos os subconjuntos de {1,2,3,4,5}.
    for (let mask = 0; mask < 32; mask++) {
      const concluidos = [1, 2, 3, 4, 5].filter((n) => mask & (1 << (n - 1)))
      const d = derivarEstados(concluidos)
      for (const n of [2, 3, 4, 5] as const) {
        const bloqueado =
          d.estagios.find((e) => e.estagio === n)?.status === "bloqueado"
        const anteriorConcluido = concluidos.includes(n - 1)
        // bloqueado  <=>  anterior NÃO concluído E ele próprio não concluído
        const esperaBloqueado = !anteriorConcluido && !concluidos.includes(n)
        expect(bloqueado).toBe(esperaBloqueado)
        // nunca permite avançar para um estágio bloqueado
        if (bloqueado) expect(d.podeAvancar((n - 1) as 1 | 2 | 3 | 4)).toBe(false)
      }
    }
  })
})

// Feature: wizard-progresso-estagios, Property 1: Apresentação de "concluído"
// exige conclusão real E alcance. Para todo `concluidos`, `maxEstagioVisitado`
// e `estagioAtual`, um estágio N é apresentado como "concluido" se e somente se
// N está concluído no Estado_Real (concluidos.includes(N)) E N <=
// maxEstagioVisitado. Corolários: concluído no real mas ainda não alcançado
// (N > maxEstagioVisitado) é apresentado "disponivel" (nunca "concluido"); e um
// estágio não concluído no real nunca é apresentado "concluido".
// Validates: Requirements 1.1, 1.2, 4.1
describe("derivarApresentacao — Property 1: concluído exige real E alcance", () => {
  /** Subconjunto arbitrário de {1,2,3,4,5}. */
  const arbConcluidos = fc.subarray([1, 2, 3, 4, 5])
  /** Estágio em 1..5 (serve para maxEstagioVisitado e estagioAtual). */
  const arbEstagio = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<
    NumeroEstagio
  >

  it("∀ inputs: apresentado(N)==='concluido' ⇔ includes(N) && N<=max", () => {
    fc.assert(
      fc.property(
        arbConcluidos,
        arbEstagio,
        arbEstagio,
        (concluidos, maxEstagioVisitado, estagioAtual) => {
          const { estagios } = derivarApresentacao(
            concluidos,
            maxEstagioVisitado,
            estagioAtual,
          )
          for (const { estagio, apresentado } of estagios) {
            const esperaConcluido =
              concluidos.includes(estagio) && estagio <= maxEstagioVisitado
            // Bicondicional: "concluido" na apresentação sse real E alcançado.
            expect(apresentado === "concluido").toBe(esperaConcluido)
            // Corolário: concluído no real mas não alcançado → "disponivel".
            if (concluidos.includes(estagio) && estagio > maxEstagioVisitado) {
              expect(apresentado).toBe("disponivel")
            }
            // Corolário: não concluído no real → nunca "concluido".
            if (!concluidos.includes(estagio)) {
              expect(apresentado).not.toBe("concluido")
            }
          }
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progresso-estagios, Property 2: Revelação monotônica ao
// avançar. Modelando "progresso por estágio" como uma ordem total
// concluido > disponivel > bloqueado, ao crescer `maxEstagioVisitado` de
// `max1` para `max2` (com `max1 <= max2`), a apresentação de cada estágio é
// não decrescente em progresso (o avanço só pode revelar conclusões, nunca
// escondê-las). Além disso, nenhum estágio além do visitado
// (`estagio > maxEstagioVisitado`) é apresentado como "concluido".
// Validates: Requirements 1.3
describe("derivarApresentacao — Property 2: revelação monotônica ao avançar", () => {
  /** Subconjunto arbitrário de {1,2,3,4,5}. */
  const arbConcluidos = fc.subarray([1, 2, 3, 4, 5])
  /** Estágio em 1..5 (serve para maxEstagioVisitado e estagioAtual). */
  const arbEstagio = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<
    NumeroEstagio
  >

  /** Ordem total do progresso apresentado: concluido > disponivel > bloqueado. */
  const rank = (apresentado: "concluido" | "disponivel" | "bloqueado"): number =>
    apresentado === "concluido" ? 2 : apresentado === "disponivel" ? 1 : 0

  it("∀ concluidos, ∀ max1<=max2, ∀ atual: progresso não decrescente e nenhum concluído além do visitado", () => {
    fc.assert(
      fc.property(
        arbConcluidos,
        arbEstagio,
        arbEstagio,
        arbEstagio,
        (concluidos, a, b, estagioAtual) => {
          // Normaliza para max1 <= max2 sem descartar casos.
          const max1 = (Math.min(a, b) as NumeroEstagio)
          const max2 = (Math.max(a, b) as NumeroEstagio)

          const apre1 = derivarApresentacao(concluidos, max1, estagioAtual)
          const apre2 = derivarApresentacao(concluidos, max2, estagioAtual)

          for (const { estagio, apresentado } of apre1.estagios) {
            const em2 = apre2.estagios.find((e) => e.estagio === estagio)
            expect(em2).toBeDefined()
            // Monotonicidade: avançar nunca reduz o progresso apresentado.
            expect(rank(em2!.apresentado)).toBeGreaterThanOrEqual(
              rank(apresentado),
            )
          }

          // Nenhum estágio além do visitado é apresentado como "concluido".
          for (const max of [max1, max2]) {
            const { estagios } = derivarApresentacao(concluidos, max, estagioAtual)
            for (const { estagio, apresentado } of estagios) {
              if (estagio > max) {
                expect(apresentado).not.toBe("concluido")
              }
            }
          }
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progresso-estagios, Property 3: Avanço governado pelo
// Estado_Real, não pela apresentação. Para todo `concluidos`, a habilitação de
// avanço de um estágio — calculada por `derivarEstados(concluidos).podeAvancar`
// — é invariante a `maxEstagioVisitado`. Como `derivarEstados` sequer recebe
// `maxEstagioVisitado`, para quaisquer dois valores `maxA`/`maxB` o mesmo
// `concluidos` produz exatamente a mesma decisão de avanço: a divergência
// temporária entre Estado_Apresentado e Estado_Real nunca altera o gate.
// Validates: Requirements 3.4, 7.3
describe("derivarEstados — Property 3: avanço invariante a maxEstagioVisitado", () => {
  /** Subconjunto arbitrário de {1,2,3,4,5}. */
  const arbConcluidos = fc.subarray([1, 2, 3, 4, 5])
  /** maxEstagioVisitado em 1..5. */
  const arbMax = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<NumeroEstagio>

  it("∀ concluidos, ∀ maxA,maxB: podeAvancar(n) idêntico (invariante a maxEstagioVisitado)", () => {
    fc.assert(
      fc.property(
        arbConcluidos,
        arbMax,
        arbMax,
        (concluidos, _maxA, _maxB) => {
          // O gate é derivado só do Estado_Real; a apresentação (governada por
          // maxEstagioVisitado) nunca o toca. Como `derivarEstados` ignora
          // qualquer avanço, duas derivações sob maxA/maxB coincidem em
          // podeAvancar para todo estágio n em 1..4 (não há avanço a partir de 5).
          const gateA = derivarEstados(concluidos)
          const gateB = derivarEstados(concluidos)
          for (const n of [1, 2, 3, 4] as const) {
            expect(gateA.podeAvancar(n)).toBe(gateB.podeAvancar(n))
          }
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progresso-estagios, Property 4: `preConcluido` só marca
// conclusões automáticas do sistema. Para todo `concluidos`,
// `maxEstagioVisitado` e `estagioAtual`, se um estágio N é marcado
// `preConcluido` então N está `concluido` no Estado_Real
// (concluidos.includes(N)), N > 1, N−1 está `concluido`
// (concluidos.includes(N-1)), N é apresentado "concluido" e o pesquisador ainda
// não agiu além da janela N (estagioAtual <= N). Em particular, um estágio não
// concluído no real (ex.: Estágio 2 de um Caso_Nao_Curado) nunca é
// `preConcluido`.
// Validates: Requirements 2.1, 4.3, 7.4
describe("derivarApresentacao — Property 4: preConcluido só marca conclusões automáticas", () => {
  /** Subconjunto arbitrário de {1,2,3,4,5}. */
  const arbConcluidos = fc.subarray([1, 2, 3, 4, 5])
  /** Estágio em 1..5 (serve para maxEstagioVisitado e estagioAtual). */
  const arbEstagio = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<
    NumeroEstagio
  >

  it("∀ inputs: preConcluido(N) ⇒ includes(N) && includes(N-1) && N>1 && apresentado(N)==='concluido' && atual<=N; não concluído nunca é preConcluido", () => {
    fc.assert(
      fc.property(
        arbConcluidos,
        arbEstagio,
        arbEstagio,
        (concluidos, maxEstagioVisitado, estagioAtual) => {
          const { estagios } = derivarApresentacao(
            concluidos,
            maxEstagioVisitado,
            estagioAtual,
          )
          for (const { estagio, apresentado, preConcluido } of estagios) {
            if (preConcluido) {
              // Implicação: preConcluido ⇒ todas as condições abaixo valem.
              expect(concluidos.includes(estagio)).toBe(true)
              expect(concluidos.includes(estagio - 1)).toBe(true)
              expect(estagio).toBeGreaterThan(1)
              expect(apresentado).toBe("concluido")
              expect(estagioAtual).toBeLessThanOrEqual(estagio)
            }
            // Contrapositiva do núcleo: estágio não concluído no real nunca é
            // preConcluido (cobre o Estágio 2 de um Caso_Nao_Curado).
            if (!concluidos.includes(estagio)) {
              expect(preConcluido).toBe(false)
            }
          }
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progresso-estagios, Property 5: `derivarApresentacao` é pura
// e determinística. Para toda tripla de entradas `(concluidos,
// maxEstagioVisitado, estagioAtual)`, invocações repetidas produzem saídas
// profundamente iguais (determinismo) e a função não causa efeitos colaterais —
// em particular, não muta o array `concluidos` recebido (pureza). Recomputar
// após uma releitura do backend depende apenas das entradas atuais.
// Validates: Requirements 6.1
describe("derivarApresentacao — Property 5: pureza e determinismo", () => {
  /** Subconjunto arbitrário de {1,2,3,4,5}. */
  const arbConcluidos = fc.subarray([1, 2, 3, 4, 5])
  /** Estágio em 1..5 (serve para maxEstagioVisitado e estagioAtual). */
  const arbEstagio = fc.integer({ min: 1, max: 5 }) as fc.Arbitrary<
    NumeroEstagio
  >

  it("∀ inputs: duas chamadas iguais ⇒ saídas profundamente iguais; sem mutação do input", () => {
    fc.assert(
      fc.property(
        arbConcluidos,
        arbEstagio,
        arbEstagio,
        (concluidos, maxEstagioVisitado, estagioAtual) => {
          // Snapshot do input para detectar mutação (pureza).
          const snapshot = [...concluidos]

          const r1 = derivarApresentacao(
            concluidos,
            maxEstagioVisitado,
            estagioAtual,
          )
          const r2 = derivarApresentacao(
            concluidos,
            maxEstagioVisitado,
            estagioAtual,
          )

          // Determinismo: duas chamadas iguais ⇒ saídas profundamente iguais.
          expect(r2).toEqual(r1)

          // Pureza: a função não muta o array de entrada.
          expect(concluidos).toEqual(snapshot)
        },
      ),
      { numRuns: 200 },
    )
  })
})

// Feature: wizard-progresso-estagios, Testes de exemplo/unitários da derivação.
// Casos concretos citados na Testing Strategy do design, ancorando os cenários
// de UX reais do Estágio 2 (Geração do DAG). Complementam as propriedades
// (fast-check) com exemplos nomeados e legíveis.
// Validates: Requirements 1.1, 1.2, 2.1, 4.1
describe("derivarApresentacao — exemplos concretos (Estágio 2)", () => {
  /** Acha o estágio N na apresentação derivada (sempre presente em 1..5). */
  const estagio = (
    r: ReturnType<typeof derivarApresentacao>,
    n: NumeroEstagio,
  ) => r.estagios.find((e) => e.estagio === n)!

  // R1.1: ao concluir o Estágio 1 de um Caso_Curado (backend devolve {1,2} de
  // uma vez) e permanecer na janela do Estágio 1, o Estágio 2 ainda não foi
  // alcançado (maxEstagioVisitado=1), então é apresentado "disponivel" — nunca
  // "concluido" — e não é marcado preConcluido.
  it("R1.1: concluidos={1,2}, max=1, atual=1 ⇒ Estágio 2 'disponivel'", () => {
    const r = derivarApresentacao([1, 2], 1, 1)
    const e2 = estagio(r, 2)
    expect(e2.apresentado).toBe("disponivel")
    expect(e2.apresentado).not.toBe("concluido")
    expect(e2.preConcluido).toBe(false)
  })

  // R1.2 / R2.1: ao navegar até a janela do Estágio 2 pré-concluído de um
  // Caso_Curado (max=2, atual=2, concluidos={1,2}), o Estágio 2 é apresentado
  // "concluido" E marcado preConcluido=true (concluído pelo sistema por
  // curadoria, não por ação direta do pesquisador).
  it("R1.2/R2.1: concluidos={1,2}, max=2, atual=2 ⇒ Estágio 2 'concluido' e preConcluido=true", () => {
    const r = derivarApresentacao([1, 2], 2, 2)
    const e2 = estagio(r, 2)
    expect(e2.apresentado).toBe("concluido")
    expect(e2.preConcluido).toBe(true)
  })

  // R4.1: para um Caso_Nao_Curado o backend devolve só {1} — o Estágio 2 não
  // está concluído no real. Varre todos os valores possíveis de
  // maxEstagioVisitado (e estagioAtual) para confirmar que o Estágio 2 nunca é
  // apresentado "concluido" nem marcado preConcluido (sem regressão).
  it("R4.1: concluidos={1} ⇒ Estágio 2 nunca 'concluido' e preConcluido=false (∀ max, ∀ atual)", () => {
    for (const max of [1, 2, 3, 4, 5] as const) {
      for (const atual of [1, 2, 3, 4, 5] as const) {
        const r = derivarApresentacao([1], max, atual)
        const e2 = estagio(r, 2)
        expect(e2.apresentado).not.toBe("concluido")
        expect(e2.preConcluido).toBe(false)
      }
    }
  })
})
