import { useState } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { Stage3View } from "@/components/Stage3View"
import { ReportView } from "@/components/ReportView"
import type {
  AbilityResult,
  DeprovisioningResult,
  ExecutionReport,
  OperationResultView,
  Stage3Result,
  SufficiencyMetrics,
} from "@/lib/stages"

/**
 * Integration test for the Stage 6 composition (Task 7.2, Req. 8.1, 8.2, 8.3).
 *
 * The real {@link Stage3View} (Input + Processing) and {@link ReportView}
 * (Métricas + Relatório de desprovisionamento) are rendered TOGETHER under a
 * harness that mirrors the `ExecutionWizard` Stage-6 wiring exactly: a parent
 * holds `executionFinished` state, passes `onExecutionFinished={setExecutionFinished}`
 * to `Stage3View` and `executionFinished={executionFinished}` to `ReportView`,
 * both inside a `flex flex-col gap-4` with `ReportView` rendered AFTER
 * `Stage3View` (so the page order is Input → Processing → Métricas →
 * Desprovisionamento). We stub the four stage wrappers (`getStage3`,
 * `startExecution`, `connectOperation`, `getReport`) so no network/WebSocket is
 * touched; the composition's own gating drives what is visible.
 *
 * Asserts, end to end:
 * - while the execution is in progress (non-terminal Operation) only Input and
 *   Processing are visible; Métricas and Desprovisionamento are absent and
 *   `GET /report` is never fetched (Req. 8.2);
 * - once the Operation is terminal the full order Input → Processing → Métricas
 *   → Desprovisionamento is present, asserted by DOM order (Req. 8.1);
 * - no `role="dialog"` is ever introduced (Req. 8.3).
 */

vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return {
    ...actual,
    getStage3: vi.fn(),
    startExecution: vi.fn(),
    connectOperation: vi.fn(),
    getReport: vi.fn(),
  }
})

import {
  connectOperation,
  getReport,
  getStage3,
  startExecution,
} from "@/lib/stages"

const getStage3Mock = vi.mocked(getStage3)
const startExecutionMock = vi.mocked(startExecution)
const connectOperationMock = vi.mocked(connectOperation)
const getReportMock = vi.mocked(getReport)

const CAMPAIGN = "shadowray"

/**
 * Harness that reproduces the Stage-6 block of {@link ExecutionWizard}: the
 * shared `executionFinished` state flows Stage3View → wizard → ReportView,
 * with ReportView rendered after Stage3View inside a vertical flex column.
 */
