import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { Campaign, CampaignStates, CasesResponse } from "@/lib/campaigns"

/**
 * Component-level navigation tests for the six-stage ExecutionWizard
 * (pipeline-seis-estagios, Req. 4.1, 4.2, 4.3, 9.7).
 *
 * The wizard is entered with no campaign chosen: it resolves a probe slug via
 * `fetchCases` to list options in Stages 1-2, and adopts the campaign id when
 * CampaignView reports a selection (unlocking Stages 3..6). These tests drive
 * the real wizard DOM:
 *
 *  1. Próximo/Voltar + reselection (Req. 4.2, 4.3): advance 1→2, go Back to
 *     Stage 1, change the selected STIX base, advance again and confirm the new
 *     base is the one propagated forward to CampaignView.
 *  2. EnvironmentStageView isolation blocks advance (Req. 9.7): at Stage 5 the
 *     advance gate follows the backend state; a non-isolated environment keeps
 *     Stage 5 unfinished, so "Avançar" stays disabled.
 *
 * The stage bodies are stubbed. The BaseStixView stub exposes the current
 * `version` and reselection buttons; the CampaignView stub echoes the `versao`
 * it received and offers a button to report the chosen campaign slug.
 */

vi.mock("@/lib/wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/wizard")>()
  return { ...actual, getCampaign: vi.fn(), resetCampaign: vi.fn() }
})

vi.mock("@/lib/campaigns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/campaigns")>()
  return { ...actual, fetchCases: vi.fn() }
})

vi.mock("@/components/BaseStixView", async () => {
  const { useEffect: ue } = await import("react")
  return {
    BaseStixView: ({
      version,
      onVersionSelected,
      onCompletionChange,
    }: {
      campaignId: string
      version?: string
      onVersionSelected?: (v: string) => void
      onCompletionChange?: (ok: boolean) => void
    }) => {
      ue(() => { onCompletionChange?.(true) }, [])
      return (
        <div data-testid="base-stix-view">
          <span data-testid="base-version">{version ?? "no-version"}</span>
          <button type="button" onClick={() => onVersionSelected?.("18.0")}>
            choose-18
          </button>
          <button type="button" onClick={() => onVersionSelected?.("17.0")}>
            choose-17
          </button>
        </div>
      )
    },
  }
})

