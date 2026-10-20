import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { ReportView } from "@/components/ReportView"
import type {
  DeprovisioningResult,
  SufficiencyMetrics,
  ExecutionReport,
} from "@/lib/stages"

// Mock the report wrapper so no network is used.
vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return {
    ...actual,
    getReport: vi.fn(),
  }
})

import { getReport } from "@/lib/stages"

const getMock = vi.mocked(getReport)

function makeMetrics(
  overrides: Partial<SufficiencyMetrics> = {},
): SufficiencyMetrics {
  // Invariant-consistent fixture matching the backend `ReportBuilder` contract
  // (Req. 3.4): one per-technique entry per technique, so `per_technique.length`
  // equals `total_dag_techniques` and the per-state counts sum to the total.
  return {
    total_dag_techniques: 4,
    successful_techniques: 3,
    success_rate: 0.75,
    procedural_coverage: 0.5,
    total_time_s: 42.4,
    per_technique: [
      {
        technique_id: "T1190",
        node_id: "n1",
        status: "sucesso",
        failed_step: null,
        execution_time_s: 12.3,
        telemetry: ["processo iniciado", "porta 8265 acessada"],
      },
      {
        technique_id: "T1203",
        node_id: "n3",
        status: "sucesso",
        failed_step: null,
        execution_time_s: 5.1,
        telemetry: [],
      },
      {
        technique_id: "T1083",
        node_id: "n4",
        status: "sucesso",
        failed_step: null,
        execution_time_s: 2.2,
        telemetry: [],
      },
      {
        technique_id: "T1059",
        node_id: "n2",
        status: "falha",
        failed_step: "coleta",
        execution_time_s: 3,
        telemetry: [],
      },
    ],
    failed_nodes: ["n2"],
    success_verdict: false,
    ...overrides,
  }
}

function makeDeprovisioning(
  overrides: Partial<DeprovisioningResult> = {},
): DeprovisioningResult {
  return {
    deprovisioned: true,
    remaining_resources: [],
    origin: "curadoria_humana",
    message: "ok",
    ...overrides,
  }
}

function makeReport(
  overrides: Partial<ExecutionReport> = {},
): ExecutionReport {
  return {
    campaign_id: "shadowray",
    metrics: makeMetrics(),
    results: [],
    environment_deprovisioned: true,
    deprovisioning: makeDeprovisioning(),
    generated_at: "2024-01-02T03:04:05Z",
    ...overrides,
  }
}

describe("ReportView (Estágio 6 — métricas + desprovisionamento com gating)", () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  // Gating (Req. 3.1, 3.2, 7.1, 8.2): while the execution has not finished the
  // view neither fetches `GET /report` nor renders the metrics section.
  it("não busca o relatório nem renderiza métricas enquanto executionFinished=false (Req. 3.1, 3.2, 7.1, 8.2)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(
      <ReportView campaignId="shadowray" executionFinished={false} />,
    )

    // A neutral empty state: no metrics, no deprovisioning.
    expect(
      container.querySelector('[data-section="metricas"]'),
    ).toBeNull()
    expect(
      container.querySelector('[data-section="desprovisionamento"]'),
    ).toBeNull()
    // The gated fetch must never fire while the run is in progress.
    expect(getMock).not.toHaveBeenCalled()
  })

  it("renderiza os quatro cards de métricas incluindo a contagem de nós falhos quando executionFinished=true (Req. 3.3, 3.4, 4.2, 5.2, 5.3)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(
      <ReportView campaignId="shadowray" executionFinished={true} />,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalledWith("shadowray"))

    // The metrics section is present with exactly four cards.
    const metricas = container.querySelector('[data-section="metricas"]')
    expect(metricas).not.toBeNull()

    // Success rate 0.75 -> 75%, coverage 0.5 -> 50%, time 42.4s.
    expect(await screen.findByText("75%")).toBeInTheDocument()
    expect(screen.getByText("50%")).toBeInTheDocument()
    expect(screen.getByText("42.4s")).toBeInTheDocument()
    // 3 / 4 technique ratio detail is shown alongside the success rate.
    expect(screen.getByText("3 / 4")).toBeInTheDocument()

    // Failed-node COUNT card (not the nodes themselves): the label is present
    // and the value equals `failed_nodes.length` (1), without listing node ids.
    expect(screen.getByText("Nós que falharam")).toBeInTheDocument()
    expect(screen.getByText("1")).toBeInTheDocument()
    expect(metricas).not.toHaveTextContent("n2")
  })

  it("não renderiza os blocos verdict, failed-nodes e telemetry (Req. 5.1, 6.1, 7.4)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(
      <ReportView campaignId="shadowray" executionFinished={true} />,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalled())

    expect(container.querySelector('[data-section="verdict"]')).toBeNull()
    expect(
      container.querySelector('[data-section="failed-nodes"]'),
    ).toBeNull()
    expect(container.querySelector('[data-section="telemetry"]')).toBeNull()
    // The removed verdict markers are gone as well.
    expect(container.querySelector("[data-verdict]")).toBeNull()
  })

  it("renderiza o bloco de desprovisionamento por último, indicando sucesso (Req. 7.2, 7.3)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(
      <ReportView campaignId="shadowray" executionFinished={true} />,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalled())

    const sections = Array.from(container.querySelectorAll("[data-section]"))
    const metricasIdx = sections.findIndex(
      (el) => el.getAttribute("data-section") === "metricas",
    )
    const teardownIdx = sections.findIndex(
      (el) => el.getAttribute("data-section") === "desprovisionamento",
    )
    // Both present and the deprovisioning block comes after the metrics.
    expect(metricasIdx).toBeGreaterThanOrEqual(0)
    expect(teardownIdx).toBeGreaterThan(metricasIdx)

    const teardown = sections[teardownIdx]
    expect(teardown).toHaveTextContent("Ambiente desprovisionado")
    // On success there are no remaining resources listed.
    expect(teardown).not.toHaveTextContent("Recursos remanescentes")
  })

  it("lista os recursos remanescentes quando o desprovisionamento falha (Req. 7.2, 7.3)", async () => {
    getMock.mockResolvedValue(
      makeReport({
        environment_deprovisioned: false,
        deprovisioning: makeDeprovisioning({
          deprovisioned: false,
          remaining_resources: ["vm-attacker", "net-lab"],
        }),
      }),
    )

    const { container } = renderWithI18n(
      <ReportView campaignId="shadowray" executionFinished={true} />,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalled())

    const teardown = container.querySelector(
      '[data-section="desprovisionamento"]',
    )
    expect(teardown).not.toBeNull()
    expect(teardown).toHaveTextContent("Ambiente não desprovisionado")
    expect(teardown).toHaveTextContent(
      "Recursos remanescentes: vm-attacker, net-lab",
    )
  })

  it("surfaces the pt-BR load error when the report fails to load", async () => {
    getMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(
      <ReportView campaignId="shadowray" executionFinished={true} />,
    )

    await screen.findByText("Não foi possível carregar os dados deste estágio.")
  })
})
