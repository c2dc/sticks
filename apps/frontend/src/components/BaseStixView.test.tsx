import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import i18n from "@/i18n"
import { BaseStixView } from "@/components/BaseStixView"

/**
 * BaseStixView — STIX base selection input step (Req. 7.1–7.4, 9.3).
 *
 * We mock the completion verdict (`getBases`) and the compact base
 * picker (`StixBaseSelect`) with a minimal stub, to focus on BaseStixView's own
 * logic: the picker↔confirmation switch driven by
 * `version` and the propagation of the completion verdict
 * (`onCompletionChange`) that enables the wizard's "Next" (task 14.4).
 *
 * Picking a version does NOT require the heavy intelligence dashboard (Req.
 * 7.3): completion is driven off the persisted-base check alone.
 */

vi.mock("@/lib/stix", () => ({
  getBases: vi.fn(),
  getPackageOverview: vi.fn(),
  getPackageAnalytics: vi.fn(),
}))

vi.mock("@/components/StixBaseSelect", () => ({
  StixBaseSelect: ({
    onSelectBase,
  }: {
    onSelectBase: (version: string) => void
  }) => (
    <button type="button" onClick={() => onSelectBase("18.0")}>
      stub-select-18
    </button>
  ),
}))

import {
  getBases,
  getPackageAnalytics,
  type BaseStixInfo,
  getPackageOverview,
  type StixPackageAnalytics,
  type StixPackageOverview,
} from "@/lib/stix"

const getBasesMock = vi.mocked(getBases)
const getPackageOverviewMock = vi.mocked(getPackageOverview)
const getPackageAnalyticsMock = vi.mocked(getPackageAnalytics)

function makeOverview(): StixPackageOverview {
  return {
    version: "18.0",
    name: "Enterprise ATT&CK",
    downloaded_at: "2024-01-01T00:00:00Z",
    // The per-type counts list is keyed by the stable English `stix_type`, not
    // by the backend `label`. We deliberately feed a backend label that is
    // NOT the rendered copy ("backend-label") so the assertions prove the list
    // renders the localized i18n value looked up by `stix_type` ("attack-pattern"
    // → baseStix.overview.techniques) rather than echoing the payload label.
    counts: [
      {
        label: "backend-label",
        stix_type: "attack-pattern",
        count: 3,
        description: "",
      },
    ],
    campaigns_total: 3,
    actors_total: 5,
    techniques_total: 12,
    tactics_total: 7,
    malware_total: 4,
    relationships_total: 20,
  }
}

function makeAnalytics(): StixPackageAnalytics {
  return {
    version: "18.0",
    curated_total: 2,
    uncurated_total: 1,
    top_actors: [
      { stix_id: "actor-1", name: "APT Example", campaigns_total: 2 },
    ],
    top_campaigns: [
      {
        campaign_key: "shadowray",
        name: "ShadowRay",
        curated: true,
        techniques_total: 4,
      },
    ],
    tactics: [{ tactic: "execution", techniques_total: 3 }],
    top_techniques: [
      {
        technique_id: "T1059",
        technique_name: "Command and Scripting Interpreter",
        campaigns_total: 2,
      },
    ],
  }
}

function makeResult(persisted: boolean): BaseStixInfo[] {
  return [
    {
      version: "18.0",
      name: "Enterprise ATT&CK v18.0",
      downloaded: persisted,
      selectable: persisted,
      file: null,
      completed: persisted,
    },
  ]
}

