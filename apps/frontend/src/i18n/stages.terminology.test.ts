import { describe, expect, it } from "vitest"

import i18n from "@/i18n"

/**
 * i18n terminology test for the six-stage scheme
 * (pipeline-six-stages, task 14.5).
 *
 * Asserts, against the loaded i18n resources, that:
 * - the six `stages.{1..6}.name` keys exist in both pt-BR and en;
 * - pt-BR stage names use the term "Estágio" and carry the design names
 *   (Seleção da base STIX / Seleção da campanha / Geração do DAG /
 *   Operacionalização / Criação do ambiente de emulação /
 *   Execução, medição e relatório);
 * - the en equivalents use the term "Stage" and the English design names;
 * - pt-BR/en keep key parity for the stage short/name keys.
 *
 * This test does NOT add or modify any i18n keys (the JSON is owned by the
 * locale files); it only reads the resources and the terminology they declare.
 */

const STAGES = [1, 2, 3, 4, 5, 6] as const

const PT_NAMES: Record<(typeof STAGES)[number], string> = {
  1: "Seleção da base STIX",
  2: "Seleção da campanha",
  3: "Geração do DAG",
  4: "Operacionalização",
  5: "Criação do ambiente de emulação",
  6: "Execução, medição e relatório",
}

const EN_NAMES: Record<(typeof STAGES)[number], string> = {
  1: "STIX Base Selection",
  2: "Campaign Selection",
  3: "DAG Generation",
  4: "Operationalization",
  5: "Emulation environment setup",
  6: "Execution, Measurement and Report",
}

function resource(lng: string, key: string): string {
  return i18n.getResource(lng, "pipeline", key) as string
}

describe("i18n — 'Estágio'/'Stage' terminology in stages.1..6", () => {
  it("pt-BR exposes the six stages with 'Estágio' and the design names", () => {
    for (const n of STAGES) {
      const name = resource("pt-BR", `stages.${n}.name`)
      const short = resource("pt-BR", `stages.${n}.short`)
      expect(name, `stages.${n}.name must exist in pt-BR`).toBeTruthy()
      expect(short, `stages.${n}.short must exist in pt-BR`).toBeTruthy()
      // pt-BR terminology: "Estágio".
      expect(name).toContain("Estágio")
      expect(short).toContain("Estágio")
      // Design name of the stage.
      expect(name).toContain(PT_NAMES[n])
    }
  })

  it("en exposes the six stages with 'Stage' and the English design names", () => {
    for (const n of STAGES) {
      const name = resource("en", `stages.${n}.name`)
      const short = resource("en", `stages.${n}.short`)
      expect(name, `stages.${n}.name must exist in en`).toBeTruthy()
      expect(short, `stages.${n}.short must exist in en`).toBeTruthy()
      // en terminology: "Stage".
      expect(name).toContain("Stage")
      expect(short).toContain("Stage")
      expect(name).toContain(EN_NAMES[n])
    }
  })

  it("there is no seventh stage (the scheme is exactly 1..6)", () => {
    expect(resource("pt-BR", "stages.7.name")).toBeUndefined()
    expect(resource("en", "stages.7.name")).toBeUndefined()
  })

  it("keeps stage key parity between pt-BR and en", () => {
    for (const n of STAGES) {
      expect(resource("pt-BR", `stages.${n}.name`)).toBeTruthy()
      expect(resource("en", `stages.${n}.name`)).toBeTruthy()
      expect(resource("pt-BR", `stages.${n}.short`)).toBeTruthy()
      expect(resource("en", `stages.${n}.short`)).toBeTruthy()
    }
  })
})
