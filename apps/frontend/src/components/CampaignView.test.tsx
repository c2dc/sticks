import { beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, screen, waitFor, within } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { CampaignView } from "@/components/CampaignView"
import type {
  ListedCampaign,
  Stage2Response,
  CampaignSelectionResult,
} from "@/lib/stages"

/**
 * CampaignView — Stage 2 (Campaign selection) of the six-stage pipeline.
 * Validates: Requirements 4.1, 6.2, 6.3, 6.5
 *
 * We mock `@/lib/stages` to control `getStageCampaign` (list + read-only
 * selection) and `selectCampaign` (attribute extraction). We assert:
 * - every bundle campaign listed and selectable (no curated/non-curated
 *   distinction in the UI, Req. 6.2);
 * - the propagation of the version to the wrappers (Req. 4.1);
 * - the selection that extracts and shows the attributes (identity + techniques)
 *   and completes the stage when `completed` (Req. 6.3, 6.5);
 * - the extraction failure surfacing as `completed === false` with the cause
 *   (Req. 6.7).
 */

vi.mock("@/lib/stages", () => ({
  getStageCampaign: vi.fn(),
  selectCampaign: vi.fn(),
}))

vi.mock("@/lib/stix", () => ({
  getCampaignOverview: vi.fn(),
}))

import { getStageCampaign, selectCampaign } from "@/lib/stages"
import { getCampaignOverview } from "@/lib/stix"
import type { StixCampaignOverview } from "@/lib/stix"

const getStageCampaignMock = vi.mocked(getStageCampaign)
const selectCampaignMock = vi.mocked(selectCampaign)
const getCampaignOverviewMock = vi.mocked(getCampaignOverview)

function campaign(
  name: string,
  slug: string | null,
  selectable: boolean,
): ListedCampaign {
  return {
    stix_id: `campaign--${name}`,
    name,
    description: `Descrição de ${name}`,
    curated: selectable,
    curated_slug: slug,
    selectable,
  }
}

/** Eight selectable curated campaigns + one non-selectable (version 18). */
function listWithEightCurated(): ListedCampaign[] {
  const curated = Array.from({ length: 8 }, (_, i) =>
    campaign(`Curada${i + 1}`, `curada-${i + 1}`, true),
  )
  const nonCurated = campaign("Externa", null, false)
  return [...curated, nonCurated]
}

function emptySelection(): CampaignSelectionResult {
  return {
    campaign_id: "shadowray",
    version: "18.0",
    completed: false,
    identity: null,
    techniques: [],
  }
}

function response(
  campaigns: ListedCampaign[],
  selection: CampaignSelectionResult = emptySelection(),
): Stage2Response {
  return {
    campaign_id: "shadowray",
    version: "18.0",
    campaigns,
    selection,
  }
}

function completedSelection(): CampaignSelectionResult {
  return {
    campaign_id: "shadowray",
    version: "18.0",
    completed: true,
    identity: {
      found: true,
      base_version: "18.0",
      stix_id: "campaign--abc",
      name: "ShadowRay",
      external_id: "C0045",
      description: "Campanha que explora clusters Ray expostos.",
      aliases: ["RayHunter"],
      first_seen: null,
      last_seen: null,
      unavailable_reason: null,
    },
    techniques: [
      {
        technique_id: "T1190",
        technique_name: "Exploit Public-Facing Application",
        tactic: "initial-access",
      },
    ],
  }
}

/** The richer overview served by `getCampaignOverview` (Req. 7.4). */
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