function Stage6Composition({ campaignId }: { campaignId: string }) {
  const [executionFinished, setExecutionFinished] = useState(false)
  return (
    <div className="flex flex-col gap-4">
      <Stage3View
        campaignId={campaignId}
        onExecutionFinished={setExecutionFinished}
      />
      <ReportView campaignId={campaignId} executionFinished={executionFinished} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

function makeInput(): Stage3Result["input"] {
  return {
    campaign_id: CAMPAIGN,
    name: "ShadowRay",
    adversary: {
      id: "adv-1",
      name: "ShadowRay Adversary",
      description: "",
      atomic_ordering: ["a1", "a2"],
    },
    abilities: [
      {
        ability_id: "a1",
        name: "Exploit T1190",
        technique_id: "T1190",
        tactic: "initial-access",
        description: "",
        executors: [],
      },
      {
        ability_id: "a2",
        name: "Execute T1059",
        technique_id: "T1059",
        tactic: "execution",
        description: "",
        executors: [],
      },
    ],
    can_emulate: true,
  }
}

function makeOperation(
  results: AbilityResult[],
  overrides: Partial<OperationResultView> = {},
): OperationResultView {
  const total_success = results.filter((r) => r.status === "sucesso").length
  const total_failure = results.filter((r) => r.status === "falha").length
  return {
    operation_id: "op-1",
    state: "em_execucao",
    total_success,
    total_failure,
    results,
    ...overrides,
  }
}

function makeStage3Result(overrides: Partial<Stage3Result> = {}): Stage3Result {
  return {
    input: makeInput(),
    output: [],
    ...overrides,
  }
}

function makeMetrics(): SufficiencyMetrics {
  return {
    total_dag_techniques: 2,
    successful_techniques: 1,
    success_rate: 0.5,
    procedural_coverage: 0.5,
    total_time_s: 12.5,
    per_technique: [
      {
        technique_id: "T1190",
        node_id: "n1",
        status: "sucesso",
        failed_step: null,
        execution_time_s: 8,
        telemetry: [],
      },
      {
        technique_id: "T1059",
        node_id: "n2",
        status: "falha",
        failed_step: "coleta",
        execution_time_s: 4,
        telemetry: [],
      },
    ],
    failed_nodes: ["n2"],
    success_verdict: false,
  }
}

function makeDeprovisioning(): DeprovisioningResult {
  return {
    deprovisioned: true,
    remaining_resources: [],
    origin: "curadoria_humana",
    message: "ok",
  }
}

function makeReport(): ExecutionReport {
  return {
    campaign_id: CAMPAIGN,
    metrics: makeMetrics(),
    results: [],
    environment_deprovisioned: true,
    deprovisioning: makeDeprovisioning(),
    generated_at: "2024-01-02T03:04:05Z",
  }
}

/** Assert `before` precedes `after` in document order. */
function expectDomOrder(before: Element, after: Element) {
  expect(
    before.compareDocumentPosition(after) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy()
}

beforeEach(() => {
  getStage3Mock.mockReset()
  startExecutionMock.mockReset()
  connectOperationMock.mockReset()
  getReportMock.mockReset()
  // Live channel is a no-op unless a terminal persisted result drives it.
  connectOperationMock.mockReturnValue({ send: vi.fn(), close: vi.fn() })
  getReportMock.mockResolvedValue(makeReport())
})

describe("Estágio 6 — composição Stage3View + ReportView (Req. 8.1, 8.2, 8.3)", () => {
  it("em andamento (Operação não terminal): apenas Input e Processing; sem Métricas/Desprovisionamento e sem GET /report (Req. 8.2)", async () => {
    // A persisted but STILL-RUNNING Operation (non-terminal state) → the live
    // list renders from the snapshot and `onExecutionFinished` never fires true.
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation(
            [{ ability_id: "a1", status: "em_execucao", command_output: "" }],
            { state: "em_execucao" },
          ),
        ],
      }),
    )

    const { container } = renderWithI18n(
      <Stage6Composition campaignId={CAMPAIGN} />,
    )

    // Processing is live → Input and Processing are both on screen.
    await screen.findByTestId("stage6-live-abilities")
    expect(container.querySelector('[data-section="entrada"]')).not.toBeNull()
    expect(
      container.querySelector('[data-section="processamento"]'),
    ).not.toBeNull()

    // Give any effect/microtask a chance to (wrongly) flip the gate before we
    // assert the gated sections stay hidden.
    await waitFor(() => expect(connectOperationMock).toHaveBeenCalled())

    // Gated sections stay hidden while the run is in progress (Req. 8.2).
    expect(container.querySelector('[data-section="metricas"]')).toBeNull()
    expect(
      container.querySelector('[data-section="desprovisionamento"]'),
    ).toBeNull()
    // The report is never fetched while the execution is in progress.
    expect(getReportMock).not.toHaveBeenCalled()

    // No modal/pop-up is ever introduced (Req. 8.3).
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(screen.queryByRole("alertdialog")).toBeNull()
  })

  it("após o término (Operação terminal): ordem completa Input → Processing → Métricas → Desprovisionamento (Req. 8.1)", async () => {
    // A persisted TERMINAL Operation → Stage3View fires onExecutionFinished(true),
    // the harness flips the gate, and ReportView fetches + renders the report.
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation(
            [
              { ability_id: "a1", status: "sucesso", command_output: "ok" },
              { ability_id: "a2", status: "falha", command_output: "x" },
            ],
            { state: "concluida" },
          ),
        ],
      }),
    )

    const { container } = renderWithI18n(
      <Stage6Composition campaignId={CAMPAIGN} />,
    )

    // The run is terminal → the gated fetch fires and the four sections render.
    await waitFor(() => expect(getReportMock).toHaveBeenCalledWith(CAMPAIGN))

    const entrada = await waitFor(() => {
      const el = container.querySelector('[data-section="entrada"]')
      expect(el).not.toBeNull()
      return el as Element
    })
    const processamento = container.querySelector(
      '[data-section="processamento"]',
    )
    const metricas = await waitFor(() => {
      const el = container.querySelector('[data-section="metricas"]')
      expect(el).not.toBeNull()
      return el as Element
    })
    const desprovisionamento = container.querySelector(
      '[data-section="desprovisionamento"]',
    )

    expect(processamento).not.toBeNull()
    expect(desprovisionamento).not.toBeNull()

    // Full Stage-6 page order (Req. 8.1): Input → Processing → Métricas →
    // Desprovisionamento, asserted on the live DOM.
    expectDomOrder(entrada, processamento as Element)
    expectDomOrder(processamento as Element, metricas)
    expectDomOrder(metricas, desprovisionamento as Element)

    // The terminal composition still introduces no modal/pop-up (Req. 8.3).
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(screen.queryByRole("alertdialog")).toBeNull()
  })

  it("nunca introduz role=\"dialog\" em nenhum estado da composição (Req. 8.3)", async () => {
    // Drive the auto-start path (empty snapshot) and hold the POST pending so
    // the view sits in its automatic-start state — still no modal.
    getStage3Mock.mockResolvedValue(makeStage3Result())
    startExecutionMock.mockReturnValue(new Promise(() => {}))

    renderWithI18n(<Stage6Composition campaignId={CAMPAIGN} />)

    await screen.findByTestId("stage6-view")
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(screen.queryByRole("alertdialog")).toBeNull()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.querySelector('[role="alertdialog"]')).toBeNull()
  })
})
