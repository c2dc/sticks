import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { ReportView } from "@/components/ReportView"
import type { SufficiencyMetrics, ExecutionReport } from "@/lib/stages"

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

function makeReport(
  overrides: Partial<ExecutionReport> = {},
): ExecutionReport {
  return {
    campaign_id: "shadowray",
    metrics: makeMetrics(),
    results: [],
    environment_deprovisioned: true,
    generated_at: "2024-01-02T03:04:05Z",
    ...overrides,
  }
}

describe("ReportView (Estágio 6 — relatório de execução, Req. 10.6, 10.7, 15.7)", () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  it("renders success rate, procedural coverage, total time and failed-node count (Req. 15.1, 15.3, 15.5)", async () => {
    getMock.mockResolvedValue(makeReport())

    renderWithI18n(<ReportView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalledWith("shadowray"))

    // Success rate 0.75 -> 75%, coverage 0.5 -> 50%, time 42.4s.
    expect(await screen.findByText("75%")).toBeInTheDocument()
    expect(screen.getByText("50%")).toBeInTheDocument()
    expect(screen.getByText("42.4s")).toBeInTheDocument()
    // 3 / 4 technique ratio detail is shown alongside the success rate.
    expect(screen.getByText("3 / 4")).toBeInTheDocument()
  })

  it("renders the failure verdict and the deprovisioning flag (Req. 15.6, 12)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(<ReportView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    expect(
      container.querySelector('[data-veredito="falha"]'),
    ).not.toBeNull()
    expect(
      container.querySelector('[data-desprovisionado="true"]'),
    ).toHaveTextContent(/desprovisionado/i)
  })

  it("renders the failed DAG nodes (Req. 10.6, 15.4)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(<ReportView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    const section = container.querySelector('[data-section="nos-falhos"]')
    expect(section).toHaveTextContent("n2")
  })

  it("renders per-technique telemetry with status, failing step and signals (Req. 15.2, 15.4, 15.7)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(<ReportView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())

    // Both techniques appear with their status pills.
    const t1190 = container.querySelector('[data-tecnica="T1190"]')
    const t1059 = container.querySelector('[data-tecnica="T1059"]')
    expect(t1190).not.toBeNull()
    expect(t1059).not.toBeNull()
    expect(t1190?.querySelector('[data-status="sucesso"]')).not.toBeNull()
    expect(t1059?.querySelector('[data-status="falha"]')).not.toBeNull()

    // Telemetry signals and the failing step surface in the card.
    expect(screen.getByText("processo iniciado")).toBeInTheDocument()
    expect(screen.getByText("porta 8265 acessada")).toBeInTheDocument()
    expect(t1059).toHaveTextContent(/coleta/)
  })

  it("renders zeroed metrics for a case with no execution without error", async () => {
    getMock.mockResolvedValue(
      makeReport({
        metrics: makeMetrics({
          total_dag_techniques: 0,
          successful_techniques: 0,
          success_rate: 0,
          procedural_coverage: 0,
          total_time_s: null,
          per_technique: [],
          failed_nodes: [],
          success_verdict: false,
        }),
        environment_deprovisioned: false,
      }),
    )

    const { container } = renderWithI18n(<ReportView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    // Two 0% metrics (success rate + coverage) and an em dash for null time.
    expect(screen.getAllByText("0%").length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText("—")).toBeInTheDocument()
    expect(
      container.querySelector('[data-section="telemetria"]'),
    ).toHaveTextContent(/Nenhuma telemetria/)
  })

  it("surfaces the pt-BR load error when the report fails to load", async () => {
    getMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<ReportView campaignId="shadowray" />)

    await screen.findByText("Não foi possível carregar os dados deste estágio.")
  })
})