describe("BaseStixView (STIX base selection)", () => {
  beforeEach(async () => {
    getBasesMock.mockReset()
    getPackageOverviewMock.mockReset()
    getPackageOverviewMock.mockResolvedValue(makeOverview())
    getPackageAnalyticsMock.mockReset()
    getPackageAnalyticsMock.mockResolvedValue(makeAnalytics())
    // The shared i18n instance persists its language across tests; reset to the
    // pt-BR default so each case starts from a known locale.
    await i18n.changeLanguage("pt-BR")
  })

  it("lists bases when no version is selected and reports not-completed", async () => {
    const onCompletionChange = vi.fn()
    const onVersionSelected = vi.fn()

    renderWithI18n(
      <BaseStixView
        onCompletionChange={onCompletionChange}
        onVersionSelected={onVersionSelected}
      />,
    )

    // No version: the base list is shown (Req. 7.1, 7.2).
    expect(screen.getByText("stub-select-18")).toBeInTheDocument()

    // With no base selected, the step cannot be completed.
    await waitFor(() => {
      expect(onCompletionChange).toHaveBeenCalledWith(false)
    })
    // And it does not resolve the backend verdict without a version.
    expect(getBasesMock).not.toHaveBeenCalled()

    // Picking a base propagates the chosen version to the wizard (Req. 7.1, 7.4).
    await userEvent.setup().click(screen.getByText("stub-select-18"))
    expect(onVersionSelected).toHaveBeenCalledWith("18.0")
  })

  it("shows a lightweight confirmation and completes off the bundle-on-disk check without the dashboard (Req. 7.3, 7.4)", async () => {
    getBasesMock.mockResolvedValue(makeResult(true))
    const onCompletionChange = vi.fn()

    renderWithI18n(
      <BaseStixView
        version="18.0"
        onCompletionChange={onCompletionChange}
      />,
    )

    // With a version: a lightweight confirmation is shown, the list is not.
    expect(
      await screen.findByText("Base selecionada: v18.0"),
    ).toBeInTheDocument()
    expect(screen.queryByText("stub-select-18")).toBeNull()
    expect(getBasesMock).toHaveBeenCalled()

    // Bundle on disk → completion enabled, no dashboard required (Req. 7.3, 7.4).
    await waitFor(() => {
      expect(onCompletionChange).toHaveBeenLastCalledWith(true)
    })
    const banner = await screen.findByText("Download concluído.")
    expect(banner).toBeInTheDocument()

    // The package overview loads via getPackageOverview and renders (Req. 6.4,
    // 7.2): the panel title plus the identity and a headline total.
    expect(getPackageOverviewMock).toHaveBeenCalledWith("18.0")
    expect(
      await screen.findByText("Visão geral do pacote"),
    ).toBeInTheDocument()
    expect(screen.getByText("Enterprise ATT&CK — v18.0")).toBeInTheDocument()
  })

  it("renders the package overview identity and headline totals from getPackageOverview (Req. 7.2)", async () => {
    getBasesMock.mockResolvedValue(makeResult(true))

    renderWithI18n(<BaseStixView version="18.0" />)

    // The overview panel is driven entirely by the mocked getPackageOverview.
    expect(getPackageOverviewMock).toHaveBeenCalledWith("18.0")
    const panel = (await screen.findByText("Visão geral do pacote")).closest(
      '[data-section="overview"]',
    ) as HTMLElement
    expect(panel).not.toBeNull()

    // IDENTITY: the base name and version line.
    expect(
      within(panel).getByText("Enterprise ATT&CK — v18.0"),
    ).toBeInTheDocument()

    // HEADLINE TOTALS: each total renders under its own labelled tile with the
    // numeric value from the overview (campaigns 3, actors 5, techniques 12,
    // tactics 7, malware 4, relationships 20). The pt-BR labels resolve from
    // the i18n instance.
    const totals: [string, string, string][] = [
      ["campaigns", "Campanhas", "3"],
      ["actors", "Atores", "5"],
      ["techniques", "Técnicas", "12"],
      ["tactics", "Táticas", "7"],
      ["malware", "Malware", "4"],
      ["relationships", "Relacionamentos", "20"],
    ]
    for (const [key, label, value] of totals) {
      const tile = panel.querySelector(
        `[data-total="${key}"]`,
      ) as HTMLElement | null
      expect(tile).not.toBeNull()
      expect(within(tile as HTMLElement).getByText(label)).toBeInTheDocument()
      expect(within(tile as HTMLElement).getByText(value)).toBeInTheDocument()
    }

  })

  it("renders the headline total labels localized in each locale, preserving the bilingual UI (Req. 2.3, 3.1, 3.2)", async () => {
    getBasesMock.mockResolvedValue(makeResult(true))

    // en locale → the techniques tile label resolves to the English i18n value.
    await i18n.changeLanguage("en")
    const { unmount } = renderWithI18n(
      <BaseStixView version="18.0" />,
    )

    const enTile = (await screen.findByText("Package overview"))
      .closest('[data-section="overview"]')!
      .querySelector('[data-total="techniques"]') as HTMLElement
    expect(enTile).not.toBeNull()
    // The localized tile label renders ("techniques" → "Techniques"), proving
    // the stable English i18n key drives the copy, not the backend label.
    expect(within(enTile).getByText("Techniques")).toBeInTheDocument()
    expect(within(enTile).queryByText("backend-label")).toBeNull()
    unmount()

    // pt-BR locale → the SAME key resolves to the Portuguese i18n value,
    // showing the bilingual UI is preserved off a single stable key.
    await i18n.changeLanguage("pt-BR")
    renderWithI18n(<BaseStixView version="18.0" />)

    const ptTile = (await screen.findByText("Visão geral do pacote"))
      .closest('[data-section="overview"]')!
      .querySelector('[data-total="techniques"]') as HTMLElement
    expect(ptTile).not.toBeNull()
    expect(within(ptTile).getByText("Técnicas")).toBeInTheDocument()
    expect(within(ptTile).queryByText("backend-label")).toBeNull()
  })

  it("returns to the base list via the change-base action (Req. 7.3)", async () => {
    getBasesMock.mockResolvedValue(makeResult(true))
    const onVersionSelected = vi.fn()

    renderWithI18n(
      <BaseStixView
        version="18.0"
        onVersionSelected={onVersionSelected}
      />,
    )

    await screen.findByText("Base selecionada: v18.0")
    await userEvent.setup().click(screen.getByText("Trocar base"))
    // Clearing the version returns the wizard to the base list.
    expect(onVersionSelected).toHaveBeenCalledWith("")
  })

  it("renders the two rankings (techniques per campaign, campaigns per technique) from getPackageAnalytics", async () => {
    getBasesMock.mockResolvedValue(makeResult(true))

    renderWithI18n(<BaseStixView version="18.0" />)

    // The analytics panel is driven entirely by the mocked getPackageAnalytics.
    expect(getPackageAnalyticsMock).toHaveBeenCalledWith("18.0")
    const panel = (await screen.findByText("Análise do pacote")).closest(
      '[data-section="analytics"]',
    ) as HTMLElement
    expect(panel).not.toBeNull()

    // TECHNIQUES PER CAMPAIGN: the campaign renders its name (no curated badge —
    // the UI makes no curated distinction), its technique count and a bar.
    const campaigns = panel.querySelector(
      '[data-section="analytics-campaigns"]',
    ) as HTMLElement
    expect(campaigns).not.toBeNull()
    expect(within(campaigns).getByText("Técnicas por campanha")).toBeInTheDocument()
    const campaignRow = campaigns.querySelector(
      '[data-campaign="shadowray"]',
    ) as HTMLElement
    expect(campaignRow).not.toBeNull()
    expect(within(campaignRow).getByText("ShadowRay")).toBeInTheDocument()
    expect(campaignRow.querySelector('[data-badge="curated"]')).toBeNull()
    expect(within(campaignRow).getByText("4 técnicas")).toBeInTheDocument()
    const campaignBar = campaignRow.querySelector(
      '[data-bar="campaign"]',
    ) as HTMLElement
    expect(campaignBar).not.toBeNull()
    expect(campaignBar.style.width).toBe("100%")

    // CAMPAIGNS PER TECHNIQUE (inverse view): the technique renders its id, its
    // human-readable name, the distinct-campaign count and a proportional bar.
    const techniques = panel.querySelector(
      '[data-section="analytics-techniques"]',
    ) as HTMLElement
    expect(techniques).not.toBeNull()
    expect(
      within(techniques).getByText("Campanhas por técnica"),
    ).toBeInTheDocument()
    const techniqueRow = techniques.querySelector(
      '[data-technique="T1059"]',
    ) as HTMLElement
    expect(techniqueRow).not.toBeNull()
    expect(within(techniqueRow).getByText("T1059")).toBeInTheDocument()
    expect(
      within(techniqueRow).getByText("Command and Scripting Interpreter"),
    ).toBeInTheDocument()
    expect(within(techniqueRow).getByText("2 campanhas")).toBeInTheDocument()
    const techniqueBar = techniqueRow.querySelector(
      '[data-bar="technique"]',
    ) as HTMLElement
    expect(techniqueBar).not.toBeNull()
    expect(techniqueBar.style.width).toBe("100%")

    // The removed sections no longer render.
    expect(panel.querySelector('[data-section="analytics-actors"]')).toBeNull()
    expect(panel.querySelector('[data-section="analytics-tactics"]')).toBeNull()
    expect(panel.querySelector('[data-card="curated"]')).toBeNull()
  })

  it("shows the analytics error note without breaking the overview when getPackageAnalytics rejects (Req. 12.9)", async () => {
    getBasesMock.mockResolvedValue(makeResult(true))
    // The analytics read fails; the overview read still succeeds (default mock).
    getPackageAnalyticsMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<BaseStixView version="18.0" />)

    // The analytics panel surfaces only its error note and no partial data.
    const analyticsError = await screen.findByText(
      "Não foi possível carregar a análise do pacote.",
    )
    expect(analyticsError).toBeInTheDocument()
    expect(analyticsError.closest('[data-section="analytics"]')).not.toBeNull()
    expect(screen.queryByText("Análise do pacote")).toBeNull()

    // The overview panel is isolated from the analytics failure: it still
    // renders its title and the base identity from getPackageOverview.
    const overview = (await screen.findByText("Visão geral do pacote")).closest(
      '[data-section="overview"]',
    ) as HTMLElement
    expect(overview).not.toBeNull()
    expect(
      within(overview).getByText("Enterprise ATT&CK — v18.0"),
    ).toBeInTheDocument()
  })

  it("keeps the step not-completed and reports the cause on a load failure", async () => {
    getBasesMock.mockRejectedValue(new Error("boom"))
    const onCompletionChange = vi.fn()

    renderWithI18n(
      <BaseStixView
        version="18.0"
        onCompletionChange={onCompletionChange}
      />,
    )

    // The failure cause is shown and the step stays not completed.
    await screen.findByText("Não foi possível concluir a Seleção da base STIX.")
    await waitFor(() => {
      expect(onCompletionChange).toHaveBeenLastCalledWith(false)
    })
  })
})
