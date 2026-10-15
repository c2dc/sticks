import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { EnvironmentStageView } from "@/components/EnvironmentStageView"
import type { CaseStageStates } from "@/lib/casos"
import type { ProvisionamentoResponse } from "@/lib/estagios"

// Mock the Stage-5 wrappers so no network/WS is used. The screen now also
// POSTs the idempotent provisioning on mount (`provisionarAmbiente`), so we mock
// it alongside the read-only `getEstagioAmbiente`.
vi.mock("@/lib/estagios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/estagios")>()
  return {
    ...actual,
    getEstagioAmbiente: vi.fn(),
    provisionarAmbiente: vi.fn(),
  }
})

// Stub MermaidDiagram so jsdom never imports the heavy `mermaid` bundle; the
// stub just exposes the definition it received for assertions.
vi.mock("@/components/MermaidDiagram", () => ({
  MermaidDiagram: ({ definition }: { definition: string }) => (
    <div data-testid="mermaid-stub">{definition}</div>
  ),
}))

// Stub the live-log console (opens a WebSocket) and the prerequisites checklist
// so this suite stays focused on the topology/isolation assertions without WS
// side effects. Their own suites (tasks 7.3 / 8.3) cover them directly.
vi.mock("@/components/ProvisioningConsole", () => ({
  ProvisioningConsole: () => <div data-testid="provisioning-console-stub" />,
}))
vi.mock("@/components/PrereqChecklist", () => ({
  PrereqChecklist: () => <div data-testid="prereq-checklist-stub" />,
}))

import { getEstagioAmbiente, provisionarAmbiente } from "@/lib/estagios"
import type { AmbienteProvisionado } from "@/lib/estagios"

const getEstagioAmbienteMock = vi.mocked(getEstagioAmbiente)
const provisionarAmbienteMock = vi.mocked(provisionarAmbiente)

function makeAmbiente(
  overrides: Partial<AmbienteProvisionado> = {},
): AmbienteProvisionado {
  return {
    caso_id: "c0026",
    plataforma: "conteiner",
    containers_alvo: ["c0026-victim"],
    compose_file: "cases/c0026/docker-compose.yml",
    saude: { aprovado: true, veredictos: [] },
    isolamento: { aprovado: true, veredictos: [] },
    abilities_carregadas: true,
    origem: "curadoria_humana",
    etapa_falha: null,
    mensagem_erro: null,
    ...overrides,
  }
}

function makeEstados(estagio4Concluido: boolean): CaseStageStates {
  return {
    caso_id: "c0026",
    estagios: [
      { estagio: 1, estado: "concluido", progresso: 100, bloqueado: false, iniciavel: true },
      { estagio: 2, estado: "concluido", progresso: 100, bloqueado: false, iniciavel: true },
      { estagio: 3, estado: "concluido", progresso: 100, bloqueado: false, iniciavel: true },
      {
        estagio: 4,
        estado: estagio4Concluido ? "concluido" : "em_andamento",
        progresso: estagio4Concluido ? 100 : 0,
        bloqueado: false,
        iniciavel: true,
      },
      {
        estagio: 5,
        estado: "nao_iniciado",
        progresso: 0,
        bloqueado: !estagio4Concluido,
        iniciavel: estagio4Concluido,
      },
    ],
    concluido_total: false,
  }
}

function makeResponse(
  overrides: Partial<ProvisionamentoResponse> = {},
): ProvisionamentoResponse {
  return {
    caso_id: "c0026",
    nome: "ShadowRay",
    isolado: true,
    isolamento_indisponivel: false,
    motivo: null,
    hosts: [
      { nome: "attacker-kali", container: "c0026-attacker", papel: "attacker", ips: ["192.168.20.30"] },
      { nome: "victim-01", container: "c0026-victim", papel: "target", ips: ["192.168.10.5"] },
    ],
    redes: [
      { nome: "ataque", subnet: "192.168.20.0/24", internal: true, hosts: ["attacker-kali"] },
      { nome: "alvo", subnet: "192.168.10.0/24", internal: true, hosts: ["victim-01"] },
    ],
    mermaid: "flowchart TD\n  attacker --> victim",
    compose_file: "cases/c0026/docker-compose.yml",
    estados: makeEstados(true),
    saude: { aprovado: true, veredictos: [] },
    logs: [],
    ...overrides,
  }
}

