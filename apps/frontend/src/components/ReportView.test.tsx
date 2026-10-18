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
      container.querySelector('[data-verdict="falha"]'),
    ).not.toBeNull()
    expect(
      container.querySelector('[data-deprovisioned="true"]'),
    ).toHaveTextContent(/desprovisionado/i)
  })

  it("renders the failed DAG nodes (Req. 10.6, 15.4)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(<ReportView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    const section = container.querySelector('[data-section="failed-nodes"]')
    expect(section).toHaveTextContent("n2")
  })

  it("renders per-technique telemetry with status, failing step and signals (Req. 15.2, 15.4, 15.7)", async () => {
    getMock.mockResolvedValue(makeReport())

    const { container } = renderWithI18n(<ReportView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())

    // Both techniques appear with their status pills.
    const t1190 = container.querySelector('[data-technique="T1190"]')
    const t1059 = container.querySelector('[data-technique="T1059"]')
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
      container.querySelector('[data-section="telemetry"]'),
    ).toHaveTextContent(/Nenhuma telemetria/)
  })

  it("normalizes a non-terminal technique to falha and keeps the invariants (Req. 3.4)", async () => {
    // A divergent/stale payload that reaches the view with non-terminal entries:
    // one `pendente` and one `em_execucao`, plus a genuine `falha` and a
    // `sucesso`. A FINAL report must never render a non-terminal status; the view
    // normalizes every non-success to `falha` and recomputes the counts so the
    // Req. 3.4 invariants hold.
    getMock.mockResolvedValue(
      makeReport({
        metrics: makeMetrics({
          // Deliberately inconsistent inputs: the normalizer must override these.
          total_dag_techniques: 99,
          successful_techniques: 99,
          success_rate: 1,
          success_verdict: true,
          per_technique: [
            {
              technique_id: "T1190",
              node_id: "n1",
              status: "sucesso",
              failed_step: null,
              execution_time_s: 12.3,
              telemetry: [],
            },
            {
              technique_id: "T1203",
              node_id: "n2",
              status: "pendente",
              failed_step: null,
              execution_time_s: null,
              telemetry: [],
            },
            {
              technique_id: "T1083",
              node_id: "n3",
              status: "em_execucao",
              failed_step: null,
              execution_time_s: null,
              telemetry: [],
            },
            {
              technique_id: "T1059",
              node_id: "n4",
              status: "falha",
              failed_step: "coleta",
              execution_time_s: 3,
              telemetry: [],
            },
          ],
          // Deliberately wrong failed_nodes: the normalizer must rebuild it.
          failed_nodes: [],
        }),
      }),
    )

    const { container } = renderWithI18n(<ReportView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())

    // (d) No technique is ever rendered in a non-terminal state: the status pills
    // only carry terminal values, and no `pendente`/`em_execucao` pill exists.
    const statusPills = Array.from(
      container.querySelectorAll("[data-status]"),
    ).map((el) => el.getAttribute("data-status"))
    expect(statusPills.length).toBe(4)
    expect(statusPills).not.toContain("pendente")
    expect(statusPills).not.toContain("em_execucao")
    for (const status of statusPills) {
      expect(["sucesso", "falha"]).toContain(status)
    }

    // The non-terminal techniques are normalized to `falha`.
    expect(
      container
        .querySelector('[data-technique="T1203"]')
        ?.querySelector('[data-status="falha"]'),
    ).not.toBeNull()
    expect(
      container
        .querySelector('[data-technique="T1083"]')
        ?.querySelector('[data-status="falha"]'),
    ).not.toBeNull()

    // (b) total == per_technique length (4) and the per-state counts sum to it:
    // 1 success + 3 failures (one genuine + two normalized). The ratio detail
    // reflects the recomputed success/total, not the bogus 99/99 input.
    const successPills = statusPills.filter((s) => s === "sucesso")
    const failurePills = statusPills.filter((s) => s === "falha")
    expect(successPills.length).toBe(1)
    expect(failurePills.length).toBe(3)
    expect(successPills.length + failurePills.length).toBe(statusPills.length)
    expect(screen.getByText("1 / 4")).toBeInTheDocument()

    // (c) failed-node count equals the number of `falha` techniques (3), rebuilt
    // from the normalized entries (n2, n3, n4) — the bogus empty input is ignored.
    const failedNodesSection = container.querySelector(
      '[data-section="failed-nodes"]',
    )
    expect(failedNodesSection?.querySelectorAll("li").length).toBe(3)
    expect(failedNodesSection).toHaveTextContent("n2")
    expect(failedNodesSection).toHaveTextContent("n3")
    expect(failedNodesSection).toHaveTextContent("n4")

    // (a) success_rate / verdict are derived deterministically from the counts:
    // 1/4 == 25% and the verdict is `falha` (not every technique succeeded),
    // overriding the inconsistent success_rate=1 / verdict=true inputs.
    expect(screen.getByText("25%")).toBeInTheDocument()
    expect(container.querySelector('[data-verdict="falha"]')).not.toBeNull()
    expect(container.querySelector('[data-verdict="sucesso"]')).toBeNull()
  })

  it("surfaces the pt-BR load error when the report fails to load", async () => {
    getMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<ReportView campaignId="shadowray" />)

    await screen.findByText("Não foi possível carregar os dados deste estágio.")
  })
})
