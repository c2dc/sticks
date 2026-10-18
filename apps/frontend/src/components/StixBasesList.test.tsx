import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { StixBasesList } from "@/components/StixBasesList"
import { ApiError } from "@/lib/api"
import type { BaseStixInfo } from "@/lib/stix"

// Mock the STIX API wrappers so no network is used.
vi.mock("@/lib/stix", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stix")>()
  return {
    ...actual,
    getBases: vi.fn(),
    downloadBase: vi.fn(),
  }
})

import { getBases, downloadBase } from "@/lib/stix"

const getBasesMock = vi.mocked(getBases)
const downloadBaseMock = vi.mocked(downloadBase)

/** Newest → oldest, with one downloaded and one not-downloaded base. */
function makeBases(): BaseStixInfo[] {
  return [
    {
      version: "15.1",
      name: "Enterprise ATT&CK v15.1",
      downloaded: false,
      selectable: false,
      file: null,
    },
    {
      version: "14.0",
      name: "Enterprise ATT&CK v14.0",
      downloaded: true,
      selectable: true,
      file: "enterprise-attack-14.0.json",
    },
  ]
}

/** The rendered base cards, in DOM order (which must mirror the input order). */
function baseCards(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>("[data-version]"))
}

describe("StixBasesList (Req. 2 — bases listing, badge, download)", () => {
  beforeEach(() => {
    getBasesMock.mockReset()
    downloadBaseMock.mockReset()
  })

  it("renders bases in backend order (newest→oldest) with the badge only on the downloaded one", async () => {
    getBasesMock.mockResolvedValue(makeBases())

    const { container } = renderWithI18n(<StixBasesList onSelectBase={vi.fn()} />)

    await screen.findByText("Enterprise ATT&CK v15.1")

    const cards = baseCards(container)
    expect(cards).toHaveLength(2)
    // Order preserved: 15.1 first, 14.0 second (Req. 2.1).
    expect(cards[0].getAttribute("data-version")).toBe("15.1")
    expect(cards[1].getAttribute("data-version")).toBe("14.0")

    // "Já baixada" badge only on the downloaded base (14.0) (Req. 2.2).
    const badges = screen.getAllByText("Já baixada")
    expect(badges).toHaveLength(1)
    expect(within(cards[1]).getByText("Já baixada")).toBeInTheDocument()
    expect(within(cards[0]).queryByText("Já baixada")).toBeNull()

    // The not-downloaded base (15.1) exposes a "Baixar" button (Req. 2.4).
    expect(within(cards[0]).getByRole("button", { name: "Baixar" })).toBeInTheDocument()
    // The downloaded base exposes "Selecionar" instead.
    expect(within(cards[1]).getByRole("button", { name: "Selecionar" })).toBeInTheDocument()
    expect(within(cards[0]).queryByRole("button", { name: "Selecionar" })).toBeNull()
  })

  it("re-fetches and flips the row to downloaded on a successful download (Req. 2.6)", async () => {
    // First load: 15.1 not downloaded. After the download succeeds, the re-fetch
    // returns it as downloaded so the row flips.
    getBasesMock
      .mockResolvedValueOnce(makeBases())
      .mockResolvedValueOnce([
        {
          version: "15.1",
          name: "Enterprise ATT&CK v15.1",
          downloaded: true,
          selectable: true,
          file: "enterprise-attack-15.1.json",
        },
        makeBases()[1],
      ])
    downloadBaseMock.mockResolvedValue({
      version: "15.1",
      name: "Enterprise ATT&CK v15.1",
      downloaded: true,
      selectable: true,
      file: "enterprise-attack-15.1.json",
    })
    const user = userEvent.setup()

    const { container } = renderWithI18n(<StixBasesList onSelectBase={vi.fn()} />)

    await screen.findByText("Enterprise ATT&CK v15.1")
    const downloadButton = within(baseCards(container)[0]).getByRole("button", { name: "Baixar" })

    await user.click(downloadButton)

    await waitFor(() => {
      expect(downloadBaseMock).toHaveBeenCalledWith("15.1")
    })
    // The list was re-fetched (initial load + post-download).
    await waitFor(() => {
      expect(getBasesMock).toHaveBeenCalledTimes(2)
    })
    // The 15.1 row flipped to downloaded: badge now present, "Selecionar" shown.
    await waitFor(() => {
      const card = baseCards(container)[0]
      expect(within(card).getByText("Já baixada")).toBeInTheDocument()
      expect(within(card).getByRole("button", { name: "Selecionar" })).toBeInTheDocument()
    })
  })

  it("shows a per-row error and stays not-downloaded when the download fails (Req. 2.7)", async () => {
    getBasesMock.mockResolvedValue(makeBases())
    downloadBaseMock.mockRejectedValue(new ApiError(502, "Falha na rede ao baixar."))
    const user = userEvent.setup()

    const { container } = renderWithI18n(<StixBasesList onSelectBase={vi.fn()} />)

    await screen.findByText("Enterprise ATT&CK v15.1")
    const card = baseCards(container)[0]
    await user.click(within(card).getByRole("button", { name: "Baixar" }))

    // The descriptive, per-row error appears (ApiError.message).
    await screen.findByText("Falha na rede ao baixar.")

    // The base stays not-downloaded: no badge, "Baixar" still present, and the
    // list was NOT re-fetched after the failure.
    expect(within(card).queryByText("Já baixada")).toBeNull()
    expect(within(card).getByRole("button", { name: "Baixar" })).toBeInTheDocument()
    expect(getBasesMock).toHaveBeenCalledTimes(1)
  })
})
