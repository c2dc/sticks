import { describe, it, expect } from "vitest"

import ptPipeline from "@/i18n/locales/pt-BR/pipeline.json"
import enPipeline from "@/i18n/locales/en/pipeline.json"

// i18n parity for the keys introduced by wizard-progresso-estagios. The two new
// progress strings must exist in BOTH locales of the `pipeline` namespace, so
// the UI reads them from i18n (never hardcoded) and the experience is identical
// in pt-BR and en.
// Validates: Requirements 3.2, 3.3, 4.2, 5.2
describe("pipeline i18n parity (wizard progress stages)", () => {
  it("state.preConcluido exists as a non-empty string in both locales", () => {
    expect(typeof ptPipeline.state.preConcluido).toBe("string")
    expect(ptPipeline.state.preConcluido.length).toBeGreaterThan(0)

    expect(typeof enPipeline.state.preConcluido).toBe("string")
    expect(enPipeline.state.preConcluido.length).toBeGreaterThan(0)
  })

  it("wizard.preCompletedNote exists as a non-empty string in both locales", () => {
    expect(typeof ptPipeline.wizard.preCompletedNote).toBe("string")
    expect(ptPipeline.wizard.preCompletedNote.length).toBeGreaterThan(0)

    expect(typeof enPipeline.wizard.preCompletedNote).toBe("string")
    expect(enPipeline.wizard.preCompletedNote.length).toBeGreaterThan(0)
  })

  it("pt-BR and en values are genuinely translated (not copied)", () => {
    expect(ptPipeline.state.preConcluido).not.toBe(enPipeline.state.preConcluido)
    expect(ptPipeline.wizard.preCompletedNote).not.toBe(
      enPipeline.wizard.preCompletedNote,
    )
  })
})
