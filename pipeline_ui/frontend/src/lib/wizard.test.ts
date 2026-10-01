import { describe, expect, it } from "vitest"

import { derivarEstados } from "@/lib/wizard"

/**
 * Feature: wizard-execucao-guiada, Property 1: Derivação do bloqueio sequencial.
 * Validates: Requirements 1.6, 5.3
 *
 * Um estágio N (N>1) é `bloqueado` sse N-1 não está concluído; "Avançar" de N
 * para N+1 só é permitido quando N+1 não está bloqueado. O wizard nunca permite
 * avançar para um estágio bloqueado.
 */
describe("derivarEstados — bloqueio sequencial", () => {
  it("nada concluído: 1 disponível, 2 e 3 bloqueados; sem avanço", () => {
    const d = derivarEstados([])
    expect(d.estagios).toEqual([
      { estagio: 1, status: "disponivel" },
      { estagio: 2, status: "bloqueado" },
      { estagio: 3, status: "bloqueado" },
    ])
    expect(d.podeAvancar(1)).toBe(false)
    expect(d.podeAvancar(2)).toBe(false)
  })

  it("estágio 1 concluído: 2 disponível, 3 bloqueado; avança de 1", () => {
    const d = derivarEstados([1])
    expect(d.estagios.find((e) => e.estagio === 1)?.status).toBe("concluido")
    expect(d.estagios.find((e) => e.estagio === 2)?.status).toBe("disponivel")
    expect(d.estagios.find((e) => e.estagio === 3)?.status).toBe("bloqueado")
    expect(d.podeAvancar(1)).toBe(true)
    expect(d.podeAvancar(2)).toBe(false)
  })

  it("1 e 2 concluídos: 3 disponível; avança de 1 e de 2", () => {
    const d = derivarEstados([1, 2])
    expect(d.estagios.find((e) => e.estagio === 3)?.status).toBe("disponivel")
    expect(d.podeAvancar(1)).toBe(true)
    expect(d.podeAvancar(2)).toBe(true)
    expect(d.podeAvancar(3)).toBe(false) // não há estágio 4
  })

  it("exaustivo: um estágio N>1 é bloqueado sse N-1 não concluído", () => {
    // Varre todos os subconjuntos de {1,2,3}.
    for (let mask = 0; mask < 8; mask++) {
      const concluidos = [1, 2, 3].filter((n) => mask & (1 << (n - 1)))
      const d = derivarEstados(concluidos)
      for (const n of [2, 3] as const) {
        const bloqueado = d.estagios.find((e) => e.estagio === n)?.status === "bloqueado"
        const anteriorConcluido = concluidos.includes(n - 1)
        // bloqueado  <=>  anterior NÃO concluído E ele próprio não concluído
        const esperaBloqueado = !anteriorConcluido && !concluidos.includes(n)
        expect(bloqueado).toBe(esperaBloqueado)
        // nunca permite avançar para um estágio bloqueado
        if (bloqueado) expect(d.podeAvancar((n - 1) as 1 | 2)).toBe(false)
      }
    }
  })
})