describe("EnvironmentStageView (Estágio 4 — Provisionamento do Ambiente, Req. 5.4–5.6)", () => {
  beforeEach(() => {
    getEstagioAmbienteMock.mockReset()
    provisionarAmbienteMock.mockReset()
    // Default: provisioning resolves a ready environment on mount so the screen
    // settles into its loaded state for the topology/isolation assertions.
    provisionarAmbienteMock.mockResolvedValue(makeAmbiente())
  })

  it("renders the contained topology for every isolation state (diagram, networks, hosts, compose)", async () => {
    getEstagioAmbienteMock.mockResolvedValue(makeResponse())

    renderWithI18n(<EnvironmentStageView casoId="c0026" />)

    await waitFor(() => expect(getEstagioAmbienteMock).toHaveBeenCalledWith("c0026"))

    // Mermaid definition from the backend renders.
    const stub = await screen.findByTestId("mermaid-stub")
    expect(stub).toHaveTextContent("flowchart TD")

    // Internal networks (nome + subnet).
    expect(screen.getByText("ataque")).toBeInTheDocument()
    expect(screen.getByText("192.168.20.0/24")).toBeInTheDocument()

    // Hosts (nome / container / IPs).
    expect(screen.getByText("attacker-kali")).toBeInTheDocument()
    expect(screen.getByText("c0026-victim")).toBeInTheDocument()
    expect(screen.getByText("192.168.10.5")).toBeInTheDocument()

    // Compose file.
    expect(screen.getByText(/docker-compose\.yml/)).toBeInTheDocument()
  })

  it("isolado=true → shows the 'isolated / ready' indicator (Stage 5 unlocked)", async () => {
    getEstagioAmbienteMock.mockResolvedValue(makeResponse())

    renderWithI18n(<EnvironmentStageView casoId="c0026" />)

    expect(await screen.findByText(/pronto para o Estágio 5/i)).toBeInTheDocument()
    // No blocking copy is shown when the environment is isolated.
    expect(screen.queryByText(/permanece bloqueado/i)).not.toBeInTheDocument()
  })

  it("isolado=false & verifiable → shows the motivo and makes clear Stage 5 is blocked", async () => {
    getEstagioAmbienteMock.mockResolvedValue(
      makeResponse({
        isolado: false,
        isolamento_indisponivel: false,
        motivo: "A rede 'alvo' tem rota para a internet (internal=false).",
        estados: makeEstados(false),
      }),
    )

    renderWithI18n(<EnvironmentStageView casoId="c0026" />)

    expect(await screen.findByText(/não isolado/i)).toBeInTheDocument()
    expect(
      screen.getByText("A rede 'alvo' tem rota para a internet (internal=false)."),
    ).toBeInTheDocument()
    expect(screen.getByText(/permanece bloqueado/i)).toBeInTheDocument()

    // The topology is still shown even though the stage is blocked.
    expect(screen.getByTestId("mermaid-stub")).toBeInTheDocument()
  })

  it("isolamento_indisponivel=true → fail-closed: not verifiable, Stage 5 blocked, topology still shown", async () => {
    getEstagioAmbienteMock.mockResolvedValue(
      makeResponse({
        isolado: false,
        isolamento_indisponivel: true,
        motivo: "O daemon Docker não respondeu; o isolamento não pôde ser verificado.",
        estados: makeEstados(false),
      }),
    )

    renderWithI18n(<EnvironmentStageView casoId="c0026" />)

    expect(await screen.findByText(/não verificável/i)).toBeInTheDocument()
    expect(screen.getByText(/fail-closed/i)).toBeInTheDocument()
    expect(
      screen.getByText("O daemon Docker não respondeu; o isolamento não pôde ser verificado."),
    ).toBeInTheDocument()
    expect(screen.getByText(/permanece bloqueado/i)).toBeInTheDocument()

    // Fail-closed still renders the topology.
    expect(screen.getByTestId("mermaid-stub")).toBeInTheDocument()
  })

  it("surfaces the pt-BR load error when the Stage-4 payload fails to load", async () => {
    getEstagioAmbienteMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<EnvironmentStageView casoId="c0026" />)

    await screen.findByText("Não foi possível carregar os dados deste estágio.")
  })
})
