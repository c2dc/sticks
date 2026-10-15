import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { Caso, CasoEstados, CasesResponse } from "@/lib/casos"

/**
 * Component-level navigation tests for the six-stage ExecutionWizard
 * (pipeline-seis-estagios, Req. 4.1, 4.2, 4.3, 9.7).
 *
 * The wizard is entered with no campaign chosen: it resolves a probe slug via
 * `fetchCases` to list options in Stages 1-2, and adopts the campaign id when
 * CampanhaView reports a selection (unlocking Stages 3..6). These tests drive
 * the real wizard DOM:
 *
 *  1. Próximo/Voltar + reselection (Req. 4.2, 4.3): advance 1→2, go Back to
 *     Stage 1, change the selected STIX base, advance again and confirm the new
 *     base is the one propagated forward to CampanhaView.
 *  2. EnvironmentStageView isolation blocks advance (Req. 9.7): at Stage 5 the
 *     advance gate follows the backend state; a non-isolated environment keeps
 *     Stage 5 unfinished, so "Avançar" stays disabled.
 *
 * The stage bodies are stubbed. The BaseStixView stub exposes the current
 * `versao` and reselection buttons; the CampanhaView stub echoes the `versao`
 * it received and offers a button to report the chosen campaign slug.
 */

vi.mock("@/lib/wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/wizard")>()
  return { ...actual, getCaso: vi.fn(), resetCaso: vi.fn() }
})

vi.mock("@/lib/casos", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/casos")>()
  return { ...actual, fetchCases: vi.fn() }
})

vi.mock("@/components/BaseStixView", async () => {
  const { useEffect: ue } = await import("react")
  return {
    BaseStixView: ({
      versao,
      onVersaoSelecionada,
      onConclusaoChange,
    }: {
      casoId: string
      versao?: string
      onVersaoSelecionada?: (v: string) => void
      onConclusaoChange?: (ok: boolean) => void
    }) => {
      ue(() => { onConclusaoChange?.(true) }, [])
      return (
        <div data-testid="base-stix-view">
          <span data-testid="base-versao">{versao ?? "sem-versao"}</span>
          <button type="button" onClick={() => onVersaoSelecionada?.("18.0")}>
            escolher-18
          </button>
          <button type="button" onClick={() => onVersaoSelecionada?.("17.0")}>
            escolher-17
          </button>
        </div>
      )
    },
  }
})

// CampanhaView stub: echoes the propagated `versao` and lets the test report a
// chosen campaign slug (sets campaignId in the wizard).
vi.mock("@/components/CampanhaView", () => ({
  CampanhaView: ({
    versao,
    onConcluido,
  }: {
    casoId: string
    versao?: string
    onConcluido?: (slug: string, resultado: unknown) => void
  }) => (
    <div data-testid="campanha-view">
      <span data-testid="campanha-versao">{versao ?? "sem-versao"}</span>
      <button
        type="button"
        data-testid="selecionar-campanha"
        onClick={() => onConcluido?.("shadowray", { concluido: true })}
      >
        selecionar
      </button>
    </div>
  ),
}))

vi.mock("@/components/DagStageView", () => ({
  DagStageView: () => <div data-testid="dag-stage-view" />,
}))
vi.mock("@/components/Stage2View", () => ({
  Stage2View: () => <div data-testid="stage2-view" />,
}))
vi.mock("@/components/ParametrosAmbienteView", () => ({
  ParametrosAmbienteView: () => <div data-testid="parametros-ambiente-view" />,
}))
vi.mock("@/components/EnvironmentStageView", () => ({
  EnvironmentStageView: () => <div data-testid="environment-stage-view" />,
}))
vi.mock("@/components/Stage3View", () => ({
  Stage3View: () => <div data-testid="stage3-view" />,
}))
vi.mock("@/components/RelatorioView", () => ({
  RelatorioView: () => <div data-testid="relatorio-view" />,
}))

import { getCaso } from "@/lib/wizard"
import { fetchCases } from "@/lib/casos"

const getCasoMock = vi.mocked(getCaso)
const fetchCasesMock = vi.mocked(fetchCases)

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

