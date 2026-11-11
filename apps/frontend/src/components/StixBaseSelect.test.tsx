import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import i18n from "@/i18n"
import { renderWithI18n } from "@/test/render"
import { StixBaseSelect } from "@/components/StixBaseSelect"
import type { BaseStixInfo } from "@/lib/stix"

/**
 * StixBaseSelect — the click-driven STIX base picker (Req. 7.1, 7.5).
 *
 * We mock the base listing/download wrappers so the test focuses on the
 * component's own behavior: clicking a downloaded version selects it directly
 * on the click (no separate "Select version" button), while clicking a
 * not-yet-downloaded version reveals the inline Download action and leaves the
 * download an explicit step.
 */

vi.mock("@/lib/stix", () => ({
  getBases: vi.fn(),
  downloadBase: vi.fn(),
}))

import { getBases, downloadBase } from "@/lib/stix"

const getBasesMock = vi.mocked(getBases)
const downloadBaseMock = vi.mocked(downloadBase)

function makeBase(
  version: string,
  downloaded: boolean,
  name = `Base ${version}`,
): BaseStixInfo {
  return {
    version,
    name,
    downloaded,
    selectable: downloaded,
    file: downloaded ? `stix-${version}.json` : null,
  }
}

describe("StixBaseSelect (click-driven base picker)", () => {
  beforeEach(() => {
    getBasesMock.mockReset()
    downloadBaseMock.mockReset()
  })

  afterEach(async () => {
    // The shared i18n instance persists its language across tests; reset to the
    // pt-BR default so each case starts from a known locale.
    await i18n.changeLanguage("pt-BR")
  })

  it("selects a downloaded version directly on click, with no select button (Req. 7.1)", async () => {
    getBasesMock.mockResolvedValue([
      makeBase("18.0", true),
      makeBase("17.0", false),
    ])
    const onSelectBase = vi.fn()
    const user = userEvent.setup()

    renderWithI18n(<StixBaseSelect onSelectBase={onSelectBase} />)

    // Open the dropdown and click the downloaded version. The i18n instance
    // defaults to pt-BR, so chrome strings resolve to their pt-BR values. The
    // clickable element is the option's inner button (StateIcon + version).
    await user.click(await screen.findByRole("button", { name: "Bases STIX" }))
    const option = within(
      await screen.findByRole("option", { name: /18\.0/ }),
    ).getByRole("button")
    await user.click(option)

    // The click itself selects the version — no separate "select" button.
    expect(onSelectBase).toHaveBeenCalledWith("18.0")
    expect(
      screen.queryByRole("button", { name: "Selecionar" }),
    ).toBeNull()
  })

  it("reveals the inline Download for a not-yet-downloaded version without selecting it (Req. 7.5)", async () => {
    getBasesMock.mockResolvedValue([
      makeBase("18.0", true),
      makeBase("17.0", false),
    ])
    const onSelectBase = vi.fn()
    const user = userEvent.setup()

    renderWithI18n(<StixBaseSelect onSelectBase={onSelectBase} />)

    await user.click(await screen.findByRole("button", { name: "Bases STIX" }))
    const option = within(
      await screen.findByRole("option", { name: /17\.0/ }),
    ).getByRole("button")
    await user.click(option)

    // No selection happens; instead the inline Download action is revealed and
    // the download stays an explicit step. The pt-BR download label is "Baixar".
    expect(onSelectBase).not.toHaveBeenCalled()
    const downloadButton = await screen.findByRole("button", {
      name: "Baixar",
    })
    expect(downloadButton).toBeInTheDocument()
    expect(downloadBaseMock).not.toHaveBeenCalled()

    // Clicking Download is the explicit action that fetches the base.
    downloadBaseMock.mockResolvedValue(makeBase("17.0", true))
    getBasesMock.mockResolvedValue([
      makeBase("18.0", true),
      makeBase("17.0", true),
    ])
    await user.click(downloadButton)
    await waitFor(() => {
      expect(downloadBaseMock).toHaveBeenCalledWith("17.0")
    })
  })

  it("renders the State_Icon label for a persisted version localized by the stable key in each locale, preserving the bilingual UI (Req. 4.1, 4.4)", async () => {
    getBasesMock.mockResolvedValue([makeBase("18.0", true)])

    // en locale → the persisted (downloaded) version's State_Icon communicates
    // "Available in the database" via the stable key `stix.bases.alreadyDownloaded`.
    await i18n.changeLanguage("en")
    const user = userEvent.setup()
    const { unmount } = renderWithI18n(<StixBaseSelect onSelectBase={vi.fn()} />)

    await user.click(await screen.findByRole("button", { name: "STIX bases" }))
    const enOption = within(
      await screen.findByRole("option", { name: /18\.0/ }),
    ).getByRole("button")
    await user.click(enOption)

    // The selected persisted version shows the State_Icon on the trigger with the
    // English value — proving the key resolves to database-availability copy.
    expect(
      await screen.findByLabelText("Available in the database"),
    ).toBeInTheDocument()
    // The Portuguese value must NOT be present in the en locale.
    expect(screen.queryByLabelText("Disponível no banco de dados")).toBeNull()
    unmount()

    // pt-BR locale → the SAME stable key resolves to the Portuguese value,
    // showing the bilingual UI is preserved off a single English key.
    await i18n.changeLanguage("pt-BR")
    renderWithI18n(<StixBaseSelect onSelectBase={vi.fn()} />)

    await user.click(await screen.findByRole("button", { name: "Bases STIX" }))
    const ptOption = within(
      await screen.findByRole("option", { name: /18\.0/ }),
    ).getByRole("button")
    await user.click(ptOption)

    expect(
      await screen.findByLabelText("Disponível no banco de dados"),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText("Available in the database")).toBeNull()
  })
})
