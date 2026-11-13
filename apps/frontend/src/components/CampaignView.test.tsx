import { beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, screen, waitFor, within } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { CampaignView } from "@/components/CampaignView"

/**
 * CampaignView — campaign selection within the chosen STIX package.
 * Validates: Requirements 4.1, 6.1, 6.2
 *
 * We mock `@/lib/stix` to control `getCampaigns` (the list served by
 * `GET /stix/bases/{version}/campaigns`) and `getCampaignOverview`. There is no
 * server-side selection: the selection is client state, reported to the wizard
 * through `onCompleted(campaignKey)` once the overview loads. We assert:
 * - every campaign listed and selectable (no curated/non-curated distinction);
 * - the version is propagated to the wrappers;
 * - selecting shows the overview and reports the campaign key;
 * - an overview failure surfaces an alert without completing the selection;
 * - the container-emulability marking and the name search.
 */

vi.mock("@/lib/stix", () => ({
  getCampaigns: vi.fn(),
  getCampaignOverview: vi.fn(),
}))

import { getCampaignOverview, getCampaigns } from "@/lib/stix"
import type { StixCampaignOverview, StixCampaignSummary } from "@/lib/stix"

const getCampaignsMock = vi.mocked(getCampaigns)
const getCampaignOverviewMock = vi.mocked(getCampaignOverview)

function campaign(name: string, key: string): StixCampaignSummary {
  return {
    campaign_key: key,
    stix_id: `campaign--${name}`,
    name,
    external_id: null,
    curated: true,
    curated_slug: key,
  }
}

/** Eight curated campaigns + one external one. */
function listWithEightCurated(): StixCampaignSummary[] {
  const curated = Array.from({ length: 8 }, (_, i) =>
    campaign(`Curada${i + 1}`, `curada-${i + 1}`),
  )
  return [...curated, { ...campaign("Externa", "campaign--Externa"), curated: false }]
}

/** The overview served by `getCampaignOverview` (Req. 7.4). */
function campaignOverview(): StixCampaignOverview {
  return {
    campaign_key: "curada-1",
    stix_id: "campaign--abc",
    name: "ShadowRay",
    external_id: "C0045",
    description: "Campanha que explora clusters Ray expostos.",
    aliases: ["RayHunter"],
    curated: true,
    curated_slug: "curada-1",
    techniques: [
      {
        technique_id: "T1190",
        technique_name: "Exploit Public-Facing Application",
        tactic: "initial-access",
        description: null,
      },
    ],
    actors: [{ stix_id: "intrusion-set--xyz", name: "ShadowRay Group" }],
  }
}

