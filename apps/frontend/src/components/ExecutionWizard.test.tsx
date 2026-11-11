import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { CasesResponse } from "@/lib/campaigns"

/**
 * UI tests for the two-layer ExecutionWizard (ai-first-campaign-flow). The
 * top-level flow is NOT the old linear `1..6` ruler; it is two conceptual
 * layers:
 *
 * - **Layer 1 — Setup** ({@link SetupScreen}): a SINGLE stylish screen where the
 *   STIX package version AND the campaign are picked, one below the other. The
 *   campaign panel is revealed only once a base is ready. There is no "Step N of
 *   6" ruler and no stepwise "Continue" between base and campaign — both live on
 *   the same screen.
 * - **Layer 2 — Run** ({@link GuidedRunView}): the guided, orchestrator-led AI
 *   loop as the PRIMARY view, reached once a campaign is selected. A "change
 *   campaign" control returns to Setup.
 *
 * We mock `fetchCases` (network) and stub the Setup bodies + the Run view. The
 * CampaignView stub exposes a button to report the chosen slug, which is what
 * transitions the flow to the Run.
 */

vi.mock("@/lib/campaigns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/campaigns")>()
  return { ...actual, fetchCases: vi.fn() }
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

// CampaignView stub: offers a button that reports the chosen slug, mirroring a
// real selection that transitions the flow to the Run.
vi.mock("@/components/CampaignView", () => ({
  CampaignView: ({
    campaignId,
    onCompleted,
  }: {
    campaignId: string
    version?: string
    onCompleted?: (slug: string, result: unknown) => void
  }) => (
    <div data-testid="campaign-view">
      <span data-testid="campaign-view-id">{campaignId}</span>
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

// GuidedRunView stub (Layer 2): echoes the campaignId it is handed.
vi.mock("@/components/GuidedRunView", () => ({
  GuidedRunView: ({ campaignId }: { campaignId: string }) => (
    <div data-testid="guided-run-view">{campaignId}</div>
  ),
}))

import { fetchCases } from "@/lib/campaigns"

const fetchCasesMock = vi.mocked(fetchCases)

function makeCases(): CasesResponse {
  return {
    campaigns: [{ id: "shadowray", name: "ShadowRay" }],
    progress: { completed_campaigns: 0, total_campaigns: 8, percentage: 0 },
  }
}

// Controls whether the BaseStixView stub auto-reports the base as ready.
let baseStixAutoComplete = true

/**
 * Select a campaign on the unified Setup screen and then advance to the Run.
 * Selection alone no longer jumps to the Run: it reveals the campaign
 * overview and the explicit Next button, which is what transitions.
 */
async function reachRun() {
  await screen.findByTestId("campaign-view")
  await userEvent.click(screen.getByTestId("select-campaign"))
  await userEvent.click(await screen.findByTestId("advance-to-run"))
  await screen.findByTestId("guided-run-view")
}

describe("ExecutionWizard (unified Setup + guided Run)", () => {
  beforeEach(() => {
    baseStixAutoComplete = true
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
  })

  it("shows the unified Setup screen with NO six-stage ruler / 'Step N of 6'", async () => {
    const { container } = renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")

    // No stepwise "Passo N de 6" nor a six-stage trail.
    expect(screen.queryByText(/Passo \d+ de 6/)).toBeNull()
    expect(container.querySelectorAll("[data-stage]")).toHaveLength(0)
    expect(screen.queryByText(/Estágio [3-6]/)).toBeNull()

    // The hero + both selection panels are present on one screen.
    expect(screen.getByText(/Vamos preparar sua emulação/)).toBeInTheDocument()
    expect(container.querySelectorAll("[data-section='setup-panel']")).toHaveLength(2)
  })

  it("renders BaseStixView and CampaignView together once the base is ready", async () => {
    renderWithI18n(<ExecutionWizard />)

    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    // Base auto-completes → the campaign panel is revealed on the same screen.
    expect(await screen.findByTestId("campaign-view")).toBeInTheDocument()
    expect(screen.queryByTestId("guided-run-view")).not.toBeInTheDocument()
  })

  it("keeps the campaign panel locked until the base is ready", async () => {
    baseStixAutoComplete = false
    renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")

    // The campaign body is not rendered while the base is not ready; a locked
    // hint is shown instead.
    expect(screen.queryByTestId("campaign-view")).toBeNull()
    expect(
      screen.getByText(/Escolha uma versão do pacote STIX acima/),
    ).toBeInTheDocument()
  })

  it("selecting a campaign transitions to the guided Run as the primary view", async () => {
    renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")

    await reachRun()

    const run = screen.getByTestId("guided-run-view")
    expect(run).toHaveTextContent("shadowray")
    // The Run is the primary view: the Setup bodies are gone, and it is not an
    // overlay dialog.
    expect(screen.queryByTestId("base-stix-view")).toBeNull()
    expect(screen.queryByTestId("campaign-view")).toBeNull()
    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("a 'back to setup' control returns to the unified Setup from the Run", async () => {
    renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")
    await reachRun()

    await userEvent.click(screen.getByTestId("back-to-setup"))

    // Back on the unified Setup screen (both panels visible again).
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(await screen.findByTestId("campaign-view")).toBeInTheDocument()
    expect(screen.queryByTestId("guided-run-view")).toBeNull()
  })

  it("the probe slug from fetchCases drives Setup listings before a campaign is chosen", async () => {
    renderWithI18n(<ExecutionWizard />)
    await waitFor(() => expect(fetchCasesMock).toHaveBeenCalled())

    // The CampaignView is addressed by the probe id until a campaign is picked.
    expect(await screen.findByTestId("campaign-view-id")).toHaveTextContent(
      "shadowray",
    )
  })

  it("Back on the Setup screen returns to the Landing via onExitToStart", async () => {
    const onExitToStart = vi.fn()
    renderWithI18n(<ExecutionWizard onExitToStart={onExitToStart} />)
    await screen.findByTestId("base-stix-view")

    await userEvent.click(screen.getByRole("button", { name: /Voltar/ }))
    expect(onExitToStart).toHaveBeenCalledTimes(1)
  })
})
