import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { Campaign, CampaignStates, CasesResponse } from "@/lib/campaigns"

/**
 * UI tests for the six-stage ExecutionWizard (pipeline-seis-estagios,
 * Req. 1.1, 1.2, 4.1, 4.2, 4.3). The wizard is reached straight from the
 * Landing with NO campaign chosen: it resolves a probe slug via `fetchCases`
 * (used only to list base/campaign options in Stages 1-2) and adopts the chosen
 * campaign id when CampaignView reports a selection, which unlocks Stages 3..6.
 *
 * We mock `fetchCases` + `getCampaign`/`resetCampaign` (network) and the Stage-4
 * conclusion wrapper, and stub the stage body components. The CampaignView stub
 * fires `onCompleted("shadowray", …)` on mount so reaching Stage 2 selects the
 * single curated campaign and unlocks the later stages.
 */

vi.mock("@/lib/wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/wizard")>()
  return { ...actual, getCampaign: vi.fn(), resetCampaign: vi.fn() }
})

vi.mock("@/lib/campaigns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/campaigns")>()
  return { ...actual, fetchCases: vi.fn() }
})

vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return { ...actual, completeOperationalization: vi.fn() }
})

vi.mock("@/components/BaseStixView", async () => {
  const { useEffect: ue } = await import("react")
  return {
    BaseStixView: ({
      campaignId,
      onCompletionChange,
    }: {
      campaignId: string
      onCompletionChange?: (ok: boolean) => void
    }) => {
      ue(() => { onCompletionChange?.(baseStixAutoComplete) }, []) // eslint-disable-line
      return <div data-testid="base-stix-view">{campaignId}</div>
    },
  }
})
// CampaignView stub: on mount, reports the chosen curated slug so the wizard
// adopts it as campaignId (unlocking Stages 3..6), mirroring a real selection.
vi.mock("@/components/CampaignView", () => ({
  CampaignView: ({
    onCompleted,
  }: {
    campaignId: string
    version?: string
    onCompleted?: (slug: string, result: unknown) => void
  }) => {
    return (
      <div data-testid="campaign-view">
        <button
          type="button"
          data-testid="select-campaign"
          onClick={() => onCompleted?.("shadowray", { completed: true })}
        >
          select
        </button>
      </div>
    )
  },
}))
vi.mock("@/components/DagStageView", () => ({
  DagStageView: ({ campaignId }: { campaignId: string }) => (
    <div data-testid="dag-stage-view">{campaignId}</div>
  ),
}))
vi.mock("@/components/Stage2View", () => ({
  Stage2View: ({ campaignId }: { campaignId: string }) => (
    <div data-testid="stage2-view">{campaignId}</div>
  ),
}))
vi.mock("@/components/EnvironmentParametersView", () => ({
  EnvironmentParametersView: ({ campaignId }: { campaignId: string }) => (
    <div data-testid="environment-parameters-view">{campaignId}</div>
  ),
}))
vi.mock("@/components/EnvironmentStageView", () => ({
  EnvironmentStageView: ({ campaignId }: { campaignId: string }) => (
    <div data-testid="environment-stage-view">{campaignId}</div>
  ),
}))
vi.mock("@/components/Stage3View", () => ({
  Stage3View: ({ campaignId }: { campaignId: string }) => (
    <div data-testid="stage3-view">{campaignId}</div>
  ),
}))
vi.mock("@/components/ReportView", () => ({
  ReportView: ({ campaignId }: { campaignId: string }) => (
    <div data-testid="report-view">{campaignId}</div>
  ),
}))

import { getCampaign, resetCampaign } from "@/lib/wizard"
import { fetchCases } from "@/lib/campaigns"
import { completeOperationalization } from "@/lib/stages"

const getCampaignMock = vi.mocked(getCampaign)
const resetCampaignMock = vi.mocked(resetCampaign)
const fetchCasesMock = vi.mocked(fetchCases)
const completeOperationalizationMock = vi.mocked(completeOperationalization)

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

/** Walk Stage 1 → 2 and click the stub's select button to set campaignId. */
async function selectCampaignAtStage2() {
  const advance = screen.getByRole("button", {
    name: /Avançar para o próximo estágio/,
  })
  await userEvent.click(advance)
  await screen.findByTestId("campaign-view")
  await userEvent.click(screen.getByTestId("select-campaign"))
}

// Controls whether the BaseStixView stub auto-reports Stage 1 as concluded.
// Individual tests can set this to false before rendering to verify the
// blocked state.
let baseStixAutoComplete = true

