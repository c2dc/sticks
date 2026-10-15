import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { Caso, CasoEstados, CasesResponse } from "@/lib/casos"

/**
 * UI tests for the six-stage ExecutionWizard (pipeline-seis-estagios,
 * Req. 1.1, 1.2, 4.1, 4.2, 4.3). The wizard is reached straight from the
 * Landing with NO campaign chosen: it resolves a probe slug via `fetchCases`
 * (used only to list base/campaign options in Stages 1-2) and adopts the chosen
 * campaign id when CampanhaView reports a selection, which unlocks Stages 3..6.
 *
 * We mock `fetchCases` + `getCaso`/`resetCaso` (network) and the Stage-4
 * conclusion wrapper, and stub the stage body components. The CampanhaView stub
 * fires `onConcluido("shadowray", …)` on mount so reaching Stage 2 selects the
 * single curated campaign and unlocks the later stages.
 */

vi.mock("@/lib/wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/wizard")>()
  return { ...actual, getCaso: vi.fn(), resetCaso: vi.fn() }
})

vi.mock("@/lib/casos", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/casos")>()
  return { ...actual, fetchCases: vi.fn() }
})

vi.mock("@/lib/estagios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/estagios")>()
  return { ...actual, concluirOperacionalizacao: vi.fn() }
})

vi.mock("@/components/BaseStixView", async () => {
  const { useEffect: ue } = await import("react")
  return {
    BaseStixView: ({
      casoId,
      onConclusaoChange,
    }: {
      casoId: string
      onConclusaoChange?: (ok: boolean) => void
    }) => {
      ue(() => { onConclusaoChange?.(baseStixAutoComplete) }, []) // eslint-disable-line
      return <div data-testid="base-stix-view">{casoId}</div>
    },
  }
})
// CampanhaView stub: on mount, reports the chosen curated slug so the wizard
// adopts it as campaignId (unlocking Stages 3..6), mirroring a real selection.
vi.mock("@/components/CampanhaView", () => ({
  CampanhaView: ({
    onConcluido,
  }: {
    casoId: string
    versao?: string
    onConcluido?: (slug: string, resultado: unknown) => void
  }) => {
    return (
      <div data-testid="campanha-view">
        <button
          type="button"
          data-testid="selecionar-campanha"
          onClick={() => onConcluido?.("shadowray", { concluido: true })}
        >
          selecionar
        </button>
      </div>
    )
  },
}))
vi.mock("@/components/DagStageView", () => ({
  DagStageView: ({ casoId }: { casoId: string }) => (
    <div data-testid="dag-stage-view">{casoId}</div>
  ),
}))
vi.mock("@/components/Stage2View", () => ({
  Stage2View: ({ casoId }: { casoId: string }) => (
    <div data-testid="stage2-view">{casoId}</div>
  ),
}))
vi.mock("@/components/ParametrosAmbienteView", () => ({
  ParametrosAmbienteView: ({ casoId }: { casoId: string }) => (
    <div data-testid="parametros-ambiente-view">{casoId}</div>
  ),
}))
vi.mock("@/components/EnvironmentStageView", () => ({
  EnvironmentStageView: ({ casoId }: { casoId: string }) => (
    <div data-testid="environment-stage-view">{casoId}</div>
  ),
}))
vi.mock("@/components/Stage3View", () => ({
  Stage3View: ({ casoId }: { casoId: string }) => (
    <div data-testid="stage3-view">{casoId}</div>
  ),
}))
vi.mock("@/components/RelatorioView", () => ({
  RelatorioView: ({ casoId }: { casoId: string }) => (
    <div data-testid="relatorio-view">{casoId}</div>
  ),
}))

import { getCaso, resetCaso } from "@/lib/wizard"
import { fetchCases } from "@/lib/casos"
import { concluirOperacionalizacao } from "@/lib/estagios"

const getCasoMock = vi.mocked(getCaso)
const resetCasoMock = vi.mocked(resetCaso)
const fetchCasesMock = vi.mocked(fetchCases)
const concluirOperacionalizacaoMock = vi.mocked(concluirOperacionalizacao)

function estados(concluidos: number[]): CasoEstados {
  const todos = [1, 2, 3, 4, 5, 6] as const
  return {
    concluido_total: concluidos.length === 6,
    estagios: todos.map((estagio) => ({
      estagio,
      estado: concluidos.includes(estagio) ? "concluido" : "nao_iniciado",
      progresso: concluidos.includes(estagio) ? 100 : 0,
      bloqueado: estagio > 1 && !concluidos.includes(estagio - 1),
    })),
  }
}

function makeCaso(concluidos: number[]): Caso {
  return { id: "shadowray", nome: "ShadowRay", estados: estados(concluidos) }
}

function makeCases(): CasesResponse {
  return {
    casos: [{ id: "shadowray", nome: "ShadowRay" }],
    progresso: { casos_concluidos: 0, total_casos: 8, percentual: 0 },
  }
}

/** Walk Stage 1 → 2 and click the stub's select button to set campaignId. */
async function selecionarCampanhaNoEstagio2() {
  const avancar = screen.getByRole("button", {
    name: /Avançar para o próximo estágio/,
  })
  await userEvent.click(avancar)
  await screen.findByTestId("campanha-view")
  await userEvent.click(screen.getByTestId("selecionar-campanha"))
}

// Controls whether the BaseStixView stub auto-reports Stage 1 as concluded.
// Individual tests can set this to false before rendering to verify the
// blocked state.
let baseStixAutoComplete = true

