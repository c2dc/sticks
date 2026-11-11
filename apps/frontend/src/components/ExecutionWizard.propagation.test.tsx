import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { CasesResponse } from "@/lib/campaigns"
import type { Stage1ResultStix } from "@/lib/stages"
import type { BaseStixInfo } from "@/lib/stix"

/**
 * Feature: ai-first-campaign-flow (task 9.10) — propagation of the chosen STIX
 * base version into Setup step A ({@link BaseStixView}).
 *
 * The two-layer shell renders the real `<BaseStixView campaignId={probe} />` as
 * the first Setup step. With no base selected yet, step A shows the bases list
 * (StixBasesList → `getBases`) and does NOT read the completion verdict. Once a
 * base is selected, BaseStixView reads `getStageBaseStix(probe, version)` with
 * the chosen version — that call is the propagation assertion.
 *
 * Only the network wrappers (`@/lib/stages`, `@/lib/stix`, `@/lib/campaigns`)
 * are mocked; BaseStixView renders for real. The Run view is stubbed since it is
 * never reached in these tests.
 */

vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return { ...actual, getStageBaseStix: vi.fn() }
})

vi.mock("@/lib/stix", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stix")>()
  return { ...actual, getBases: vi.fn(), getPackageOverview: vi.fn() }
})

vi.mock("@/lib/campaigns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/campaigns")>()
  return { ...actual, fetchCases: vi.fn() }
})

vi.mock("@/components/GuidedRunView", () => ({
  GuidedRunView: ({ campaignId }: { campaignId: string }) => (
    <div data-testid="guided-run-view">{campaignId}</div>
  ),
}))

import { getStageBaseStix } from "@/lib/stages"
import { getBases, getPackageOverview, type StixPackageOverview } from "@/lib/stix"
import { fetchCases } from "@/lib/campaigns"

const getStageBaseStixMock = vi.mocked(getStageBaseStix)
const getBasesMock = vi.mocked(getBases)
const getPackageOverviewMock = vi.mocked(getPackageOverview)
const fetchCasesMock = vi.mocked(fetchCases)

function makeCases(): CasesResponse {
  return {
    campaigns: [{ id: "shadowray", name: "ShadowRay" }],
    progress: { completed_campaigns: 0, total_campaigns: 8, percentage: 0 },
  }
}

function makeStage1Result(version: string): Stage1ResultStix {
  return {
    version,
    completed: true,
    bundle_on_disk: true,
    dashboard: null,
    campaigns: [],
    curated: true,
  }
}

function makeOverview(version: string): StixPackageOverview {
  return {
    version,
    name: "MITRE ATT&CK v18.0",
    downloaded_at: null,
    counts: [],
    campaigns_total: 0,
    actors_total: 0,
    techniques_total: 0,
    tactics_total: 0,
    malware_total: 0,
    relationships_total: 0,
  }
}

const bases: BaseStixInfo[] = [
  {
    version: "18.0",
    name: "MITRE ATT&CK v18.0",
    downloaded: true,
    selectable: true,
    file: "enterprise-attack-18.0.json",
  },
]

describe("ExecutionWizard — propagates the version to Setup step A (task 9.10)", () => {
  beforeEach(() => {
    getStageBaseStixMock.mockReset()
    getStageBaseStixMock.mockResolvedValue(makeStage1Result("18.0"))
    getBasesMock.mockReset()
    getBasesMock.mockResolvedValue(bases)
    getPackageOverviewMock.mockReset()
    getPackageOverviewMock.mockResolvedValue(makeOverview("18.0"))
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
  })

  it("on selecting a base: BaseStixView calls getStageBaseStix(probe, version)", async () => {
    renderWithI18n(<ExecutionWizard />)

    // No base selected yet: the bases list loads, no verdict read.
    await waitFor(() => expect(getBasesMock).toHaveBeenCalled())
    expect(getStageBaseStixMock).not.toHaveBeenCalled()

    // The picker starts empty: open the dropdown and click 18.0. A downloaded
    // version is selected directly on the click (Req. 7.1) — no separate
    // "Selecionar" button. The verdict read fires with the probe id resolved
    // from fetchCases.
    const user = userEvent.setup()
    // Open the dropdown (trigger carries the "Bases STIX" aria-label).
    await user.click(await screen.findByRole("button", { name: /Bases STIX/ }))
    // The options render as buttons inside the listbox; clicking 18.0 selects.
    const option = await screen.findByRole("option", { name: /18\.0/ })
    await user.click(option.querySelector("button") as HTMLButtonElement)

    await waitFor(() =>
      expect(getStageBaseStixMock).toHaveBeenCalledWith("shadowray", "18.0"),
    )
  })

  it("with no base selected: Setup step A shows the bases list and does not query the verdict", async () => {
    renderWithI18n(<ExecutionWizard />)

    await waitFor(() => expect(getBasesMock).toHaveBeenCalled())
    expect(getStageBaseStixMock).not.toHaveBeenCalled()
  })
})
