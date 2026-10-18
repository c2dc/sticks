import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { BaseStixView } from "@/components/BaseStixView"
import type { Stage1ResultStix } from "@/lib/stages"

/**
 * BaseStixView — Stage 1 (STIX base selection) (Task 14.1, Req. 4.1, 5.7,
 * 5.8, 5.10).
 *
 * We mock the completion verdict (`getStageBaseStix`) and the two screens
 * reused from the fluxo-stix-dashboard flow (`StixBasesList`,
 * `StixIntelligenceDashboard`) with minimal stubs, to focus on BaseStixView's
 * own logic: the list↔dashboard switch driven by `version` and the propagation
 * of the completion verdict (`onCompletionChange`) that enables the wizard's
 * "Next" (task 14.4).
 */

vi.mock("@/lib/stages", () => ({
  getStageBaseStix: vi.fn(),
}))

vi.mock("@/components/StixBasesList", () => ({
  StixBasesList: ({
    onSelectBase,
  }: {
    onSelectBase: (version: string) => void
  }) => (
    <button type="button" onClick={() => onSelectBase("18.0")}>
      stub-selecionar-18
    </button>
  ),
}))

vi.mock("@/components/StixIntelligenceDashboard", () => ({
  StixIntelligenceDashboard: ({ version }: { version: string }) => (
    <div data-testid="dashboard-stub">dashboard-{version}</div>
  ),
}))

import { getStageBaseStix } from "@/lib/stages"

const getStageBaseStixMock = vi.mocked(getStageBaseStix)

function makeResult(completed: boolean): Stage1ResultStix {
  return {
    version: "18.0",
    completed,
    bundle_on_disk: completed,
    dashboard: completed ? { version: "18.0", name: "v18.0", counts: [] } : null,
    campaigns: [],
    curated: true,
    error_message: completed ? null : undefined,
  }
}

describe("BaseStixView (Stage 1 — STIX base selection)", () => {
  beforeEach(() => {
    getStageBaseStixMock.mockReset()
  })

  it("lists bases when no version is selected and reports not-completed", async () => {
    const onCompletionChange = vi.fn()
    const onVersionSelected = vi.fn()

    renderWithI18n(
      <BaseStixView
        campaignId="shadowray"
        onCompletionChange={onCompletionChange}
        onVersionSelected={onVersionSelected}
      />,
    )

    // No version: the base list is shown, the dashboard is not (Req. 5.1–5.4).
    expect(screen.getByText("stub-selecionar-18")).toBeInTheDocument()
    expect(screen.queryByTestId("dashboard-stub")).toBeNull()

    // With no base selected, Stage 1 cannot be completed (Req. 5.10).
    await waitFor(() => {
      expect(onCompletionChange).toHaveBeenCalledWith(false)
    })
    // And it does not resolve the backend verdict without a version.
    expect(getStageBaseStixMock).not.toHaveBeenCalled()

    // Picking a base propagates the chosen version to the wizard (Req. 5.1).
    await userEvent.setup().click(screen.getByText("stub-selecionar-18"))
    expect(onVersionSelected).toHaveBeenCalledWith("18.0")
  })

  it("shows the bundle dashboard and enables completion when the bundle is on disk (Req. 5.7, 5.8, 5.10)", async () => {
    getStageBaseStixMock.mockResolvedValue(makeResult(true))
    const onCompletionChange = vi.fn()

    renderWithI18n(
      <BaseStixView
        campaignId="shadowray"
        version="18.0"
        onCompletionChange={onCompletionChange}
      />,
    )

    // With a version: the bundle dashboard is shown, the list is not.
    expect(await screen.findByTestId("dashboard-stub")).toHaveTextContent(
      "dashboard-18.0",
    )
    expect(screen.queryByText("stub-selecionar-18")).toBeNull()
    expect(getStageBaseStixMock).toHaveBeenCalledWith("shadowray", "18.0")

    // Bundle on disk + dashboard shown → completion enabled (Req. 5.8, 5.10).
    await waitFor(() => {
      expect(onCompletionChange).toHaveBeenLastCalledWith(true)
    })
    const banner = await screen.findByText(
      "Base em disco e dashboard exibido — o Estágio 1 pode ser concluído.",
    )
    expect(banner).toBeInTheDocument()
  })

  it("keeps Stage 1 not-completed and reports the cause on a load failure (Req. 5.9)", async () => {
    getStageBaseStixMock.mockRejectedValue(new Error("boom"))
    const onCompletionChange = vi.fn()

    renderWithI18n(
      <BaseStixView
        campaignId="shadowray"
        version="18.0"
        onCompletionChange={onCompletionChange}
      />,
    )

    // The failure cause is shown and the stage stays not completed.
    await screen.findByText("Não foi possível concluir a Seleção da base STIX.")
    await waitFor(() => {
      expect(onCompletionChange).toHaveBeenLastCalledWith(false)
    })
  })
})
