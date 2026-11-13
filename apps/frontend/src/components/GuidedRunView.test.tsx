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
 * Deterministic tests for the Guided_Run_View: the robot opens on the
 * operationalization listing and starts the AI run only after the Researcher
 * reviews it, then reveals the semantic, number-free phases `flow →
 * environment → execution → evaluation` one at a time. `evaluation` is its OWN
 * revealed item carrying the evaluator verdict, distinct from the `execution`
 * step that precedes it (Req. 2.2, 5.7, 6.1, 2.5).
 *
 * The backend run and the two read-only phase views are mocked so a single
 * terminal read drives the whole conversation; no network and no polling delay
 * are exercised.
 */

vi.mock("@/components/OperationalizationScreen", () => ({
  OperationalizationScreen: ({
    version,
    campaignKey,
    campaignName,
  }: {
    version: string
    campaignKey: string
    campaignName?: string
  }) => (
    <div data-testid="operationalization-stub" data-campaign-name={campaignName}>
      {`${campaignKey}@${version}`}
    </div>
  ),
}))

vi.mock("@/components/FlowPhaseView", () => ({
  FlowPhaseView: ({ campaign }: { campaign?: string }) => (
    <div data-testid="flow-phase-stub">{campaign}</div>
  ),
}))

vi.mock("@/lib/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/ai")>()
  return { ...actual, startAiRun: vi.fn(), getAiRun: vi.fn() }
})

import { getAiRun, startAiRun } from "@/lib/ai"

const startAiRunMock = vi.mocked(startAiRun)
const getAiRunMock = vi.mocked(getAiRun)

/** A minimal Agent_Output row for a curation phase (flow/op/environment). */
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
      stageOutput("operationalization"),
      stageOutput("flow"),
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

  it("opens on the operationalization listing and starts the run only after the user reviews it", async () => {
    const user = userEvent.setup()
    renderWithI18n(<GuidedRunView campaignId="shadowray" version="18.0" />)

    // The first screen is the operationalization listing, with no run started.
    expect(await screen.findByTestId("operationalization-stub")).toHaveTextContent(
      "shadowray@18.0",
    )
    expect(startAiRunMock).not.toHaveBeenCalled()
    expect(screen.queryByTestId("flow-phase-stub")).toBeNull()

    // Reviewing it and starting the run reveals the attack flow phase.
    await user.click(await screen.findByText("Começar a emulação"))
    expect(startAiRunMock).toHaveBeenCalledWith("shadowray")
    expect(await screen.findByTestId("flow-phase-stub")).toHaveTextContent("shadowray")
  })

  it.each([
    ["the provided name", "Outer Space"],
    ["the humanized key when the name is absent", undefined],
  ])("shows the campaign name in every orchestrator message using %s", async (_label, name) => {
    const user = userEvent.setup()
    const { container } = renderWithI18n(
      <GuidedRunView campaignId="outer_space" campaignName={name} version="18.0" />,
    )

    // The operationalization header receives the readable name.
    expect(await screen.findByTestId("operationalization-stub")).toHaveAttribute(
      "data-campaign-name",
      "Outer Space",
    )

    await user.click(await screen.findByText("Começar a emulação"))
    for (let i = 0; i < 3; i++) {
      await user.click(await screen.findByText("Pode prosseguir"))
    }
    await waitFor(() => {
      expect(document.querySelector('[data-section="guided-verdict"]')).not.toBeNull()
    })

    // Operationalization, flow, environment, execution and evaluation speak the name.
    const text = container.textContent ?? ""
    expect(text.match(/campanha Outer Space/g)).toHaveLength(5)

    // The slug never shows in the conversation (stubs echo raw ids on purpose).
    const clone = container.cloneNode(true) as HTMLElement
    clone.querySelectorAll("[data-testid$='-stub']").forEach((node) => node.remove())
    expect(clone.textContent).not.toContain("outer_space")
  })

  it("renders the phases in the order operationalization → flow → execution → evaluation", async () => {
    const user = userEvent.setup()
    renderWithI18n(<GuidedRunView campaignId="shadowray" version="18.0" />)

    await user.click(await screen.findByText("Começar a emulação"))
    for (let i = 0; i < 3; i++) {
      await user.click(await screen.findByText("Pode prosseguir"))
    }
    await waitFor(() => {
      expect(document.querySelector('[data-section="guided-verdict"]')).not.toBeNull()
    })

    const nodes = [
      screen.getByTestId("operationalization-stub"),
      screen.getByTestId("flow-phase-stub"),
      document.querySelector('[data-section="guided-execution"]') as Element,
      document.querySelector('[data-section="guided-verdict"]') as Element,
    ]
    for (let i = 0; i < nodes.length - 1; i++) {
      expect(
        nodes[i].compareDocumentPosition(nodes[i + 1]) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy()
    }
  })

  it("reveals execution and evaluation as two distinct ordered items, with the verdict only in evaluation", async () => {
    const user = userEvent.setup()
    renderWithI18n(<GuidedRunView campaignId="shadowray" version="18.0" />)

    // Start the run; the robot reaches the first agent phase (flow).
    await user.click(await screen.findByText("Começar a emulação"))

    // Walk the two curation phases by approving each "proceed" prompt:
    // flow → environment → (reveals) execution.
    for (let i = 0; i < 2; i++) {
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
