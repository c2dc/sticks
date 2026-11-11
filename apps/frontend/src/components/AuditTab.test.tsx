import { describe, expect, it } from "vitest"
import { within } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { AuditTab } from "@/components/AuditTab"
import type { AiIteration, AiStageOutput } from "@/lib/ai"

/**
 * AuditTab — the consolidated Audit view (task 9.5, Req. 1.1, 1.5). It lists the
 * WHOLE Audit_Trace at once, grouped by iteration → agent step, each agent step
 * reusing the Input → Processing → Output {@link AgentTracePanel}.
 *
 * These tests cover the task 9.8 bullet for the Audit tab: the whole trace is
 * grouped by iteration → agent step (vs. the per-iteration navigation of the
 * Run view), with the stage outputs ordered by the meaningful agent-step
 * sequence and every step carrying the three-part trace.
 */

function stageOutput(
  id: number,
  stage: string,
  overrides: Partial<AiStageOutput> = {},
): AiStageOutput {
  return {
    id,
    stage,
    origin: "ai_generated",
    payload: { ok: true },
    trace: { agent_input: { modeling: `in-${id}` }, processing: { reasoning: `proc-${id}` } },
    ...overrides,
  }
}

function iteration(
  id: number,
  index: number,
  stageOutputs: AiStageOutput[],
): AiIteration {
  return {
    id,
    index,
    refined_stage: null,
    notes: null,
    created_at: "2024-01-02T03:04:05",
    stage_outputs: stageOutputs,
    failing_agent: null,
    evaluation: null,
  }
}

describe("AuditTab — whole trace grouped by iteration → agent step (Req. 1.1, 1.5)", () => {
  it("renders the empty note when there are no iterations", () => {
    const { container } = renderWithI18n(<AuditTab iterations={[]} />)
    const audit = container.querySelector('[data-section="ai-run-audit"]') as HTMLElement
    expect(audit).not.toBeNull()
    expect(within(audit).getByText("Nenhum trace de auditoria disponível ainda.")).toBeInTheDocument()
  })

  it("groups the whole trace by iteration then by agent step", () => {
    const iterations: AiIteration[] = [
      iteration(100, 0, [
        stageOutput(1, "dag"),
        stageOutput(2, "operationalization"),
      ]),
      iteration(101, 1, [stageOutput(3, "environment")]),
    ]

    const { container } = renderWithI18n(<AuditTab iterations={iterations} />)

    // One group per iteration, with 1-based headers.
    const groups = Array.from(
      container.querySelectorAll('[data-section="ai-run-audit-iteration"]'),
    ) as HTMLElement[]
    expect(groups).toHaveLength(2)
    expect(within(groups[0]).getByText("Iteração 1")).toBeInTheDocument()
    expect(within(groups[1]).getByText("Iteração 2")).toBeInTheDocument()

    // The first iteration group holds two agent-step trace panels; the second one.
    expect(
      groups[0].querySelectorAll('[data-section="agent-trace-panel"]'),
    ).toHaveLength(2)
    expect(
      groups[1].querySelectorAll('[data-section="agent-trace-panel"]'),
    ).toHaveLength(1)

    // Each agent step carries the three-part Input/Processing/Output trace.
    const firstPanel = groups[0].querySelector(
      '[data-section="agent-trace-panel"]',
    ) as HTMLElement
    expect(firstPanel.querySelector('[data-part="input"]')).not.toBeNull()
    expect(firstPanel.querySelector('[data-part="processing"]')).not.toBeNull()
    expect(firstPanel.querySelector('[data-part="output"]')).not.toBeNull()
  })

  it("orders each iteration's agent steps by the meaningful agent-step sequence", () => {
    // Provide the stage outputs out of order; AuditTab must sort them.
    const iterations: AiIteration[] = [
      iteration(100, 0, [
        stageOutput(3, "environment"),
        stageOutput(1, "dag"),
        stageOutput(2, "operationalization"),
      ]),
    ]

    const { container } = renderWithI18n(<AuditTab iterations={iterations} />)

    const panels = Array.from(
      container.querySelectorAll('[data-section="agent-trace-panel"]'),
    ) as HTMLElement[]
    expect(panels.map((p) => p.getAttribute("data-stage"))).toEqual([
      "dag",
      "operationalization",
      "environment",
    ])
  })

  it("shows the no-steps note for an iteration with no agent steps", () => {
    const { container } = renderWithI18n(
      <AuditTab iterations={[iteration(100, 0, [])]} />,
    )
    const group = container.querySelector(
      '[data-section="ai-run-audit-iteration"]',
    ) as HTMLElement
    expect(within(group).getByText("Nenhum passo de agente nesta iteração.")).toBeInTheDocument()
  })
})
