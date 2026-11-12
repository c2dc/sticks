import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"

/**
 * UI tests for the two-layer ExecutionWizard (ai-first-campaign-flow). The
 * top-level flow is NOT the old linear `1..6` ruler; it is two conceptual
 * layers:
 *
 * - **Layer 1 — Input** ({@link InputScreen}): a SINGLE stylish screen where the
 *   STIX package version AND the campaign are picked, one below the other. The
 *   campaign panel is revealed only once a base is ready. There is no "Step N of
 *   6" ruler and no stepwise "Continue" between base and campaign — both live on
 *   the same screen.
 * - **Layer 2 — Run** ({@link GuidedRunView}): the guided, orchestrator-led AI
 *   loop as the PRIMARY view, reached once a campaign is selected. A "change
 *   campaign" control returns to Input.
 *
 * The Input bodies and the Run view are stubbed (no network). The BaseStixView
 * stub picks version 18.0 and the CampaignView stub exposes a button to report
 * the chosen campaign key, which is what enables the move to the Run.
 */

vi.mock("@/components/BaseStixView", async () => {
  const { useEffect: ue } = await import("react")
  return {
    BaseStixView: ({
      onVersionSelected,
      onCompletionChange,
    }: {
      version?: string
      onVersionSelected?: (v: string) => void
      onCompletionChange?: (ok: boolean) => void
    }) => {
      ue(() => {
        onVersionSelected?.("18.0")
        onCompletionChange?.(baseStixAutoComplete)
      }, []) // eslint-disable-line
      return <div data-testid="base-stix-view" />
    },
  }
})

// CampaignView stub: offers a button that reports the chosen slug, mirroring a
// real selection that transitions the flow to the Run.
vi.mock("@/components/CampaignView", () => ({
  CampaignView: ({
    version,
    onCompleted,
  }: {
    version: string
    onCompleted?: (campaignKey: string) => void
  }) => (
    <div data-testid="campaign-view">
      <span data-testid="campaign-view-version">{version}</span>
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

// GuidedRunView stub (Layer 2): echoes the campaign key and version it is handed.
vi.mock("@/components/GuidedRunView", () => ({
  GuidedRunView: ({ campaignId, version }: { campaignId: string; version: string }) => (
    <div data-testid="guided-run-view">{`${campaignId}@${version}`}</div>
  ),
}))

// Controls whether the BaseStixView stub auto-reports the base as ready.
let baseStixAutoComplete = true

/**
 * Select a campaign on the unified Input screen and then advance to the Run.
 * Selection alone no longer jumps to the Run: it reveals the campaign
 * overview and the explicit Next button, which is what transitions.
 */
async function reachRun() {
  await screen.findByTestId("campaign-view")
  await userEvent.click(screen.getByTestId("select-campaign"))
  await userEvent.click(await screen.findByTestId("advance-to-run"))
  await screen.findByTestId("guided-run-view")
}

describe("ExecutionWizard (unified Input + guided Run)", () => {
  beforeEach(() => {
    baseStixAutoComplete = true
  })

  it("shows the unified Input screen with NO six-stage ruler / 'Step N of 6'", async () => {
    const { container } = renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")

    // No stepwise "Passo N de 6" nor a six-stage trail.
    expect(screen.queryByText(/Passo \d+ de 6/)).toBeNull()
    expect(container.querySelectorAll("[data-stage]")).toHaveLength(0)
    expect(screen.queryByText(/Estágio [3-6]/)).toBeNull()

    // The hero + both selection panels are present on one screen.
    expect(screen.getByText(/Vamos preparar sua emulação/)).toBeInTheDocument()
    expect(container.querySelectorAll("[data-section='input-panel']")).toHaveLength(2)
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
      screen.getByText(/Escolha uma versão do STIX acima/),
    ).toBeInTheDocument()
  })

  it("treats package and version as ONE choice: a single 'STIX version (package)' step, then the campaign", async () => {
    const { container } = renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("campaign-view")
    const panels = Array.from(
      container.querySelectorAll("[data-section='input-panel']"),
    )
    // Exactly two steps: version (= package) and campaign; no separate package step.
    expect(panels).toHaveLength(2)
    expect(panels[0]).toHaveTextContent("Versão do STIX (pacote)")
    expect(panels[1]).toHaveTextContent("Campanha")
    expect(screen.getAllByTestId("base-stix-view")).toHaveLength(1)
    // No copy presents package and version as distinct picks.
    expect(screen.queryByText(/versão do pacote/i)).toBeNull()
    expect(screen.queryByText(/pacote e versão|pacote, versão/i)).toBeNull()
    // version → campaign → operationalization (Run).
    await reachRun()
    expect(screen.getByTestId("guided-run-view")).toHaveTextContent("shadowray@18.0")
  })

  it("selecting a campaign transitions to the guided Run as the primary view", async () => {
    renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")

    await reachRun()

    const run = screen.getByTestId("guided-run-view")
    expect(run).toHaveTextContent("shadowray@18.0")
    // The Run is the primary view: the Input bodies are gone, and it is not an
    // overlay dialog.
    expect(screen.queryByTestId("base-stix-view")).toBeNull()
    expect(screen.queryByTestId("campaign-view")).toBeNull()
    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("a 'back to input' control returns to the unified Input from the Run", async () => {
    renderWithI18n(<ExecutionWizard />)
    await screen.findByTestId("base-stix-view")
    await reachRun()

    await userEvent.click(screen.getByTestId("back-to-input"))

    // Back on the unified Input screen (both panels visible again).
    expect(await screen.findByTestId("base-stix-view")).toBeInTheDocument()
    expect(await screen.findByTestId("campaign-view")).toBeInTheDocument()
    expect(screen.queryByTestId("guided-run-view")).toBeNull()
  })

  it("the chosen base version drives the campaign listing (no probe slug)", async () => {
    renderWithI18n(<ExecutionWizard />)

    expect(await screen.findByTestId("campaign-view-version")).toHaveTextContent(
      "18.0",
    )
  })

  it("Back on the Input screen returns to the Landing via onExitToStart", async () => {
    const onExitToStart = vi.fn()
    renderWithI18n(<ExecutionWizard onExitToStart={onExitToStart} />)
    await screen.findByTestId("base-stix-view")

    await userEvent.click(screen.getByRole("button", { name: /Voltar/ }))
    expect(onExitToStart).toHaveBeenCalledTimes(1)
  })
})