describe("ExecutionWizard — Próximo/Voltar + reseleção (Req. 4.2, 4.3)", () => {
  beforeEach(() => {
    getCasoMock.mockReset()
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
  })

  it("Voltar retorna ao Estágio anterior e re-renderiza o seu corpo", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    await screen.findByTestId("base-stix-view")
    // Choose a base so a version is propagated forward.
    await userEvent.click(screen.getByRole("button", { name: "escolher-18" }))
    expect(screen.getByText("Passo 1 de 6")).toBeInTheDocument()

    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    await userEvent.click(avancar)
    expect(await screen.findByTestId("campanha-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 2 de 6")).toBeInTheDocument()

    const voltar = screen.getByRole("button", { name: /Voltar/ })
    await userEvent.click(voltar)
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 1 de 6")).toBeInTheDocument()
    expect(screen.queryByTestId("campanha-view")).not.toBeInTheDocument()
  })

  it("Voltar + alterar seleção + Próximo adota a nova seleção propagada adiante", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    await screen.findByTestId("base-stix-view")
    await userEvent.click(screen.getByRole("button", { name: "escolher-18" }))
    await waitFor(() =>
      expect(screen.getByTestId("base-versao")).toHaveTextContent("18.0"),
    )
    await userEvent.click(avancar())
    expect(await screen.findByTestId("campanha-versao")).toHaveTextContent("18.0")

    await userEvent.click(screen.getByRole("button", { name: /Voltar/ }))
    await screen.findByTestId("base-stix-view")
    await userEvent.click(screen.getByRole("button", { name: "escolher-17" }))
    await waitFor(() =>
      expect(screen.getByTestId("base-versao")).toHaveTextContent("17.0"),
    )

    await userEvent.click(avancar())
    expect(await screen.findByTestId("campanha-versao")).toHaveTextContent("17.0")
  })
})

describe("ExecutionWizard — isolamento do Estágio 5 bloqueia o avanço (Req. 9.7)", () => {
  beforeEach(() => {
    getCasoMock.mockReset()
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
  })

  async function caminharAteEstagio5() {
    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    await screen.findByTestId("base-stix-view")
    await userEvent.click(avancar()) // 1 → 2
    await screen.findByTestId("campanha-view")
    await userEvent.click(screen.getByTestId("selecionar-campanha")) // set campaignId
    await userEvent.click(avancar()) // 2 → 3
    await screen.findByTestId("dag-stage-view")
    await userEvent.click(avancar()) // 3 → 4
    await screen.findByTestId("stage2-view")
    await userEvent.click(avancar()) // 4 → 5
    await screen.findByTestId("environment-stage-view")
  }

  it("com o ambiente NÃO isolado (Estágio 5 não concluído), 'Avançar' fica desabilitado no Estágio 5", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1, 2, 3, 4]))

    const { container } = renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    await caminharAteEstagio5()
    expect(screen.getByText("Passo 5 de 6")).toBeInTheDocument()

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })
    const botao5 = avancar()
    expect(botao5).toBeDisabled()
    expect(botao5).toHaveAttribute("aria-disabled", "true")

    await userEvent.click(botao5)
    expect(screen.getByText("Passo 5 de 6")).toBeInTheDocument()
    expect(screen.queryByTestId("stage3-view")).not.toBeInTheDocument()
    expect(screen.queryByTestId("relatorio-view")).not.toBeInTheDocument()

    const badge5 = container.querySelector('[data-stage="5"]') as HTMLElement
    expect(badge5.getAttribute("data-status")).not.toBe("concluido")
  })

  it("com o ambiente isolado (Estágio 5 concluído), 'Avançar' habilita e leva ao Estágio 6", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1, 2, 3, 4, 5]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    await caminharAteEstagio5()

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })
    const botao5 = avancar()
    expect(botao5).not.toBeDisabled()
    await userEvent.click(botao5)
    expect(await screen.findByTestId("stage3-view")).toBeInTheDocument()
    expect(screen.getByTestId("relatorio-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 6 de 6")).toBeInTheDocument()
  })
})
