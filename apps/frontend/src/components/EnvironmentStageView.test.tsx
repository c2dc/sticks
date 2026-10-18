import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { EnvironmentStageView } from "@/components/EnvironmentStageView"
import type { CaseStageStates } from "@/lib/campaigns"
import type { ProvisioningResponse } from "@/lib/stages"

// Mock the Stage-5 wrappers so no network/WS is used. The screen now also
// POSTs the idempotent provisioning on mount (`provisionEnvironment`), so we mock
// it alongside the read-only `getStageEnvironment`.
vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return {
    ...actual,
    getStageEnvironment: vi.fn(),
    provisionEnvironment: vi.fn(),
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

import { getStageEnvironment, provisionEnvironment } from "@/lib/stages"
import type { ProvisionedEnvironment } from "@/lib/stages"

const getStageEnvironmentMock = vi.mocked(getStageEnvironment)
const provisionEnvironmentMock = vi.mocked(provisionEnvironment)

function makeEnvironment(
  overrides: Partial<ProvisionedEnvironment> = {},
): ProvisionedEnvironment {
  return {
    campaign_id: "c0026",
    platform: "conteiner",
    target_containers: ["c0026-victim"],
    compose_file: "cases/c0026/docker-compose.yml",
    health: { approved: true, verdicts: [] },
    isolation: { approved: true, verdicts: [] },
    loaded_abilities: true,
    origin: "curadoria_humana",
    failed_step: null,
    error_message: null,
    ...overrides,
  }
}

function makeStates(stage4Completed: boolean): CaseStageStates {
  return {
    campaign_id: "c0026",
    stages: [
      { stage: 1, state: "concluido", progress: 100, blocked: false, startable: true },
      { stage: 2, state: "concluido", progress: 100, blocked: false, startable: true },
      { stage: 3, state: "concluido", progress: 100, blocked: false, startable: true },
      {
        stage: 4,
        state: stage4Completed ? "concluido" : "em_andamento",
        progress: stage4Completed ? 100 : 0,
        blocked: false,
        startable: true,
      },
      {
        stage: 5,
        state: "nao_iniciado",
        progress: 0,
        blocked: !stage4Completed,
        startable: stage4Completed,
      },
    ],
    fully_completed: false,
  }
}

function makeResponse(
  overrides: Partial<ProvisioningResponse> = {},
): ProvisioningResponse {
  return {
    campaign_id: "c0026",
    name: "ShadowRay",
    isolated: true,
    isolation_unavailable: false,
    reason: null,
    hosts: [
      { name: "attacker-kali", container: "c0026-attacker", role: "attacker", ips: ["192.168.20.30"] },
      { name: "victim-01", container: "c0026-victim", role: "target", ips: ["192.168.10.5"] },
    ],
    networks: [
      { name: "ataque", subnet: "192.168.20.0/24", internal: true, hosts: ["attacker-kali"] },
      { name: "alvo", subnet: "192.168.10.0/24", internal: true, hosts: ["victim-01"] },
    ],
    mermaid: "flowchart TD\n  attacker --> victim",
    compose_file: "cases/c0026/docker-compose.yml",
    states: makeStates(true),
    health: { approved: true, verdicts: [] },
    logs: [],
    ...overrides,
  }
}

describe("EnvironmentStageView (Stage 4 — Environment Provisioning, Req. 5.4–5.6)", () => {
  beforeEach(() => {
    getStageEnvironmentMock.mockReset()
    provisionEnvironmentMock.mockReset()
    // Default: provisioning resolves a ready environment on mount so the screen
    // settles into its loaded state for the topology/isolation assertions.
    provisionEnvironmentMock.mockResolvedValue(makeEnvironment())
  })

  it("renders the contained topology for every isolation state (diagram, networks, hosts, compose)", async () => {
    getStageEnvironmentMock.mockResolvedValue(makeResponse())

    renderWithI18n(<EnvironmentStageView campaignId="c0026" />)

    await waitFor(() => expect(getStageEnvironmentMock).toHaveBeenCalledWith("c0026"))

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
    getStageEnvironmentMock.mockResolvedValue(makeResponse())

    renderWithI18n(<EnvironmentStageView campaignId="c0026" />)

    expect(await screen.findByText(/pronto para o Estágio 5/i)).toBeInTheDocument()
    // No blocking copy is shown when the environment is isolated.
    expect(screen.queryByText(/permanece bloqueado/i)).not.toBeInTheDocument()
  })

  it("isolated=false & verifiable → shows the reason and makes clear Stage 5 is blocked", async () => {
    getStageEnvironmentMock.mockResolvedValue(
      makeResponse({
        isolated: false,
        isolation_unavailable: false,
        reason: "A rede 'alvo' tem rota para a internet (internal=false).",
        states: makeStates(false),
      }),
    )

    renderWithI18n(<EnvironmentStageView campaignId="c0026" />)

    expect(await screen.findByText(/não isolado/i)).toBeInTheDocument()
    expect(
      screen.getByText("A rede 'alvo' tem rota para a internet (internal=false)."),
    ).toBeInTheDocument()
    expect(screen.getByText(/permanece bloqueado/i)).toBeInTheDocument()

    // The topology is still shown even though the stage is blocked.
    expect(screen.getByTestId("mermaid-stub")).toBeInTheDocument()
  })

  it("isolamento_indisponivel=true → fail-closed: not verifiable, Stage 5 blocked, topology still shown", async () => {
    getStageEnvironmentMock.mockResolvedValue(
      makeResponse({
        isolated: false,
        isolation_unavailable: true,
        reason: "O daemon Docker não respondeu; o isolamento não pôde ser verificado.",
        states: makeStates(false),
      }),
    )

    renderWithI18n(<EnvironmentStageView campaignId="c0026" />)

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
    getStageEnvironmentMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<EnvironmentStageView campaignId="c0026" />)

    await screen.findByText("Não foi possível carregar os dados deste estágio.")
  })
})