describe("CampaignView — Stage 2 (Req. 4.1, 6.2, 6.3, 6.5)", () => {
  beforeEach(() => {
    getStageCampaignMock.mockReset()
    selectCampaignMock.mockReset()
    getCampaignOverviewMock.mockReset()
    getCampaignOverviewMock.mockResolvedValue(campaignOverview())
  })

  it("lists all bundle campaigns and every one is selectable", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    // The 8 curated campaigns appear and are selectable (enabled).
    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    expect(curada1).not.toBeDisabled()

    const curated = screen.getAllByRole("button", { name: /Curada\d/ })
    expect(curated).toHaveLength(8)
    curated.forEach((btn) => expect(btn).not.toBeDisabled())

    // The previously "non-curated" campaign is listed and selectable too: the
    // UI makes no curated/non-curated distinction (curation is a DB detail).
    const externa = screen.getByRole("button", { name: /Externa/ })
    expect(externa).not.toBeDisabled()
  })

  it("propagates the version to getStageCampaign", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    await waitFor(() =>
      expect(getStageCampaignMock).toHaveBeenCalledWith("shadowray", "18.0"),
    )
  })

  it("selecting a campaign extracts and shows the attributes and completes the stage", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))
    selectCampaignMock.mockResolvedValue(completedSelection())
    const onCompleted = vi.fn()

    renderWithI18n(
      <CampaignView campaignId="shadowray" version="18.0" onCompleted={onCompleted} />,
    )

    // Clicking a campaign selects it directly (no separate confirm button).
    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    fireEvent.click(curada1)

    // The wrapper receives the curated slug + the version.
    await waitFor(() =>
      expect(selectCampaignMock).toHaveBeenCalledWith(
        "shadowray",
        "curada-1",
        "18.0",
      ),
    )

    // The extracted attributes are shown (identity reusing the Stage1View block
    // + techniques), and the completion indicator appears.
    await screen.findByTestId("campaign-completed")
    const attributes = screen
      .getByTestId("campaign-completed")
      .closest('[data-section="atributos-extraidos"]') as HTMLElement
    expect(attributes).not.toBeNull()
    expect(attributes).toHaveTextContent("ShadowRay")
    expect(attributes).toHaveTextContent("C0045")
    expect(attributes).toHaveTextContent("T1190")
    expect(attributes).toHaveTextContent("Exploit Public-Facing Application")

    expect(onCompleted).toHaveBeenCalledTimes(1)
    // onCompleted now reports the chosen curated slug plus the typed result so
    // the wizard can adopt the slug as the campaignId for Stages 3..6.
    expect(onCompleted).toHaveBeenCalledWith("curada-1", completedSelection())
  })

  it("selects on click with no separate confirm button and renders the campaign overview identity, aliases and techniques (Req. 7.3, 7.4)", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))
    selectCampaignMock.mockResolvedValue(completedSelection())

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    // No separate "Selecionar campanha" confirm button exists — selection is
    // driven by clicking the campaign itself (Req. 7.3). The pt-BR label for
    // the old confirm action is "Selecionar campanha".
    await screen.findByRole("button", { name: /Curada1/ })
    expect(
      screen.queryByRole("button", { name: "Selecionar campanha" }),
    ).toBeNull()

    // Clicking the campaign is the selection — it fetches the richer overview
    // via getCampaignOverview (Req. 7.4) and runs the extraction flow.
    fireEvent.click(screen.getByRole("button", { name: /Curada1/ }))

    await waitFor(() =>
      expect(getCampaignOverviewMock).toHaveBeenCalledWith("18.0", "curada-1"),
    )

    // The overview panel renders content served ONLY by getCampaignOverview.
    const panel = (await screen.findByTestId("campaign-completed")).closest(
      '[data-section="atributos-extraidos"]',
    ) as HTMLElement
    expect(panel).not.toBeNull()

    // IDENTITY: name + ATT&CK external_id from the mocked overview.
    expect(within(panel).getByText("ShadowRay")).toBeInTheDocument()
    expect(within(panel).getByText("C0045")).toBeInTheDocument()

    // ALIASES: the alias chip comes exclusively from the overview payload.
    expect(within(panel).getByText("RayHunter")).toBeInTheDocument()

    // TECHNIQUES: the technique id and name from the overview technique list.
    expect(within(panel).getByText("T1190")).toBeInTheDocument()
    expect(
      within(panel).getByText(/Exploit Public-Facing Application/),
    ).toBeInTheDocument()
  })

  it("extraction failure surfaces as completed=false with the cause (without completing)", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))
    selectCampaignMock.mockResolvedValue({
      campaign_id: "shadowray",
      version: "18.0",
      completed: false,
      identity: null,
      techniques: [],
      error_message: "Artefato curado ausente.",
      failed_step: "extracao_atributos",
    })
    const onCompleted = vi.fn()

    renderWithI18n(
      <CampaignView campaignId="shadowray" version="18.0" onCompleted={onCompleted} />,
    )

    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    fireEvent.click(curada1)

    const alert = await screen.findByTestId("campaign-failure")
    expect(alert).toHaveTextContent("Artefato curado ausente.")
    expect(screen.queryByTestId("campaign-completed")).toBeNull()
    // Extraction failed → the stage did not complete.
    expect(onCompleted).not.toHaveBeenCalled()
  })

  it("shows the 'no campaigns' note when the bundle has no campaigns", async () => {
    getStageCampaignMock.mockResolvedValue(response([]))

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    expect(
      await screen.findByText("Nenhuma campanha disponível neste bundle."),
    ).toBeInTheDocument()
  })

  // Task 9.8 — the campaign list is name-searchable (Req. 8.1).
  it("filters the campaign list by name via the search box (Req. 8.1)", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    // All nine campaigns listed before any search.
    await screen.findByRole("button", { name: /Curada1/ })
    expect(screen.getByRole("button", { name: /Externa/ })).toBeInTheDocument()

    // Typing a name narrows the list to the matching campaign only.
    const search = screen.getByRole("searchbox")
    fireEvent.change(search, { target: { value: "Externa" } })

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Externa/ })).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /Curada1/ })).toBeNull()
    })

    // A query that matches nothing shows the "no results" note.
    fireEvent.change(search, { target: { value: "zzz-nao-existe" } })
    expect(
      await screen.findByText("Nenhuma campanha corresponde a sua busca."),
    ).toBeInTheDocument()
  })

  // Task 9.8 — no idle "extracted attributes" panel before a selection (Req. 8.4).
  it("renders no idle extracted-attributes panel before a campaign is selected (Req. 8.4)", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))

    const { container } = renderWithI18n(
      <CampaignView campaignId="shadowray" version="18.0" />,
    )

    // The list is present...
    await screen.findByRole("button", { name: /Curada1/ })
    // ...but the titled attributes panel is not rendered until a selection.
    expect(container.querySelector('[data-section="atributos"]')).toBeNull()
    expect(container.querySelector('[data-section="atributos-extraidos"]')).toBeNull()
    expect(screen.queryByTestId("campaign-completed")).toBeNull()
  })

  it("renders the extracted-attributes panel only AFTER a selection (Req. 8.4)", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))
    selectCampaignMock.mockResolvedValue(completedSelection())

    const { container } = renderWithI18n(
      <CampaignView campaignId="shadowray" version="18.0" />,
    )

    // No idle panel before the selection.
    await screen.findByRole("button", { name: /Curada1/ })
    expect(container.querySelector('[data-section="atributos"]')).toBeNull()

    // After clicking the campaign, the attributes panel appears.
    fireEvent.click(screen.getByRole("button", { name: /Curada1/ }))

    await screen.findByTestId("campaign-completed")
    expect(container.querySelector('[data-section="atributos"]')).not.toBeNull()
  })

  // Regression (probe-flash bug): opening the screen must NOT adopt/flash the
  // probe campaign read-only selection (a completed OTHER campaign).
  it("does NOT flash the probe campaign read-only selection on mount", async () => {
    const probeReadOnly: CampaignSelectionResult = {
      campaign_id: "apt41_dust",
      version: "18.0",
      completed: true,
      identity: {
        found: true,
        base_version: "18.0",
        stix_id: "campaign--apt41",
        name: "APT41 DUST",
        external_id: "C0040",
        description: "probe",
        aliases: ["APT41 DUST"],
        first_seen: null,
        last_seen: null,
        unavailable_reason: null,
      },
      techniques: [],
    }
    getStageCampaignMock.mockResolvedValue(
      response(listWithEightCurated(), probeReadOnly),
    )

    const { container } = renderWithI18n(
      <CampaignView campaignId="shadowray" version="18.0" />,
    )

    await screen.findByRole("button", { name: /Curada1/ })
    expect(container.querySelector('[data-section="atributos"]')).toBeNull()
    expect(screen.queryByText("APT41 DUST")).toBeNull()
    expect(screen.queryByText("C0040")).toBeNull()
    expect(screen.queryByTestId("campaign-completed")).toBeNull()
  })

  // Regression (probe-auto-select bug): while `probing`, a read-only selection
  // of the SAME slug the list is addressed by (the probe) must NOT be adopted —
  // otherwise the probe campaign appears auto-selected with its overview shown
  // before the user picks anything.
  it("does NOT adopt the read-only selection while probing, even for the same slug", async () => {
    const readOnlySameSlug: CampaignSelectionResult = {
      ...completedSelection(),
      campaign_id: "shadowray",
    }
    getStageCampaignMock.mockResolvedValue(
      response(listWithEightCurated(), readOnlySameSlug),
    )

    const { container } = renderWithI18n(
      <CampaignView campaignId="shadowray" version="18.0" probing />,
    )

    await screen.findByRole("button", { name: /Curada1/ })
    // No overview panel and no completion indicator before an explicit pick.
    expect(container.querySelector('[data-section="atributos"]')).toBeNull()
    expect(screen.queryByTestId("campaign-completed")).toBeNull()
    // No campaign is marked as selected (aria-pressed) before an explicit pick.
    screen
      .getAllByRole("button", { name: /Curada\d|Externa/ })
      .forEach((btn) => expect(btn).toHaveAttribute("aria-pressed", "false"))
  })

  // Task 13.2 — the container icon marks ONLY the container-emulable campaign
  // (Req. 6.4, 6.5). A non-emulable campaign stays listed (never removed) with
  // an accessible, screen-reader-only reason naming the blocking techniques.
  it("shows the container icon only on the emulable campaign and keeps the non-emulable one listed with an accessible reason", async () => {
    const emulable: ListedCampaign = {
      ...campaign("Emulavel", "emulavel", true),
      container_emulable: true,
    }
    const notEmulable: ListedCampaign = {
      ...campaign("NaoEmulavel", "nao-emulavel", true),
      container_emulable: false,
      blocking_techniques: ["T1055", "T1014"],
    }
    getStageCampaignMock.mockResolvedValue(response([emulable, notEmulable]))

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    // Both campaigns are listed — the non-emulable one is never removed. The
    // accessible name is anchored (^) so "Emulavel" does not also match
    // "NaoEmulavel" (and the emulable row's name carries the icon aria-label).
    const emulableBtn = await screen.findByRole("button", { name: /^Emulavel/ })
    const notEmulableBtn = screen.getByRole("button", { name: /^NaoEmulavel/ })

    // Exactly one container icon is rendered, and it lives on the emulable
    // campaign's row — not on the non-emulable one.
    const icons = screen.getAllByTestId("container-emulable-icon")
    expect(icons).toHaveLength(1)
    expect(within(emulableBtn).getByTestId("container-emulable-icon")).toBe(icons[0])
    expect(within(notEmulableBtn).queryByTestId("container-emulable-icon")).toBeNull()

    // The non-emulable campaign carries the accessible reason naming the
    // blocking techniques instead of the icon.
    const reason = within(notEmulableBtn).getByTestId("container-not-emulable-reason")
    expect(reason).toHaveTextContent("T1055")
    expect(reason).toHaveTextContent("T1014")
    expect(
      within(emulableBtn).queryByTestId("container-not-emulable-reason"),
    ).toBeNull()
  })

  // Regression (probe-flash bug): selecting posts the CHOSEN slug and shows
  // only the chosen campaign attributes, even if the probe read-only was a
  // completed OTHER campaign.
  it("selects the chosen campaign (not the probe) and shows only its attributes", async () => {
    const probeReadOnly: CampaignSelectionResult = {
      campaign_id: "apt41_dust",
      version: "18.0",
      completed: true,
      identity: {
        found: true,
        base_version: "18.0",
        stix_id: "campaign--apt41",
        name: "APT41 DUST",
        external_id: "C0040",
        description: "probe",
        aliases: ["APT41 DUST"],
        first_seen: null,
        last_seen: null,
        unavailable_reason: null,
      },
      techniques: [],
    }
    getStageCampaignMock.mockResolvedValue(
      response(listWithEightCurated(), probeReadOnly),
    )
    selectCampaignMock.mockResolvedValue(completedSelection())

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    fireEvent.click(await screen.findByRole("button", { name: /Curada1/ }))

    await waitFor(() =>
      expect(selectCampaignMock).toHaveBeenCalledWith("shadowray", "curada-1", "18.0"),
    )

    await screen.findByTestId("campaign-completed")
    expect(screen.getByText("ShadowRay")).toBeInTheDocument()
    expect(screen.queryByText("APT41 DUST")).toBeNull()
    expect(screen.queryByTestId("campaign-failure")).toBeNull()
  })
})
