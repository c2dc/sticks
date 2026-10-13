import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import {
  CampaignActorSelector,
  filtrarCampanhas,
  isCurada,
} from "@/components/CampaignActorSelector"
import type { AtorStix, CampanhaStix } from "@/lib/stix"

// Mock the STIX API wrappers so no network is used.
vi.mock("@/lib/stix", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stix")>()
  return {
    ...actual,
    getCampanhas: vi.fn(),
    getAtores: vi.fn(),
  }
})

import { getCampanhas, getAtores } from "@/lib/stix"

const getCampanhasMock = vi.mocked(getCampanhas)
const getAtoresMock = vi.mocked(getAtores)

const campanhaCurada: CampanhaStix = {
  stix_id: "campaign--curada-1",
  nome: "C0010 ShadowRay",
  descricao: "Campanha curada.",
  curada: true,
  slug_curado: "shadowray",
}

const campanhaNaoCurada: CampanhaStix = {
  stix_id: "campaign--livre-1",
  nome: "Operação Livre",
  descricao: null,
  curada: false,
  slug_curado: null,
}

function makeCampanhas(): CampanhaStix[] {
  return [campanhaCurada, campanhaNaoCurada]
}

function makeAtores(): AtorStix[] {
  return [
    {
      stix_id: "intrusion-set--apt1",
      nome: "APT1",
      campanhas: [campanhaCurada],
    },
    {
      stix_id: "sem-ator",
      nome: "sem-ator",
      campanhas: [campanhaNaoCurada],
    },
  ]
}

describe("CampaignActorSelector (Req. 4 — campaign/actor selection)", () => {
  beforeEach(() => {
    getCampanhasMock.mockReset()
    getAtoresMock.mockReset()
    getCampanhasMock.mockResolvedValue(makeCampanhas())
    getAtoresMock.mockResolvedValue(makeAtores())
  })

  it("defaults to the campaign view and shows the 'Já curada' badge only on curated campaigns (Req. 4.2, 4.5)", async () => {
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")
    expect(screen.getByText("Operação Livre")).toBeInTheDocument()

    // Only the curated campaign carries the "Já curada" badge.
    const badges = screen.getAllByText("Já curada")
    expect(badges).toHaveLength(1)
  })

  it("toggles between campaign and actor views (Req. 4.1)", async () => {
    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")

    // Switch to the actor view: actor groups appear.
    await user.click(screen.getByRole("tab", { name: "Por ator de ameaça" }))

    expect(await screen.findByText("APT1")).toBeInTheDocument()
    // The synthetic "sem-ator" group renders with its localized label.
    expect(screen.getByText("Sem ator associado")).toBeInTheDocument()
    expect(getAtoresMock).toHaveBeenCalledWith("14.0")
  })

  it("calls onSelecionarCampanha with {stix_id, nome, slug_curado} when a campaign is clicked (Req. 4.4)", async () => {
    const onSelecionarCampanha = vi.fn()
    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={onSelecionarCampanha} />,
    )

    await screen.findByText("C0010 ShadowRay")
    await user.click(screen.getByText("C0010 ShadowRay"))

    expect(onSelecionarCampanha).toHaveBeenCalledWith({
      stix_id: "campaign--curada-1",
      nome: "C0010 ShadowRay",
      slug_curado: "shadowray",
    })
  })

  it("nests campaigns under their actor, including the 'sem-ator' group (Req. 4.3)", async () => {
    const onSelecionarCampanha = vi.fn()
    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={onSelecionarCampanha} />,
    )

    await screen.findByText("C0010 ShadowRay")
    await user.click(screen.getByRole("tab", { name: "Por ator de ameaça" }))

    // APT1's actor group (its <li>) nests its curated campaign.
    const apt1Card = (await screen.findByText("APT1")).closest("li") as HTMLElement
    expect(within(apt1Card).getByText("C0010 ShadowRay")).toBeInTheDocument()

    // The synthetic group nests the non-curated campaign.
    const semAtorCard = screen.getByText("Sem ator associado").closest("li") as HTMLElement
    expect(within(semAtorCard).getByText("Operação Livre")).toBeInTheDocument()

    // Selecting a nested campaign still forwards its identity (Req. 4.4).
    await user.click(within(apt1Card).getByText("C0010 ShadowRay"))
    expect(onSelecionarCampanha).toHaveBeenCalledWith({
      stix_id: "campaign--curada-1",
      nome: "C0010 ShadowRay",
      slug_curado: "shadowray",
    })
  })

  // --- search + "curated only" filter -------------------------------------

  it("filters campaigns by the search term (name/description), case/accent-insensitive", async () => {
    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")
    expect(screen.getByText("Operação Livre")).toBeInTheDocument()

    // Typing a term that matches only the curated campaign hides the other.
    // "shadow" (lowercase) matches "ShadowRay" case-insensitively.
    await user.type(screen.getByRole("searchbox"), "shadow")

    expect(screen.getByText("C0010 ShadowRay")).toBeInTheDocument()
    expect(screen.queryByText("Operação Livre")).not.toBeInTheDocument()

    // Accent-insensitive: "operacao" (no cedilla/accent) matches "Operação".
    await user.clear(screen.getByRole("searchbox"))
    await user.type(screen.getByRole("searchbox"), "operacao")
    expect(screen.getByText("Operação Livre")).toBeInTheDocument()
    expect(screen.queryByText("C0010 ShadowRay")).not.toBeInTheDocument()
  })

  it("the 'curated only' filter keeps only curated campaigns", async () => {
    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")

    await user.click(screen.getByRole("checkbox", { name: "Só curadas" }))

    expect(screen.getByText("C0010 ShadowRay")).toBeInTheDocument()
    expect(screen.queryByText("Operação Livre")).not.toBeInTheDocument()
  })

  it("shows a 'no results' message when the search matches nothing", async () => {
    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")
    await user.type(screen.getByRole("searchbox"), "zzz-nao-existe")

    expect(
      screen.getByText("Nenhuma campanha corresponde à busca."),
    ).toBeInTheDocument()
    expect(screen.queryByText("C0010 ShadowRay")).not.toBeInTheDocument()
  })

  it("the clear-search control resets the query", async () => {
    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")
    await user.type(screen.getByRole("searchbox"), "shadow")
    expect(screen.queryByText("Operação Livre")).not.toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Limpar busca" }))
    expect(screen.getByText("Operação Livre")).toBeInTheDocument()
  })

  it("counts ACTORS (not campaigns) and excludes the synthetic 'sem ator' group", async () => {
    // Three groups: two real intrusion-sets + the synthetic "sem ator" bucket.
    // The counter reports REAL ACTORS only, so it shows "2 de 2 atores" (the
    // synthetic group is not a threat actor), even though it still renders in
    // the list.
    getAtoresMock.mockResolvedValue([
      { stix_id: "intrusion-set--apt1", nome: "APT1", campanhas: [campanhaCurada] },
      { stix_id: "intrusion-set--apt2", nome: "APT2", campanhas: [campanhaCurada] },
      { stix_id: "sem-ator", nome: "sem-ator", campanhas: [campanhaNaoCurada] },
    ])

    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")
    await user.click(screen.getByRole("tab", { name: "Por ator de ameaça" }))
    await screen.findByText("APT1")

    expect(screen.getByText("2 de 2 atores")).toBeInTheDocument()
    // The synthetic bucket is still visible in the list (just not counted).
    expect(screen.getByText("Sem ator associado")).toBeInTheDocument()
  })

  it("the actor counter shrinks to the matching actors when searching", async () => {
    getAtoresMock.mockResolvedValue([
      { stix_id: "intrusion-set--apt1", nome: "APT1", campanhas: [campanhaCurada] },
      { stix_id: "intrusion-set--apt2", nome: "APT2", campanhas: [campanhaCurada] },
      { stix_id: "sem-ator", nome: "sem-ator", campanhas: [campanhaNaoCurada] },
    ])

    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")
    await user.click(screen.getByRole("tab", { name: "Por ator de ameaça" }))
    await screen.findByText("APT1")

    // Searching for the curated campaign keeps the two actors that reference it
    // and drops the synthetic group. The synthetic bucket never counts, so the
    // total is the 2 real actors => "2 de 2 atores".
    await user.type(screen.getByRole("searchbox"), "shadow")
    expect(screen.getByText("2 de 2 atores")).toBeInTheDocument()
  })

  it("the search/filter applies to the actor view too, hiding actors left empty", async () => {
    const user = userEvent.setup()
    renderWithI18n(
      <CampaignActorSelector versao="14.0" onSelecionarCampanha={vi.fn()} />,
    )

    await screen.findByText("C0010 ShadowRay")
    await user.click(screen.getByRole("tab", { name: "Por ator de ameaça" }))
    await screen.findByText("APT1")

    // Searching for the curated campaign keeps APT1 and drops the sem-ator
    // group (its only campaign no longer matches).
    await user.type(screen.getByRole("searchbox"), "shadow")
    expect(screen.getByText("APT1")).toBeInTheDocument()
    expect(screen.queryByText("Sem ator associado")).not.toBeInTheDocument()
  })
})