describe("ExecutionWizard (6 Estágios — Req. 1.1, 1.2, 4.1–4.3)", () => {
  beforeEach(() => {
    baseStixAutoComplete = true
    getCasoMock.mockReset()
    resetCasoMock.mockReset()
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
    concluirOperacionalizacaoMock.mockReset()
  })

  it("shows the step indicator 'Passo 1 de 6' and a six-badge track (1..6)", async () => {
    getCasoMock.mockResolvedValue(makeCaso([]))

    const { container } = renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    expect(screen.getByText("Passo 1 de 6")).toBeInTheDocument()

    const badges = Array.from(
      container.querySelectorAll("[data-stage]"),
    ) as HTMLElement[]
    expect(badges).toHaveLength(6)
    expect(badges.map((b) => b.getAttribute("data-stage"))).toEqual([
      "1",
      "2",
      "3",
      "4",
      "5",
      "6",
    ])
  })

  it("renders BaseStixView (Seleção da base STIX) as the body of Estágio 1", async () => {
    getCasoMock.mockResolvedValue(makeCaso([]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(screen.queryByTestId("campanha-view")).not.toBeInTheDocument()
    expect(screen.queryByTestId("dag-stage-view")).not.toBeInTheDocument()
  })

  it("blocks 'Avançar' at Estágio 1 while the stage is not concluded", async () => {
    baseStixAutoComplete = false
    getCasoMock.mockResolvedValue(makeCaso([]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    expect(avancar).toBeDisabled()
    expect(avancar).toHaveAttribute("aria-disabled", "true")

    await userEvent.click(avancar)
    expect(screen.getByText("Passo 1 de 6")).toBeInTheDocument()
    expect(screen.getByTestId("base-stix-view")).toBeInTheDocument()
  })

  it("enables 'Avançar' once Estágio 1 is concluded and advances to Estágio 2 (Seleção da campanha)", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    await waitFor(() => expect(avancar).not.toBeDisabled())
    await userEvent.click(avancar)

    expect(await screen.findByText("Passo 2 de 6")).toBeInTheDocument()
    expect(screen.getByTestId("campanha-view")).toBeInTheDocument()
    expect(screen.queryByTestId("base-stix-view")).not.toBeInTheDocument()
  })

  it("maps each stage number to its body component across the six stages", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1, 2, 3, 4, 5, 6]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    // Estágio 1 — BaseStixView.
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()

    // Estágio 2 — CampanhaView; selecting the campaign unlocks Stages 3..6.
    await selecionarCampanhaNoEstagio2()
    expect(screen.getByTestId("campanha-view")).toBeInTheDocument()

    // Estágio 3 — DagStageView.
    await userEvent.click(avancar())
    expect(await screen.findByTestId("dag-stage-view")).toBeInTheDocument()

    // Estágio 4 — Stage2View + ParametrosAmbienteView.
    await userEvent.click(avancar())
    expect(await screen.findByTestId("stage2-view")).toBeInTheDocument()
    expect(screen.getByTestId("parametros-ambiente-view")).toBeInTheDocument()

    // Estágio 5 — EnvironmentStageView.
    await userEvent.click(avancar())
    expect(await screen.findByTestId("environment-stage-view")).toBeInTheDocument()

    // Estágio 6 — Stage3View + RelatorioView; no "Avançar" on the last.
    await userEvent.click(avancar())
    expect(await screen.findByTestId("stage3-view")).toBeInTheDocument()
    expect(screen.getByTestId("relatorio-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 6 de 6")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: /Avançar para o próximo estágio/ }),
    ).not.toBeInTheDocument()
  })

  it("enables 'Próximo' on Estágio 4 when it is available but NOT yet concluded, and clicking concludes it", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1, 2, 3]))
    concluirOperacionalizacaoMock.mockImplementation(async () => {
      getCasoMock.mockResolvedValue(makeCaso([1, 2, 3, 4]))
      return makeCaso([1, 2, 3, 4]).estados as never
    })

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    await selecionarCampanhaNoEstagio2() // 1 → 2 (+ select)
    await userEvent.click(avancar()) // 2 → 3
    await screen.findByTestId("dag-stage-view")
    await userEvent.click(avancar()) // 3 → 4
    await screen.findByTestId("stage2-view")
    expect(screen.getByText("Passo 4 de 6")).toBeInTheDocument()

    const botao4 = avancar()
    expect(botao4).not.toBeDisabled()

    await userEvent.click(botao4)
    await waitFor(() =>
      expect(concluirOperacionalizacaoMock).toHaveBeenCalledWith("shadowray"),
    )
    expect(await screen.findByTestId("environment-stage-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 5 de 6")).toBeInTheDocument()
  })

  it("renders the next button's visible label as 'Próximo'", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1]))
    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const botao = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    expect(botao).toHaveTextContent("Próximo")
  })

  it("resets the trail to Estágio 1 and recomputes from the re-read when the campaign is restarted (R6.3)", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1, 2, 3, 4, 5, 6]))
    resetCasoMock.mockImplementation(async () => {
      getCasoMock.mockResolvedValue(makeCaso([]))
      return undefined as never
    })

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    // Walk 1 → 2 (+ select) → 3 so the wizard is on a later stage before restart.
    await selecionarCampanhaNoEstagio2()
    await userEvent.click(avancar()) // 2 → 3
    await screen.findByTestId("dag-stage-view")
    expect(screen.getByText("Passo 3 de 6")).toBeInTheDocument()

    const reiniciar = screen.getByRole("button", { name: /Reiniciar/ })
    await userEvent.click(reiniciar)

    await waitFor(() => expect(resetCasoMock).toHaveBeenCalledWith("shadowray"))

    expect(await screen.findByText("Passo 1 de 6")).toBeInTheDocument()
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(screen.queryByTestId("dag-stage-view")).not.toBeInTheDocument()
  })
})
