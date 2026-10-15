import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { RelatorioView } from "@/components/RelatorioView"
import type { MetricasSuficiencia, RelatorioExecucao } from "@/lib/estagios"

// Mock the report wrapper so no network is used.
vi.mock("@/lib/estagios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/estagios")>()
  return {
    ...actual,
    getRelatorio: vi.fn(),
  }
})

import { getRelatorio } from "@/lib/estagios"

const getMock = vi.mocked(getRelatorio)

function makeMetricas(
  overrides: Partial<MetricasSuficiencia> = {},
): MetricasSuficiencia {
  return {
    total_tecnicas_dag: 4,
    tecnicas_sucesso: 3,
    taxa_sucesso: 0.75,
    cobertura_procedural: 0.5,
    tempo_total_s: 42.4,
    por_tecnica: [
      {
        technique_id: "T1190",
        node_id: "n1",
        status: "sucesso",
        etapa_falha: null,
        tempo_execucao_s: 12.3,
        telemetria: ["processo iniciado", "porta 8265 acessada"],
      },
      {
        technique_id: "T1059",
        node_id: "n2",
        status: "falha",
        etapa_falha: "coleta",
        tempo_execucao_s: 3,
        telemetria: [],
      },
    ],
    nos_falhos: ["n2"],
    veredito_sucesso: false,
    ...overrides,
  }
}

function makeRelatorio(
  overrides: Partial<RelatorioExecucao> = {},
): RelatorioExecucao {
  return {
    caso_id: "shadowray",
    metricas: makeMetricas(),
    resultados: [],
    ambiente_desprovisionado: true,
    gerado_em: "2024-01-02T03:04:05Z",
    ...overrides,
  }
}

describe("RelatorioView (Estágio 6 — relatório de execução, Req. 10.6, 10.7, 15.7)", () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  it("renders success rate, procedural coverage, total time and failed-node count (Req. 15.1, 15.3, 15.5)", async () => {
    getMock.mockResolvedValue(makeRelatorio())

    renderWithI18n(<RelatorioView casoId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalledWith("shadowray"))

    // Success rate 0.75 -> 75%, coverage 0.5 -> 50%, time 42.4s.
    expect(await screen.findByText("75%")).toBeInTheDocument()
    expect(screen.getByText("50%")).toBeInTheDocument()
    expect(screen.getByText("42.4s")).toBeInTheDocument()
    // 3 / 4 technique ratio detail is shown alongside the success rate.
    expect(screen.getByText("3 / 4")).toBeInTheDocument()
  })

  it("renders the failure verdict and the deprovisioning flag (Req. 15.6, 12)", async () => {
    getMock.mockResolvedValue(makeRelatorio())

    const { container } = renderWithI18n(<RelatorioView casoId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    expect(
      container.querySelector('[data-veredito="falha"]'),
    ).not.toBeNull()
    expect(
      container.querySelector('[data-desprovisionado="true"]'),
    ).toHaveTextContent(/desprovisionado/i)
  })

  it("renders the failed DAG nodes (Req. 10.6, 15.4)", async () => {
    getMock.mockResolvedValue(makeRelatorio())

    const { container } = renderWithI18n(<RelatorioView casoId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    const section = container.querySelector('[data-section="nos-falhos"]')
    expect(section).toHaveTextContent("n2")
  })

  it("renders per-technique telemetry with status, failing step and signals (Req. 15.2, 15.4, 15.7)", async () => {
    getMock.mockResolvedValue(makeRelatorio())

    const { container } = renderWithI18n(<RelatorioView casoId="shadowray" />)

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
      makeRelatorio({
        metricas: makeMetricas({
          total_tecnicas_dag: 0,
          tecnicas_sucesso: 0,
          taxa_sucesso: 0,
          cobertura_procedural: 0,
          tempo_total_s: null,
          por_tecnica: [],
          nos_falhos: [],
          veredito_sucesso: false,
        }),
        ambiente_desprovisionado: false,
      }),
    )

    const { container } = renderWithI18n(<RelatorioView casoId="shadowray" />)

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

    renderWithI18n(<RelatorioView casoId="shadowray" />)

    await screen.findByText("Não foi possível carregar os dados deste estágio.")
  })
})
