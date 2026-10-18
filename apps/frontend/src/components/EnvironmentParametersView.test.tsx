import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { EnvironmentParametersView } from "@/components/EnvironmentParametersView"
import type { Stage4Response, EnvironmentParameters } from "@/lib/stages"

// Mock the stage-4 wrapper so no network is used; the view only reads
// `parameters` out of the Stage4Response.
vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return {
    ...actual,
    getStageOperationalization: vi.fn(),
  }
})

import { getStageOperationalization } from "@/lib/stages"

const getMock = vi.mocked(getStageOperationalization)

function makeParameters(
  overrides: Partial<EnvironmentParameters> = {},
): EnvironmentParameters {
  return {
    campaign_id: "shadowray",
    platform: "conteiner",
    origin: "curadoria_humana",
    hosts: [
      {
        label: "alvo-a",
        address: "10.0.0.5",
        container: "sticks-target-a",
        host_type: "linux",
        vulnerabilities: ["CVE-2023-48022", "ray-dashboard"],
      },
    ],
    services: [{ description: "Ray dashboard exposto", origin_technique: "T1190" }],
    network_context: ["rede interna attack-plane 10.0.0.0/24"],
    ...overrides,
  }
}

function makeResponse(parameters: EnvironmentParameters): Stage4Response {
  return {
    campaign_id: "shadowray",
    abilities: null,
    parameters,
    origin: parameters.origin,
    both_produced: true,
    states: { campaign_id: "shadowray", stages: [], fully_completed: false },
  }
}

describe("EnvironmentParametersView (Stage 4 — environment parameters, Req. 8.3, 8.4)", () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  it("renders target hosts with address, container, host type and vulnerabilities (Req. 8.3)", async () => {
    getMock.mockResolvedValue(makeResponse(makeParameters()))

    renderWithI18n(<EnvironmentParametersView campaignId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalledWith("shadowray"))

    expect(await screen.findByText("alvo-a")).toBeInTheDocument()
    expect(screen.getByText("10.0.0.5")).toBeInTheDocument()
    expect(screen.getByText("sticks-target-a")).toBeInTheDocument()
    expect(screen.getByText("linux")).toBeInTheDocument()
    expect(screen.getByText("CVE-2023-48022")).toBeInTheDocument()
    expect(screen.getByText("ray-dashboard")).toBeInTheDocument()
  })

  it("renders services/vulnerabilities with the originating technique (Req. 8.3)", async () => {
    getMock.mockResolvedValue(makeResponse(makeParameters()))

    renderWithI18n(<EnvironmentParametersView campaignId="shadowray" />)

    expect(await screen.findByText("Ray dashboard exposto")).toBeInTheDocument()
    expect(screen.getByText("T1190")).toBeInTheDocument()
  })

  it("renders the network_context notes and the platform badge (Req. 8.3, 8.4, 17.1)", async () => {
    getMock.mockResolvedValue(makeResponse(makeParameters()))

    const { container } = renderWithI18n(
      <EnvironmentParametersView campaignId="shadowray" />,
    )

    expect(
      await screen.findByText("rede interna attack-plane 10.0.0.0/24"),
    ).toBeInTheDocument()
    // The platform is a container (Req. 17.1) — exposed on a data attribute.
    expect(
      container.querySelector('[data-plataforma="conteiner"]'),
    ).not.toBeNull()
  })

  it("shows absence states when hosts/services/network_context are empty", async () => {
    getMock.mockResolvedValue(
      makeResponse(
        makeParameters({ hosts: [], services: [], network_context: [] }),
      ),
    )

    const { container } = renderWithI18n(
      <EnvironmentParametersView campaignId="shadowray" />,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    // Each section renders its own empty note (one per section).
    expect(
      container.querySelector('[data-section="hosts"]'),
    ).toHaveTextContent(/Nenhum host-alvo/)
    expect(
      container.querySelector('[data-section="servicos"]'),
    ).toHaveTextContent(/Nenhum serviço/)
    expect(
      container.querySelector('[data-section="network-context"]'),
    ).toHaveTextContent(/Nenhuma nota de rede/)
  })

  it("surfaces the pt-BR load error when the stage fails to load", async () => {
    getMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<EnvironmentParametersView campaignId="shadowray" />)

    await screen.findByText("Não foi possível carregar os dados deste estágio.")
  })

  it("falls back to the raw platform value for a future, untranslated platform (Req. 17.2)", async () => {
    getMock.mockResolvedValue(
      makeResponse(makeParameters({ platform: "maquina_virtual" })),
    )

    const { container } = renderWithI18n(
      <EnvironmentParametersView campaignId="shadowray" />,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    expect(
      container.querySelector('[data-plataforma="maquina_virtual"]'),
    ).not.toBeNull()
  })
})
