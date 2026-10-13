import { describe, expect, it } from "vitest"

import i18n from "@/i18n"

/**
 * i18n terminology test for the five-stage scheme
 * (pipeline-cinco-estagios, task 10.4).
 *
 * Asserts, against the loaded i18n resources, that:
 * - the five `stages.{1..5}.name` keys exist in both pt-BR and en;
 * - pt-BR stage names use the term "Estágio" and carry the design names
 *   (Modelagem Estrutural / Geração do DAG / Operacionalização /
 *   Provisionamento do Ambiente / Execução da Campanha);
 * - the en equivalents use the term "Stage" and the English design names;
 * - pt-BR/en keep key parity for the stage short/name keys.
 *
 * This test does NOT add or modify any i18n keys (the JSON is owned by the
 * locale files); it only reads the resources and the terminology they declare.
 */

const STAGES = [1, 2, 3, 4, 5] as const

const PT_NAMES: Record<(typeof STAGES)[number], string> = {
  1: "Modelagem Estrutural",
  2: "Geração do DAG",
  3: "Operacionalização",
  4: "Provisionamento do Ambiente",
  5: "Execução da Campanha",
}

const EN_NAMES: Record<(typeof STAGES)[number], string> = {
  1: "Structural Modeling",
  2: "DAG Generation",
  3: "Operationalization",
  4: "Environment Provisioning",
  5: "Campaign Execution",
}

function resource(lng: string, key: string): string {
  return i18n.getResource(lng, "pipeline", key) as string
}

describe("i18n — terminologia 'Estágio'/'Stage' em stages.1..5", () => {
  it("pt-BR expõe os cinco stages com 'Estágio' e os nomes de design", () => {
    for (const n of STAGES) {
      const name = resource("pt-BR", `stages.${n}.name`)
      const short = resource("pt-BR", `stages.${n}.short`)
      expect(name, `stages.${n}.name deve existir em pt-BR`).toBeTruthy()
      expect(short, `stages.${n}.short deve existir em pt-BR`).toBeTruthy()
      // Terminologia pt-BR: "Estágio".
      expect(name).toContain("Estágio")
      expect(short).toContain("Estágio")
      // Nome de design do estágio.
      expect(name).toContain(PT_NAMES[n])
    }
  })

  it("en expõe os cinco stages com 'Stage' e os nomes de design em inglês", () => {
    for (const n of STAGES) {
      const name = resource("en", `stages.${n}.name`)
      const short = resource("en", `stages.${n}.short`)
      expect(name, `stages.${n}.name deve existir em en`).toBeTruthy()
      expect(short, `stages.${n}.short deve existir em en`).toBeTruthy()
      // Terminologia en: "Stage".
      expect(name).toContain("Stage")
      expect(short).toContain("Stage")
      expect(name).toContain(EN_NAMES[n])
    }
  })

  it("não existe um sexto estágio (o esquema é exatamente 1..5)", () => {
    expect(resource("pt-BR", "stages.6.name")).toBeUndefined()
    expect(resource("en", "stages.6.name")).toBeUndefined()
  })

  it("mantém paridade de chaves de stages entre pt-BR e en", () => {
    for (const n of STAGES) {
      expect(resource("pt-BR", `stages.${n}.name`)).toBeTruthy()
      expect(resource("en", `stages.${n}.name`)).toBeTruthy()
      expect(resource("pt-BR", `stages.${n}.short`)).toBeTruthy()
      expect(resource("en", `stages.${n}.short`)).toBeTruthy()
    }
  })
})
