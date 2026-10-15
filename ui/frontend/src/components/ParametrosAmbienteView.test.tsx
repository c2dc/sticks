import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { ParametrosAmbienteView } from "@/components/ParametrosAmbienteView"
import type { Estagio4Response, ParametrosDeAmbiente } from "@/lib/estagios"

// Mock the stage-4 wrapper so no network is used; the view only reads
// `parametros` out of the Estagio4Response.
vi.mock("@/lib/estagios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/estagios")>()
  return {
    ...actual,
    getEstagioOperacionalizacao: vi.fn(),
  }
})

import { getEstagioOperacionalizacao } from "@/lib/estagios"

const getMock = vi.mocked(getEstagioOperacionalizacao)

function makeParametros(
  overrides: Partial<ParametrosDeAmbiente> = {},
): ParametrosDeAmbiente {
  return {
    caso_id: "shadowray",
    plataforma: "conteiner",
    origem: "curadoria_humana",
    hosts: [
      {
        rotulo: "alvo-a",
        endereco: "10.0.0.5",
        container: "sticks-target-a",
        host_type: "linux",
        vulnerabilidades: ["CVE-2023-48022", "ray-dashboard"],
      },
    ],
    servicos: [{ descricao: "Ray dashboard exposto", tecnica_origem: "T1190" }],
    network_context: ["rede interna attack-plane 10.0.0.0/24"],
    ...overrides,
  }
}

function makeResponse(parametros: ParametrosDeAmbiente): Estagio4Response {
  return {
    caso_id: "shadowray",
    abilities: null,
    parametros,
    origem: parametros.origem,
    ambos_produzidos: true,
    estados: { caso_id: "shadowray", estagios: [], concluido_total: false },
  }
}

describe("ParametrosAmbienteView (Estágio 4 — parâmetros de ambiente, Req. 8.3, 8.4)", () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  it("renders target hosts with address, container, host type and vulnerabilities (Req. 8.3)", async () => {
    getMock.mockResolvedValue(makeResponse(makeParametros()))

    renderWithI18n(<ParametrosAmbienteView casoId="shadowray" />)

    await waitFor(() => expect(getMock).toHaveBeenCalledWith("shadowray"))

    expect(await screen.findByText("alvo-a")).toBeInTheDocument()
    expect(screen.getByText("10.0.0.5")).toBeInTheDocument()
    expect(screen.getByText("sticks-target-a")).toBeInTheDocument()
    expect(screen.getByText("linux")).toBeInTheDocument()
    expect(screen.getByText("CVE-2023-48022")).toBeInTheDocument()
    expect(screen.getByText("ray-dashboard")).toBeInTheDocument()
  })

  it("renders services/vulnerabilities with the originating technique (Req. 8.3)", async () => {
    getMock.mockResolvedValue(makeResponse(makeParametros()))

    renderWithI18n(<ParametrosAmbienteView casoId="shadowray" />)

    expect(await screen.findByText("Ray dashboard exposto")).toBeInTheDocument()
    expect(screen.getByText("T1190")).toBeInTheDocument()
  })

  it("renders the network_context notes and the platform badge (Req. 8.3, 8.4, 17.1)", async () => {
    getMock.mockResolvedValue(makeResponse(makeParametros()))

    const { container } = renderWithI18n(
      <ParametrosAmbienteView casoId="shadowray" />,
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
        makeParametros({ hosts: [], servicos: [], network_context: [] }),
      ),
    )

    const { container } = renderWithI18n(
      <ParametrosAmbienteView casoId="shadowray" />,
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

    renderWithI18n(<ParametrosAmbienteView casoId="shadowray" />)

    await screen.findByText("Não foi possível carregar os dados deste estágio.")
  })

  it("falls back to the raw platform value for a future, untranslated platform (Req. 17.2)", async () => {
    getMock.mockResolvedValue(
      makeResponse(makeParametros({ plataforma: "maquina_virtual" })),
    )

    const { container } = renderWithI18n(
      <ParametrosAmbienteView casoId="shadowray" />,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    expect(
      container.querySelector('[data-plataforma="maquina_virtual"]'),
    ).not.toBeNull()
  })
})
