import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"

/**
 * Navigation tests for the two-layer ExecutionWizard (ai-first-campaign-flow).
 * The unified Input screen replaces the obsolete two-step Continue/Back between
 * base and campaign. These drive the real shell DOM and assert:
 *
 *  1. Base and campaign selection live on the SAME screen; Back on Input exits
 *     to the Landing.
 *  2. Propagation: the STIX base chosen in the base panel flows into the
 *     campaign panel, and changing the base updates what is propagated.
 *  3. Selecting a campaign reaches the guided Run (Layer 2); "change campaign"
 *     returns to the unified Input — the Run is reached from Input, not as an
 *     overlay off a ruler.
 *
 * The Input bodies and the Run view are stubbed. The BaseStixView stub exposes
 * the current `version` and reselection buttons; the CampaignView stub echoes
 * the `version` it received and offers a button to report the chosen slug.
 */

vi.mock("@/components/BaseStixView", async () => {
  const { useEffect: ue } = await import("react")
  return {
    BaseStixView: ({
      version,
      onVersionSelected,
      onCompletionChange,
    }: {
      version?: string
      onVersionSelected?: (v: string) => void
      onCompletionChange?: (ok: boolean) => void
    }) => {
      // Start with version 18.0 chosen (the Researcher picked it in the base list).
      ue(() => { onVersionSelected?.("18.0") }, []) // eslint-disable-line
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
    version: string
    onCompleted?: (campaignKey: string) => void
  }) => (
    <div data-testid="campaign-view">
      <span data-testid="campaign-version">{version}</span>
      <button
        type="button"
        data-testid="select-campaign"
        onClick={() => onCompleted?.("shadowray")}
      >
        select
      </button>
    </div>
  ),
}))

vi.mock("@/components/GuidedRunView", () => ({
  GuidedRunView: ({ campaignId, version }: { campaignId: string; version: string }) => (
    <div data-testid="guided-run-view">{`${campaignId}@${version}`}</div>
  ),
}))

describe("ExecutionWizard — unified Input selection + propagation", () => {
  it("renders base and campaign selection on the same screen", async () => {
    renderWithI18n(<ExecutionWizard />)
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(await screen.findByTestId("campaign-view")).toBeInTheDocument()
  })

  it("Back on the Input screen exits to the Landing", async () => {
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

describe("ExecutionWizard — reaching the Run and returning to Input", () => {
  it("selecting a campaign reaches the Run; 'change campaign' returns to Input", async () => {
    renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")
    await screen.findByTestId("campaign-view")

    await userEvent.click(screen.getByTestId("select-campaign"))
    // Selection now reveals the campaign overview and an explicit Next; the
    // Run is reached only after clicking Next.
    await userEvent.click(await screen.findByTestId("advance-to-run"))

    const run = await screen.findByTestId("guided-run-view")
    expect(run).toHaveTextContent("shadowray@18.0")
    // Not a modal/overlay — Layer 2 is the primary view.
    expect(screen.queryByRole("dialog")).toBeNull()

    await userEvent.click(screen.getByTestId("back-to-input"))
    expect(await screen.findByTestId("campaign-view")).toBeInTheDocument()
    expect(screen.queryByTestId("guided-run-view")).toBeNull()
  })
})
