import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { CasesResponse } from "@/lib/campaigns"

/**
 * Navigation tests for the two-layer ExecutionWizard (ai-first-campaign-flow).
 * The unified Setup screen replaces the obsolete two-step Continue/Back between
 * base and campaign. These drive the real shell DOM and assert:
 *
 *  1. Base and campaign selection live on the SAME screen; Back on Setup exits
 *     to the Landing.
 *  2. Propagation: the STIX base chosen in the base panel flows into the
 *     campaign panel, and changing the base updates what is propagated.
 *  3. Selecting a campaign reaches the guided Run (Layer 2); "change campaign"
 *     returns to the unified Setup — the Run is reached from Setup, not as an
 *     overlay off a ruler.
 *
 * The Setup bodies and the Run view are stubbed. The BaseStixView stub exposes
 * the current `version` and reselection buttons; the CampaignView stub echoes
 * the `version` it received and offers a button to report the chosen slug.
 */

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
      ue(() => { onCompletionChange?.(true) }, [version]) // eslint-disable-line
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

describe("ExecutionWizard — unified Setup selection + propagation", () => {
  beforeEach(() => {
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
  })

  it("renders base and campaign selection on the same screen", async () => {
    renderWithI18n(<ExecutionWizard />)
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(await screen.findByTestId("campaign-view")).toBeInTheDocument()
  })

  it("Back on the Setup screen exits to the Landing", async () => {
    const onExitToStart = vi.fn()
    renderWithI18n(<ExecutionWizard onExitToStart={onExitToStart} />)
    await screen.findByTestId("base-stix-view")

    await userEvent.click(screen.getByRole("button", { name: /Voltar/ }))
    expect(onExitToStart).toHaveBeenCalledTimes(1)
  })

  it("propagates the chosen base into the campaign panel, and a reselection changes it", async () => {
    renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")
    await screen.findByTestId("campaign-view")

    await userEvent.click(screen.getByRole("button", { name: "choose-18" }))
    await waitFor(() =>
      expect(screen.getByTestId("campaign-version")).toHaveTextContent("18.0"),
    )

    await userEvent.click(screen.getByRole("button", { name: "choose-17" }))
    await waitFor(() =>
      expect(screen.getByTestId("campaign-version")).toHaveTextContent("17.0"),
    )
  })
})

describe("ExecutionWizard — reaching the Run and returning to Setup", () => {
  beforeEach(() => {
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
  })

  it("selecting a campaign reaches the Run; 'change campaign' returns to Setup", async () => {
    renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")
    await screen.findByTestId("campaign-view")

    await userEvent.click(screen.getByTestId("select-campaign"))
    // Selection now reveals the campaign overview and an explicit Next; the
    // Run is reached only after clicking Next.
    await userEvent.click(await screen.findByTestId("advance-to-run"))

    const run = await screen.findByTestId("guided-run-view")
    expect(run).toHaveTextContent("shadowray")
    // Not a modal/overlay — Layer 2 is the primary view.
    expect(screen.queryByRole("dialog")).toBeNull()

    await userEvent.click(screen.getByTestId("back-to-setup"))
    expect(await screen.findByTestId("campaign-view")).toBeInTheDocument()
    expect(screen.queryByTestId("guided-run-view")).toBeNull()
  })
})
