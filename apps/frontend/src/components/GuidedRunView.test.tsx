import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { GuidedRunView } from "@/components/GuidedRunView"
import type {
  AiEvaluation,
  AiIteration,
  AiRun,
  AiRunDetail,
  AiStageOutput,
} from "@/lib/ai"

/**
 * Deterministic tests for the Guided_Run_View refactor (task 7): the robot
 * reveals the semantic, number-free phases `dag → operationalization →
 * environment → execution → evaluation` one at a time, and `evaluation` is its
 * OWN revealed item carrying the evaluator verdict, distinct from the
 * `execution` step that precedes it (Req. 7.1, 7.3, 7.4, 2.5).
 *
 * The backend run is mocked so a single terminal read drives the whole
 * conversation; no network and no polling delay are exercised.
 */

vi.mock("@/lib/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/ai")>()
  return { ...actual, startAiRun: vi.fn(), getAiRun: vi.fn() }
})

import { getAiRun, startAiRun } from "@/lib/ai"

const startAiRunMock = vi.mocked(startAiRun)
const getAiRunMock = vi.mocked(getAiRun)

/** A minimal Agent_Output row for a curation phase (dag/op/environment). */
function stageOutput(stage: string): AiStageOutput {
  return {
    id: stage.length,
    stage,
    origin: "ai_generated",
    payload: {},
    trace: { agent_input: {}, processing: {} },
  }
}

/** The evaluator verdict for the terminal iteration. */
const EVALUATION: AiEvaluation = {
  id: 1,
  report_id: 7,
  success_rate: 1,
  procedural_coverage: 1,
  verdict_success: true,
  decision: "finish",
  rationale: "All techniques executed as planned.",
}

function terminalDetail(): AiRunDetail {
  const run: AiRun = {
    id: 42,
    campaign_id: "shadowray",
    status: "completed",
    thread_id: "t-42",
    max_iterations: 3,
    iterations_used: 1,
    final_verdict_success: true,
    error: null,
  }
  const iteration: AiIteration = {
    id: 1,
    index: 0,
    refined_stage: null,
    notes: null,
    created_at: "2024-01-01T00:00:00",
    stage_outputs: [
      stageOutput("dag"),
      stageOutput("operationalization"),
      stageOutput("environment"),
    ],
    failing_agent: null,
    evaluation: EVALUATION,
  }
  return { run, iterations: [iteration] }
}

describe("GuidedRunView — semantic phases with evaluation as its own item", () => {
  beforeEach(() => {
    startAiRunMock.mockReset()
    getAiRunMock.mockReset()
    const detail = terminalDetail()
    startAiRunMock.mockResolvedValue(detail.run)
    getAiRunMock.mockResolvedValue(detail)
  })

  it("reveals execution and evaluation as two distinct ordered items, with the verdict only in evaluation", async () => {
    const user = userEvent.setup()
    renderWithI18n(<GuidedRunView campaignId="shadowray" />)

    // Start the run; the robot reaches the first (DAG) phase.
    await user.click(await screen.findByText("Começar a emulação"))

    // Walk the three curation phases by approving each "proceed" prompt:
    // dag → operationalization → environment → (reveals) execution.
    for (let i = 0; i < 3; i++) {
      const proceed = await screen.findByText("Pode prosseguir")
      await user.click(proceed)
    }

    // Execution is revealed as its OWN item, carrying no verdict.
    const execution = await screen.findByText(
      /A emulação ofensiva rodou contra os alvos/i,
    )
    expect(execution).toBeInTheDocument()
    expect(document.querySelector('[data-section="guided-execution"]')).not.toBeNull()
    // The verdict artifact is NOT part of the execution step yet.
    expect(document.querySelector('[data-section="guided-verdict"]')).toBeNull()

    // Approving execution reveals the distinct evaluation phase with the verdict.
    await user.click(await screen.findByText("Pode prosseguir"))

    await waitFor(() => {
      expect(
        document.querySelector('[data-section="guided-verdict"]'),
      ).not.toBeNull()
    })
    // The verdict (Aprovado) and the evaluator rationale live in evaluation.
    expect(screen.getByText("Aprovado")).toBeInTheDocument()
    expect(
      screen.getByText("All techniques executed as planned."),
    ).toBeInTheDocument()
  })
})