// --- pure helpers ---------------------------------------------------------

describe("filtrarCampanhas / isCurada (pure)", () => {
  const campanhas: CampanhaStix[] = [
    {
      stix_id: "a",
      nome: "ShadowRay",
      descricao: "Uses Ray clusters",
      curada: true,
      slug_curado: "shadowray",
    },
    {
      stix_id: "b",
      nome: "Operação Livre",
      descricao: null,
      curada: false,
      slug_curado: null,
    },
    {
      stix_id: "c",
      nome: "Curada sem slug",
      descricao: null,
      curada: true,
      slug_curado: null,
    },
  ]

  it("isCurada requires both the flag and a slug", () => {
    expect(isCurada(campanhas[0])).toBe(true)
    expect(isCurada(campanhas[1])).toBe(false)
    // curada=true but no slug_curado => not actually curated.
    expect(isCurada(campanhas[2])).toBe(false)
  })

  it("empty search returns everything (optionally curated-only)", () => {
    expect(filtrarCampanhas(campanhas, "", false)).toHaveLength(3)
    expect(filtrarCampanhas(campanhas, "", true)).toEqual([campanhas[0]])
  })

  it("matches name and description, accent/case-insensitive", () => {
    expect(filtrarCampanhas(campanhas, "shadow", false)).toEqual([campanhas[0]])
    expect(filtrarCampanhas(campanhas, "ray clusters", false)).toEqual([
      campanhas[0],
    ])
    expect(filtrarCampanhas(campanhas, "operacao", false)).toEqual([campanhas[1]])
  })

  it("combines search with the curated-only filter", () => {
    // "sem slug" matches campaign c by name, but it is not curated → excluded.
    expect(filtrarCampanhas(campanhas, "sem slug", true)).toEqual([])
  })
})