describe("ExecutionWizard (6 stages — Req. 1.1, 1.2, 4.1–4.3)", () => {
  beforeEach(() => {
    baseStixAutoComplete = true
    getCampaignMock.mockReset()
    resetCampaignMock.mockReset()
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
    completeOperationalizationMock.mockReset()
  })

  it("shows the step indicator 'Passo 1 de 6' and a six-badge track (1..6)", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([]))

    const { container } = renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

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

  it("renders BaseStixView (STIX base selection) as the body of Stage 1", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(screen.queryByTestId("campaign-view")).not.toBeInTheDocument()
    expect(screen.queryByTestId("dag-stage-view")).not.toBeInTheDocument()
  })

  it("blocks 'Avançar' at Stage 1 while the stage is not concluded", async () => {
    baseStixAutoComplete = false
    getCampaignMock.mockResolvedValue(makeCampaign([]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    const advance = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    expect(advance).toBeDisabled()
    expect(advance).toHaveAttribute("aria-disabled", "true")

    await userEvent.click(advance)
    expect(screen.getByText("Passo 1 de 6")).toBeInTheDocument()
    expect(screen.getByTestId("base-stix-view")).toBeInTheDocument()
  })

  it("enables 'Avançar' once Stage 1 is concluded and advances to Stage 2 (Campaign selection)", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    const advance = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    await waitFor(() => expect(advance).not.toBeDisabled())
    await userEvent.click(advance)

    expect(await screen.findByText("Passo 2 de 6")).toBeInTheDocument()
    expect(screen.getByTestId("campaign-view")).toBeInTheDocument()
    expect(screen.queryByTestId("base-stix-view")).not.toBeInTheDocument()
  })

  it("maps each stage number to its body component across the six stages", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1, 2, 3, 4, 5, 6]))

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    const advance = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    // Stage 1 — BaseStixView.
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()

    // Stage 2 — CampaignView; selecting the campaign unlocks Stages 3..6.
    await selectCampaignAtStage2()
    expect(screen.getByTestId("campaign-view")).toBeInTheDocument()

    // Stage 3 — DagStageView.
    await userEvent.click(advance())
    expect(await screen.findByTestId("dag-stage-view")).toBeInTheDocument()

    // Stage 4 — Stage2View + EnvironmentParametersView.
    await userEvent.click(advance())
    expect(await screen.findByTestId("stage2-view")).toBeInTheDocument()
    expect(screen.getByTestId("environment-parameters-view")).toBeInTheDocument()

    // Stage 5 — EnvironmentStageView.
    await userEvent.click(advance())
    expect(await screen.findByTestId("environment-stage-view")).toBeInTheDocument()

    // Stage 6 — Stage3View + ReportView; no "Avançar" on the last.
    await userEvent.click(advance())
    expect(await screen.findByTestId("stage3-view")).toBeInTheDocument()
    expect(screen.getByTestId("report-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 6 de 6")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: /Avançar para o próximo estágio/ }),
    ).not.toBeInTheDocument()
  })

  it("enables 'Próximo' on Stage 4 when it is available but NOT yet concluded, and clicking concludes it", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1, 2, 3]))
    completeOperationalizationMock.mockImplementation(async () => {
      getCampaignMock.mockResolvedValue(makeCampaign([1, 2, 3, 4]))
      return makeCampaign([1, 2, 3, 4]).states as never
    })

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    const advance = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    await selectCampaignAtStage2() // 1 → 2 (+ select)
    await userEvent.click(advance()) // 2 → 3
    await screen.findByTestId("dag-stage-view")
    await userEvent.click(advance()) // 3 → 4
    await screen.findByTestId("stage2-view")
    expect(screen.getByText("Passo 4 de 6")).toBeInTheDocument()

    const button4 = advance()
    expect(button4).not.toBeDisabled()

    await userEvent.click(button4)
    await waitFor(() =>
      expect(completeOperationalizationMock).toHaveBeenCalledWith("shadowray"),
    )
    expect(await screen.findByTestId("environment-stage-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 5 de 6")).toBeInTheDocument()
  })

  it("renders the next button's visible label as 'Próximo'", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1]))
    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    const button = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    expect(button).toHaveTextContent("Próximo")
  })

  it("resets the trail to Stage 1 and recomputes from the re-read when the campaign is restarted (R6.3)", async () => {
    getCampaignMock.mockResolvedValue(makeCampaign([1, 2, 3, 4, 5, 6]))
    resetCampaignMock.mockImplementation(async () => {
      getCampaignMock.mockResolvedValue(makeCampaign([]))
      return undefined as never
    })

    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(getCampaignMock).toHaveBeenCalledWith("shadowray"))

    const advance = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    // Walk 1 → 2 (+ select) → 3 so the wizard is on a later stage before restart.
    await selectCampaignAtStage2()
    await userEvent.click(advance()) // 2 → 3
    await screen.findByTestId("dag-stage-view")
    expect(screen.getByText("Passo 3 de 6")).toBeInTheDocument()

    const restart = screen.getByRole("button", { name: /Reiniciar/ })
    await userEvent.click(restart)

    await waitFor(() => expect(resetCampaignMock).toHaveBeenCalledWith("shadowray"))

    expect(await screen.findByText("Passo 1 de 6")).toBeInTheDocument()
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(screen.queryByTestId("dag-stage-view")).not.toBeInTheDocument()
  })
})
