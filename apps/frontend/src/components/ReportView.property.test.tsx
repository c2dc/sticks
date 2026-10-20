import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, waitFor } from "@testing-library/react"
import fc from "fast-check"

import { renderWithI18n } from "@/test/render"
import { ReportView } from "@/components/ReportView"
import type { ExecutionReport, SufficiencyMetrics } from "@/lib/stages"

// Mock the report wrapper so no network is used — same seam the example-based
// `ReportView.test.tsx` relies on. Each run drives the gated fetch deterministically.
vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return {
    ...actual,
    getReport: vi.fn(),
  }
})

import { getReport } from "@/lib/stages"

const getMock = vi.mocked(getReport)

// A small, invariant-consistent report fixture: one per-technique entry per
// technique so `ReportView`'s final-report normalization has valid input.
function makeReport(): ExecutionReport {
  const metrics: SufficiencyMetrics = {
    total_dag_techniques: 1,
    successful_techniques: 1,
    success_rate: 1,
    procedural_coverage: 1,
    total_time_s: 10,
    per_technique: [
      {
        technique_id: "T1190",
        node_id: "n1",
        status: "sucesso",
        failed_step: null,
        execution_time_s: 10,
        telemetry: [],
      },
    ],
    failed_nodes: [],
    success_verdict: true,
  }
  return {
    campaign_id: "shadowray",
    metrics,
    results: [],
    environment_deprovisioned: true,
    generated_at: "2024-01-02T03:04:05Z",
  }
}

// Non-terminal execution states the run can be in before termination. We pair
// each with the derived `executionFinished` so the generator exercises both the
// terminal and non-terminal halves of the biconditional.
const EXECUTION_STATES = [
  "pendente",
  "em_execucao",
  "aguardando",
  "sucesso",
  "falha",
  "persisted",
] as const

describe("ReportView gating (propriedade)", () => {
  afterEach(() => {
    cleanup()
    getMock.mockReset()
  })

  // Feature: stage6-screen-redesign, Property 2: Gating das seções pós-término
  // Validates: Requirements 3.1, 3.2, 7.1, 7.2, 8.2
  //
  // Para todo estado de execução, as seções de métricas
  // (`data-section="metricas"`) e de relatório de desprovisionamento
  // (`data-section="desprovisionamento"`) são renderizadas se e somente se a
  // execução terminou (`executionFinished`). Em qualquer estado não terminal
  // ambas permanecem ocultas e `GET /report` nunca é disparado.
  it("renders metrics + deprovisioning iff executionFinished", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.boolean(),
        fc.constantFrom(...EXECUTION_STATES),
        async (executionFinished, executionState) => {
          getMock.mockReset()
          getMock.mockResolvedValue(makeReport())

          const { container, unmount } = renderWithI18n(
            <ReportView
              campaignId="shadowray"
              executionFinished={executionFinished}
            />,
          )

          try {
            if (executionFinished) {
              // Once finished, the view fetches and renders both sections.
              await waitFor(() => {
                expect(
                  container.querySelector('[data-section="metricas"]'),
                ).not.toBeNull()
              })
              expect(
                container.querySelector('[data-section="desprovisionamento"]'),
              ).not.toBeNull()
            } else {
              // Not finished: no fetch, and neither section is ever rendered.
              expect(getMock).not.toHaveBeenCalled()
              expect(
                container.querySelector('[data-section="metricas"]'),
              ).toBeNull()
              expect(
                container.querySelector('[data-section="desprovisionamento"]'),
              ).toBeNull()
            }

            // Biconditional, expressed directly on the two gated sections:
            // both present exactly when `executionFinished` is true.
            const hasMetricas =
              container.querySelector('[data-section="metricas"]') !== null
            const hasDesprov =
              container.querySelector(
                '[data-section="desprovisionamento"]',
              ) !== null
            expect(hasMetricas).toBe(executionFinished)
            expect(hasDesprov).toBe(executionFinished)
            // `executionState` only varies the non-terminal context; the gate
            // is governed solely by `executionFinished`.
            void executionState
          } finally {
            unmount()
            getMock.mockReset()
          }
        },
      ),
      { numRuns: 100 },
    )
  })
})