describe("CampaignView — campaign selection (Req. 4.1, 6.1, 6.2)", () => {
  beforeEach(() => {
    getCampaignsMock.mockReset()
    getCampaignOverviewMock.mockReset()
    getCampaignOverviewMock.mockResolvedValue(campaignOverview())
  })

  it("lists all package campaigns and every one is selectable", async () => {
    getCampaignsMock.mockResolvedValue(listWithEightCurated())

    renderWithI18n(<CampaignView version="18.0" />)

    const curated = await screen.findAllByRole("button", { name: /Curada\d/ })
    expect(curated).toHaveLength(8)
    curated.forEach((btn) => expect(btn).not.toBeDisabled())
    expect(screen.getByRole("button", { name: /Externa/ })).not.toBeDisabled()
  })

  it("propagates the version to getCampaigns", async () => {
    getCampaignsMock.mockResolvedValue(listWithEightCurated())

    renderWithI18n(<CampaignView version="18.0" />)

    await waitFor(() => expect(getCampaignsMock).toHaveBeenCalledWith("18.0"))
  })

  it("selecting a campaign shows its overview and reports the campaign key", async () => {
    getCampaignsMock.mockResolvedValue(listWithEightCurated())
    const onCompleted = vi.fn()

    renderWithI18n(<CampaignView version="18.0" onCompleted={onCompleted} />)

    // Clicking a campaign selects it directly (no separate confirm button).
    fireEvent.click(await screen.findByRole("button", { name: /Curada1/ }))

    await waitFor(() =>
      expect(getCampaignOverviewMock).toHaveBeenCalledWith("18.0", "curada-1"),
    )

    const panel = (await screen.findByTestId("campaign-completed")).closest(
      '[data-section="atributos-extraidos"]',
    ) as HTMLElement
    expect(panel).not.toBeNull()
    expect(within(panel).getByText("ShadowRay")).toBeInTheDocument()
    expect(within(panel).getByText("C0045")).toBeInTheDocument()
    expect(within(panel).getByText("RayHunter")).toBeInTheDocument()
    expect(within(panel).getByText("T1190")).toBeInTheDocument()
    expect(
      within(panel).getByText(/Exploit Public-Facing Application/),
    ).toBeInTheDocument()

    expect(onCompleted).toHaveBeenCalledTimes(1)
    expect(onCompleted).toHaveBeenCalledWith("curada-1", "Curada1")
  })

  it("an overview failure surfaces an alert without completing the selection", async () => {
    getCampaignsMock.mockResolvedValue(listWithEightCurated())
    getCampaignOverviewMock.mockRejectedValue(new Error("boom"))
    const onCompleted = vi.fn()

    renderWithI18n(<CampaignView version="18.0" onCompleted={onCompleted} />)

    fireEvent.click(await screen.findByRole("button", { name: /Curada1/ }))

    expect(await screen.findByTestId("campaign-failure")).toBeInTheDocument()
    expect(screen.queryByTestId("campaign-completed")).toBeNull()
    expect(onCompleted).not.toHaveBeenCalled()
  })

  it("shows the 'no campaigns' note when the package has no campaigns", async () => {
    getCampaignsMock.mockResolvedValue([])

    renderWithI18n(<CampaignView version="18.0" />)

    expect(
      await screen.findByText("Nenhuma campanha disponível neste bundle."),
    ).toBeInTheDocument()
  })

  it("filters the campaign list by name via the search box (Req. 8.1)", async () => {
    getCampaignsMock.mockResolvedValue(listWithEightCurated())

    renderWithI18n(<CampaignView version="18.0" />)

    await screen.findByRole("button", { name: /Curada1/ })
    expect(screen.getByRole("button", { name: /Externa/ })).toBeInTheDocument()

    const search = screen.getByRole("searchbox")
    fireEvent.change(search, { target: { value: "Externa" } })

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Externa/ })).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /Curada1/ })).toBeNull()
    })

    fireEvent.change(search, { target: { value: "zzz-nao-existe" } })
    expect(
      await screen.findByText("Nenhuma campanha corresponde a sua busca."),
    ).toBeInTheDocument()
  })

  it("renders no overview panel and marks no campaign before a selection (Req. 8.4)", async () => {
    getCampaignsMock.mockResolvedValue(listWithEightCurated())

    const { container } = renderWithI18n(<CampaignView version="18.0" />)

    await screen.findByRole("button", { name: /Curada1/ })
    expect(container.querySelector('[data-section="atributos"]')).toBeNull()
    expect(container.querySelector('[data-section="atributos-extraidos"]')).toBeNull()
    expect(screen.queryByTestId("campaign-completed")).toBeNull()
    screen
      .getAllByRole("button", { name: /Curada\d|Externa/ })
      .forEach((btn) => expect(btn).toHaveAttribute("aria-pressed", "false"))
  })

  // The container icon marks ONLY the container-emulable campaign (Req. 6.4,
  // 6.5). A non-emulable campaign stays listed with an accessible reason.
  it("shows the container icon only on the emulable campaign and keeps the non-emulable one listed with an accessible reason", async () => {
    const emulable: StixCampaignSummary = {
      ...campaign("Emulavel", "emulavel"),
      container_emulable: true,
    }
    const notEmulable: StixCampaignSummary = {
      ...campaign("NaoEmulavel", "nao-emulavel"),
      container_emulable: false,
      blocking_techniques: ["T1055", "T1014"],
    }
    getCampaignsMock.mockResolvedValue([emulable, notEmulable])

    renderWithI18n(<CampaignView version="18.0" />)

    const emulableBtn = await screen.findByRole("button", { name: /^Emulavel/ })
    const notEmulableBtn = screen.getByRole("button", { name: /^NaoEmulavel/ })

    const icons = screen.getAllByTestId("container-emulable-icon")
    expect(icons).toHaveLength(1)
    expect(within(emulableBtn).getByTestId("container-emulable-icon")).toBe(icons[0])
    expect(within(notEmulableBtn).queryByTestId("container-emulable-icon")).toBeNull()

    const reason = within(notEmulableBtn).getByTestId("container-not-emulable-reason")
    expect(reason).toHaveTextContent("T1055")
    expect(reason).toHaveTextContent("T1014")
    expect(
      within(emulableBtn).queryByTestId("container-not-emulable-reason"),
    ).toBeNull()
  })
})
