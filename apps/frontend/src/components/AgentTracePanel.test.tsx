import { describe, expect, it } from "vitest"
import { screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { AgentTracePanel } from "@/components/AgentTracePanel"
import type { AiStageOutput } from "@/lib/ai"

/**
 * AgentTracePanel — the three-part Input → Processing → Output view of a single
 * agent step within one iteration of an AI Run (task 9.4/9.5, Req. 1.1, 1.5,
 * 1.6, 6.2, 6.3).
 *
 * These tests cover the task 9.8 bullets scoped to this component:
 * - the three distinctly-labeled parts (Input / Processing / Output) render
 *   (Req. 1.1, 1.5);
 * - the raw LLM prompt/response "show details" disclosure is CLOSED by default
 *   and only reveals the raw text on expand (Req. 6.2, 6.3);
 * - a processing element returned as the `"not_available"` sentinel renders an
 *   explicit "not available" marker rather than being dropped (Req. 1.6);
 * - the Environment agent step surfaces the fixed-vs-agent-decided split and
 *   BOTH gate verdicts (ResourceGate + isolation) via EnvironmentStepDetails.
 */

const NOT_AVAILABLE = "not_available"

function makeStageOutput(overrides: Partial<AiStageOutput> = {}): AiStageOutput {
  return {
    id: 1,
    stage: "flow",
    origin: "ai_generated",
    payload: { nodes: ["T1190"] },
    trace: {
      agent_input: { modeling: "entrada de modelagem" },
      processing: {
        reasoning: "raciocínio do agente",
        llm_prompt: "PROMPT BRUTO DO LLM",
        llm_response: "RESPOSTA BRUTA DO LLM",
      },
    },
    ...overrides,
  }
}

describe("AgentTracePanel — Input/Processing/Output (Req. 1.1, 1.5, 1.6, 6.2, 6.3)", () => {
  it("renders the three distinctly-labeled parts: Input, Processing, Output", () => {
    const { container } = renderWithI18n(
      <AgentTracePanel stageName="Agente de geração do attack flow" stageOutput={makeStageOutput()} />,
    )

    const panel = container.querySelector(
      '[data-section="agent-trace-panel"]',
    ) as HTMLElement
    expect(panel).not.toBeNull()

    // Each of the three parts is present and distinctly identified.
    expect(panel.querySelector('[data-part="input"]')).not.toBeNull()
    expect(panel.querySelector('[data-part="processing"]')).not.toBeNull()
    expect(panel.querySelector('[data-part="output"]')).not.toBeNull()

    // ...with their pt-BR source-of-truth labels.
    expect(within(panel).getByText("Entrada")).toBeInTheDocument()
    expect(within(panel).getByText("Processamento")).toBeInTheDocument()
    expect(within(panel).getByText("Saída")).toBeInTheDocument()

    // The stage name and the AI-generated origin stamp are shown.
    expect(within(panel).getByText("Agente de geração do attack flow")).toBeInTheDocument()
    expect(within(panel).getByText("Gerado por IA")).toBeInTheDocument()

    // Input and processing summary content is rendered (not behind a disclosure).
    expect(within(panel).getByText("entrada de modelagem")).toBeInTheDocument()
    expect(within(panel).getByText("raciocínio do agente")).toBeInTheDocument()
  })

  it("keeps the raw LLM 'show details' disclosure CLOSED by default and reveals it on expand (Req. 6.2, 6.3)", async () => {
    const { container } = renderWithI18n(
      <AgentTracePanel stageName="Agente de geração do attack flow" stageOutput={makeStageOutput()} />,
    )

    const details = container.querySelector(
      '[data-part="raw-details"]',
    ) as HTMLDetailsElement
    expect(details).not.toBeNull()
    // Closed by default — the raw prompt/response are not revealed.
    expect(details.open).toBe(false)

    // The disclosure summary carries the localized "show details" affordance.
    expect(within(details).getByText("Ver detalhes")).toBeInTheDocument()

    // Expanding the disclosure reveals the raw LLM prompt and response.
    await userEvent.click(within(details).getByText("Ver detalhes"))
    expect(details.open).toBe(true)
    expect(within(details).getByText("PROMPT BRUTO DO LLM")).toBeInTheDocument()
    expect(within(details).getByText("RESPOSTA BRUTA DO LLM")).toBeInTheDocument()
  })

  it("renders an explicit 'not available' marker for a sentinel processing element (Req. 1.6)", () => {
    const { container } = renderWithI18n(
      <AgentTracePanel
        stageName="Agente de geração do attack flow"
        stageOutput={makeStageOutput({
          trace: {
            agent_input: { modeling: "entrada" },
            processing: {
              reasoning: NOT_AVAILABLE,
              llm_prompt: NOT_AVAILABLE,
              llm_response: "resposta",
            },
          },
        })}
      />,
    )

    // The sentinel renders an explicit, identifiable "not available" marker
    // rather than being silently dropped.
    const markers = container.querySelectorAll(
      '[data-section="agent-trace-not-available"]',
    )
    expect(markers.length).toBeGreaterThan(0)
    expect(screen.getAllByText("não disponível").length).toBeGreaterThan(0)
  })

  it("renders the 'not available' output marker when the stage produced no payload", () => {
    const { container } = renderWithI18n(
      <AgentTracePanel
        stageName="Agente de geração do attack flow"
        stageOutput={makeStageOutput({ payload: {} })}
      />,
    )

    const output = container.querySelector('[data-part="output"]') as HTMLElement
    expect(
      output.querySelector('[data-section="agent-trace-not-available"]'),
    ).not.toBeNull()
  })

  it("surfaces the Environment agent fixed-vs-agent-decided split and BOTH gate verdicts", () => {
    const envStageOutput = makeStageOutput({
      id: 5,
      stage: "environment",
      payload: { provisioned: true },
      trace: {
        agent_input: { attack_context: "ATT&CK" },
        processing: {
          fixed_infrastructure: {
            execution_engine: "Caldera",
            isolable_network_structure: {
              internal: true,
              platform: "container",
              network_context: ["sem rota ao host"],
            },
            resource_limits: { max_containers: 10, max_ram_gb: 20 },
          },
          agent_decided: {
            attacker_node: "attacker-1",
            targets: [
              { system: "web-01", vulnerabilities: ["CVE-2024-0001"] },
            ],
          },
          gates: {
            resource_gate: { approved: true, reasons: [] },
            isolation: {
              approved: false,
              verdicts: [
                {
                  container: "web-01",
                  isolated: false,
                  reasons: ["bridge externa detectada"],
                  external_networks: ["bridge"],
                },
              ],
            },
          },
          substages: {
            build_online_ok: true,
            egress_closed: true,
            egress_lost_verified: false,
            offline_applied: false,
            egress_verdicts: [],
          },
          llm_prompt: "prompt",
          llm_response: "response",
        },
      },
    })

    const { container } = renderWithI18n(
      <AgentTracePanel stageName="Agente de ambiente" stageOutput={envStageOutput} />,
    )

    const details = container.querySelector(
      '[data-section="environment-step-details"]',
    ) as HTMLElement
    expect(details).not.toBeNull()

    // Fixed-vs-agent-decided split: both halves render.
    const fixed = details.querySelector(
      '[data-section="environment-fixed-infrastructure"]',
    ) as HTMLElement
    const decided = details.querySelector(
      '[data-section="environment-agent-decided"]',
    ) as HTMLElement
    expect(fixed).not.toBeNull()
    expect(decided).not.toBeNull()
    expect(within(fixed).getByText("Caldera")).toBeInTheDocument()
    expect(within(decided).getByText("attacker-1")).toBeInTheDocument()
    expect(within(decided).getByText("web-01")).toBeInTheDocument()

    // BOTH gate verdicts: ResourceGate (approved) + isolation (rejected).
    const resourceGate = details.querySelector(
      '[data-section="environment-gate-resource"]',
    ) as HTMLElement
    const isolation = details.querySelector(
      '[data-section="environment-gate-isolation"]',
    ) as HTMLElement
    expect(resourceGate).not.toBeNull()
    expect(isolation).not.toBeNull()
    expect(within(resourceGate).getByText("Aprovado")).toBeInTheDocument()
    expect(within(isolation).getByText("Reprovado")).toBeInTheDocument()
    expect(within(isolation).getByText("Não isolado")).toBeInTheDocument()
  })
})