// CampaignView stub: echoes the propagated `version` and lets the test report a
// chosen campaign slug (sets campaignId in the wizard).
vi.mock("@/components/CampaignView", () => ({
  CampaignView: ({
    version,
    onCompleted,
  }: {
    campaignId: string
    version?: string
    onCompleted?: (slug: string, result: unknown) => void
  }) => (
    <div data-testid="campaign-view">
      <span data-testid="campaign-version">{version ?? "no-version"}</span>
      <button
        type="button"
        data-testid="select-campaign"
        onClick={() => onCompleted?.("shadowray", { completed: true })}
      >
        select
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
vi.mock("@/components/EnvironmentParametersView", () => ({
  EnvironmentParametersView: () => <div data-testid="environment-parameters-view" />,
}))
vi.mock("@/components/EnvironmentStageView", () => ({
  EnvironmentStageView: () => <div data-testid="environment-stage-view" />,
}))
vi.mock("@/components/Stage3View", () => ({
  Stage3View: () => <div data-testid="stage3-view" />,
}))
vi.mock("@/components/ReportView", () => ({
  ReportView: () => <div data-testid="report-view" />,
}))

import { getCampaign } from "@/lib/wizard"
import { fetchCases } from "@/lib/campaigns"

const getCampaignMock = vi.mocked(getCampaign)
const fetchCasesMock = vi.mocked(fetchCases)

function states(completed: number[]): CampaignStates {
  const all = [1, 2, 3, 4, 5, 6] as const
  return {
    fully_completed: completed.length === 6,
    stages: all.map((stage) => ({
      stage,
      state: completed.includes(stage) ? "concluido" : "nao_iniciado",
      progress: completed.includes(stage) ? 100 : 0,
      blocked: stage > 1 && !completed.includes(stage - 1),
    })),
  }
}

function makeCampaign(completed: number[]): Campaign {
  return { id: "shadowray", name: "ShadowRay", states: states(completed) }
}

function makeCases(): CasesResponse {
  return {
    campaigns: [{ id: "shadowray", name: "ShadowRay" }],
    progress: { completed_campaigns: 0, total_campaigns: 8, percentage: 0 },
  }
}

describe("ExecutionWizard — Next/Back + reselection (Req. 4.2, 4.3)", () => {
  beforeEach(() => {
    getCampaignMock.mockReset()
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
  })

  it("Back returns to the previous stage and re-renders its body", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    await screen.findByTestId("base-stix-view")
    // Choose a base so a version is propagated forward.
    await userEvent.click(screen.getByRole("button", { name: "choose-18" }))
    expect(screen.getByText("Passo 1 de 6")).toBeInTheDocument()

    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    await userEvent.click(avancar)
    expect(await screen.findByTestId("campaign-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 2 de 6")).toBeInTheDocument()

    const voltar = screen.getByRole("button", { name: /Voltar/ })
    await userEvent.click(voltar)
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 1 de 6")).toBeInTheDocument()
    expect(screen.queryByTestId("campaign-view")).not.toBeInTheDocument()
  })

  it("Back + change selection + Next adopts the new selection propagated forward", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    await screen.findByTestId("base-stix-view")
    await userEvent.click(screen.getByRole("button", { name: "choose-18" }))
    await waitFor(() =>
      expect(screen.getByTestId("base-version")).toHaveTextContent("18.0"),
    )
    await userEvent.click(avancar())
    expect(await screen.findByTestId("campaign-version")).toHaveTextContent("18.0")

    await userEvent.click(screen.getByRole("button", { name: /Voltar/ }))
    await screen.findByTestId("base-stix-view")
    await userEvent.click(screen.getByRole("button", { name: "choose-17" }))
    await waitFor(() =>
      expect(screen.getByTestId("base-version")).toHaveTextContent("17.0"),
    )

    await userEvent.click(avancar())
    expect(await screen.findByTestId("campaign-version")).toHaveTextContent("17.0")
  })
})

describe("ExecutionWizard — Stage 5 isolation blocks advancing (Req. 9.7)", () => {
  beforeEach(() => {
    getCampaignMock.mockReset()
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
  })

  async function walkToStage5() {
    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    await screen.findByTestId("base-stix-view")
    await userEvent.click(avancar()) // 1 → 2
    await screen.findByTestId("campaign-view")
    await userEvent.click(screen.getByTestId("select-campaign")) // set campaignId
    await userEvent.click(avancar()) // 2 → 3
    await screen.findByTestId("dag-stage-view")
    await userEvent.click(avancar()) // 3 → 4
    await screen.findByTestId("stage2-view")
    await userEvent.click(avancar()) // 4 → 5
    await screen.findByTestId("environment-stage-view")
  }

  it("with the environment NOT isolated (Stage 5 not concluded), 'Advance' stays disabled on Stage 5", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1, 2, 3, 4]))

    const { container } = renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    await walkToStage5()
    expect(screen.getByText("Passo 5 de 6")).toBeInTheDocument()

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })
    const button5 = avancar()
    expect(button5).toBeDisabled()
    expect(button5).toHaveAttribute("aria-disabled", "true")

    await userEvent.click(button5)
    expect(screen.getByText("Passo 5 de 6")).toBeInTheDocument()
    expect(screen.queryByTestId("stage3-view")).not.toBeInTheDocument()
    expect(screen.queryByTestId("report-view")).not.toBeInTheDocument()

    const badge5 = container.querySelector('[data-stage="5"]') as HTMLElement
    expect(badge5.getAttribute("data-status")).not.toBe("completed")
  })

  it("with the environment isolated (Stage 5 concluded), 'Advance' enables and leads to Stage 6", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1, 2, 3, 4, 5]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    await walkToStage5()

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })
    const button5 = avancar()
    expect(button5).not.toBeDisabled()
    await userEvent.click(button5)
    expect(await screen.findByTestId("stage3-view")).toBeInTheDocument()
    expect(screen.getByTestId("report-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 6 de 6")).toBeInTheDocument()
  })
})
