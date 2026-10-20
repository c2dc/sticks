import { act, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { renderWithI18n } from "@/test/render"
import { Stage3View } from "@/components/Stage3View"
import type {
  AbilityResult,
  OperationChannelHandlers,
  OperationResultView,
  Stage3Result,
  Stage5Verdict,
} from "@/lib/stages"
import type { WsConnection } from "@/lib/ws"
import type { EmulationPreview } from "@/lib/emulation"

// Mock the stage + emulation wrappers so no network/WebSocket is used. The
// component only touches these four stage wrappers plus the preview wrapper.
vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return {
    ...actual,
    getStage3: vi.fn(),
    startExecution: vi.fn(),
    connectOperation: vi.fn(),
    getTopology: vi.fn(),
  }
})

vi.mock("@/lib/emulation", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/emulation")>()
  return {
    ...actual,
    getEmulationPreview: vi.fn(),
  }
})

import { connectOperation, getStage3, getTopology, startExecution } from "@/lib/stages"
import { getEmulationPreview } from "@/lib/emulation"

const getStage3Mock = vi.mocked(getStage3)
const startExecutionMock = vi.mocked(startExecution)
const connectOperationMock = vi.mocked(connectOperation)
const getTopologyMock = vi.mocked(getTopology)
const getPreviewMock = vi.mocked(getEmulationPreview)

const CAMPAIGN = "shadowray"

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

function makeInput(overrides: Partial<Stage3Result["input"]> = {}): Stage3Result["input"] {
  return {
    campaign_id: CAMPAIGN,
    name: "ShadowRay",
    adversary: {
      id: "adv-1",
      name: "ShadowRay Adversary",
      description: "",
      atomic_ordering: ["a1", "a2"],
    },
    abilities: [
      {
        ability_id: "a1",
        name: "Exploit T1190",
        technique_id: "T1190",
        tactic: "initial-access",
        description: "",
        executors: [],
      },
      {
        ability_id: "a2",
        name: "Execute T1059",
        technique_id: "T1059",
        tactic: "execution",
        description: "",
        executors: [],
      },
    ],
    can_emulate: true,
    ...overrides,
  }
}

function makeOperation(
  results: AbilityResult[],
  overrides: Partial<OperationResultView> = {},
): OperationResultView {
  const total_success = results.filter((r) => r.status === "sucesso").length
  const total_failure = results.filter((r) => r.status === "falha").length
  return {
    operation_id: "op-1",
    state: "em_execucao",
    total_success,
    total_failure,
    results,
    ...overrides,
  }
}

function makeStage3Result(overrides: Partial<Stage3Result> = {}): Stage3Result {
  return {
    input: makeInput(),
    output: [],
    ...overrides,
  }
}

const EMPTY_PREVIEW: EmulationPreview = {
  campaign_id: CAMPAIGN,
  has_external: false,
  abilities: [],
}

/**
 * Install a fake {@link connectOperation} that captures the lifecycle handlers
 * so a test can drive `onFrame`/`onOpen`/`onClose` by hand. Returns a getter for
 * the captured handlers plus the `close` spy.
 */
function captureConnection() {
  const close = vi.fn()
  let handlers: OperationChannelHandlers | null = null
  connectOperationMock.mockImplementation((_operation, h) => {
    handlers = typeof h === "function" ? { onFrame: h } : h
    return { send: vi.fn(), close } satisfies WsConnection
  })
  return {
    get handlers(): OperationChannelHandlers {
      if (!handlers) throw new Error("connectOperation was not called yet")
      return handlers
    },
    close,
  }
}

beforeEach(() => {
  getStage3Mock.mockReset()
  startExecutionMock.mockReset()
  connectOperationMock.mockReset()
  getTopologyMock.mockReset()
  getPreviewMock.mockReset()

  // Best-effort context wrappers default to resolving empty so they never block.
  getTopologyMock.mockRejectedValue(new Error("no topology"))
  getPreviewMock.mockResolvedValue(EMPTY_PREVIEW)
  // A connection is only used by the live-follow tests; default to a no-op.
  connectOperationMock.mockReturnValue({ send: vi.fn(), close: vi.fn() })
})

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// Task 5.1 / Req. 1.1, 1.3 — Input + Processing stacked as horizontal rows, NOT
// a grid; no modal/Dialog is ever introduced.
// ---------------------------------------------------------------------------

describe("Stage3View — Input/Processing as stacked rows, no grid (Req. 1.1, 1.3)", () => {
  it("renders NO md:grid-cols-3 container and stacks Input above Processing as rows", async () => {
    getStage3Mock.mockResolvedValue(makeStage3Result())
    // Hold the auto-start off so the snapshot view stays put for the assertion.
    startExecutionMock.mockReturnValue(new Promise(() => {}))

    const { container } = renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const view = await screen.findByTestId("stage6-view")

    // The former 3-column grid wrapper is gone (Task 5.1): no element in the
    // Stage-6 view carries the `md:grid-cols-3` (or any grid-cols) class.
    expect(container.querySelector(".\\md\\:grid-cols-3")).toBeNull()
    expect(container.querySelector('[class*="md:grid-cols-3"]')).toBeNull()
    expect(container.querySelector('[class*="grid-cols"]')).toBeNull()

    // Input and Processing are both present as full-width rows, and their shared
    // parent lays them out as a vertical flex column (stacked rows), not a grid.
    const input = view.querySelector('[data-section="entrada"]') as HTMLElement
    const processing = view.querySelector('[data-section="processamento"]') as HTMLElement
    expect(input).not.toBeNull()
    expect(processing).not.toBeNull()

    const rowsParent = input.parentElement as HTMLElement
    // Input and Processing share the same stacking parent...
    expect(processing.parentElement).toBe(rowsParent)
    // ...and that parent is a flex column (rows), never a grid.
    expect(rowsParent.className).toContain("flex")
    expect(rowsParent.className).toContain("flex-col")
    expect(rowsParent.className).not.toContain("grid")

    // Input is rendered before Processing in document order (Input → Processing).
    expect(
      input.compareDocumentPosition(processing) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it("introduces no modal/dialog in any state (Req. 1.3)", async () => {
    getStage3Mock.mockResolvedValue(makeStage3Result())
    startExecutionMock.mockReturnValue(new Promise(() => {}))

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    await screen.findByTestId("stage6-view")
    // Execution is automatic; nothing in the document is a modal/dialog (1.3).
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(screen.queryByRole("alertdialog")).toBeNull()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Task 5.1 / Req. 1.2 — per-Ability Processing content preserved without
// duplication (command / target / attacker host / state / logs each once).
// ---------------------------------------------------------------------------

describe("Stage3View — per-Ability content preserved without duplication (Req. 1.2)", () => {
  it("renders each Ability once with its command/target/host/logs shown a single time", async () => {
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation(
            [
              {
                ability_id: "a1",
                status: "em_execucao",
                command_output: "stdout-a1",
                command: "whoami",
                target_container: "nginx (192.168.20.30)",
                attacker_host: "kali",
              },
              {
                ability_id: "a2",
                status: "sucesso",
                command_output: "stdout-a2",
                command: "id",
                target_container: "app (192.168.20.31)",
                attacker_host: "kali",
              },
            ],
            { state: "em_execucao" },
          ),
        ],
      }),
    )

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const list = await screen.findByTestId("stage6-live-abilities")
    const cards = within(list).getAllByTestId("stage6-ability-card")
    // Two Abilities in the ordering → exactly two cards, one per ability_id.
    expect(cards).toHaveLength(2)
    expect(within(list).getAllByTitle("a1")).toHaveLength(1)
    expect(within(list).getAllByTitle("a2")).toHaveLength(1)

    // a1's concrete content appears exactly once (no duplication, Req. 1.2).
    const a1 = cards.find((c) => c.getAttribute("data-status") === "em_execucao")!
    expect(within(a1).getAllByText("whoami")).toHaveLength(1)
    expect(within(a1).getAllByText("nginx (192.168.20.30)")).toHaveLength(1)
    expect(within(a1).getAllByText("stdout-a1")).toHaveLength(1)
    // a2's logs render only inside the a2 card, once.
    expect(within(list).getAllByText("stdout-a2")).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// Task 5.2 / Req. 2.1, 2.2 — the Output block (data-section="saida") is gone in
// EVERY execution state (confirming/running/persisted/terminal).
// ---------------------------------------------------------------------------

describe("Stage3View — no Output section in any state (Req. 2.1, 2.2)", () => {
  it("has no data-section=\"saida\" before the run starts (snapshot / auto-start pending)", async () => {
    getStage3Mock.mockResolvedValue(makeStage3Result())
    startExecutionMock.mockReturnValue(new Promise(() => {}))

    const { container } = renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    await screen.findByTestId("stage6-view")
    expect(container.querySelector('[data-section="saida"]')).toBeNull()
  })

  it("has no data-section=\"saida\" while an Operation is running", async () => {
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation([{ ability_id: "a1", status: "em_execucao", command_output: "" }], {
            state: "em_execucao",
          }),
        ],
      }),
    )

    const { container } = renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    await screen.findByTestId("stage6-live-abilities")
    expect(container.querySelector('[data-section="saida"]')).toBeNull()
  })

  it("has no data-section=\"saida\" for a persisted terminal result", async () => {
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation(
            [
              { ability_id: "a1", status: "sucesso", command_output: "ok" },
              { ability_id: "a2", status: "falha", command_output: "x" },
            ],
            { state: "concluida" },
          ),
        ],
      }),
    )

    const { container } = renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    await screen.findByTestId("stage6-live-abilities")
    // Terminal, persisted re-display: still no Output/aggregate block (2.1, 2.2).
    expect(container.querySelector('[data-section="saida"]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Task 5.3 / Req. 3.1, 7.1, 8.2 — onExecutionFinished fires true once when the
// current Operation is terminal (or a persisted result is re-displayed), and
// resets to false when another campaign loads.
// ---------------------------------------------------------------------------

describe("Stage3View — onExecutionFinished signal (Req. 3.1, 7.1, 8.2)", () => {
  it("fires true exactly once for a persisted terminal Operation", async () => {
    const onExecutionFinished = vi.fn()
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation([{ ability_id: "a1", status: "sucesso", command_output: "ok" }], {
            state: "concluida",
          }),
        ],
      }),
    )

    renderWithI18n(
      <Stage3View campaignId={CAMPAIGN} onExecutionFinished={onExecutionFinished} />,
    )

    await screen.findByTestId("stage6-live-abilities")
    await waitFor(() => expect(onExecutionFinished).toHaveBeenCalledWith(true))
    // Idempotent: the true signal is emitted a single time for this run.
    expect(onExecutionFinished.mock.calls.filter(([v]) => v === true)).toHaveLength(1)
  })

  it("fires true when the current Operation reaches a terminal state via a live frame", async () => {
    const onExecutionFinished = vi.fn()
    const connection = captureConnection()
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation([{ ability_id: "a1", status: "em_execucao", command_output: "" }], {
            state: "concluida",
          }),
        ],
      }),
    )

    renderWithI18n(
      <Stage3View campaignId={CAMPAIGN} onExecutionFinished={onExecutionFinished} />,
    )

    await screen.findByTestId("stage6-live-abilities")
    // The persisted Operation state ("concluida") is terminal → finished fires.
    await waitFor(() => expect(onExecutionFinished).toHaveBeenCalledWith(true))
    // A later frame does not re-emit the true signal (idempotent).
    act(() => {
      connection.handlers.onFrame({
        type: "ability",
        ability_id: "a1",
        status: "sucesso",
        command_output: "done",
      })
    })
    expect(onExecutionFinished.mock.calls.filter(([v]) => v === true)).toHaveLength(1)
  })

  it("does not fire true while the Operation is still running (non-terminal)", async () => {
    const onExecutionFinished = vi.fn()
    // Empty snapshot → nothing persisted → the view auto-starts. The runner
    // returns a NON-terminal, non-reused Operation, so the view enters the
    // "running" phase following a live Operation whose state is not terminal.
    getStage3Mock.mockResolvedValue(makeStage3Result())
    startExecutionMock.mockResolvedValue({
      campaign_id: CAMPAIGN,
      executed: true,
      blocked: false,
      reused: false,
      output: makeOperation(
        [{ ability_id: "a1", status: "em_execucao", command_output: "" }],
        { state: "em_execucao" },
      ),
      operation_id: 1,
    })

    renderWithI18n(
      <Stage3View campaignId={CAMPAIGN} onExecutionFinished={onExecutionFinished} />,
    )

    await screen.findByTestId("stage6-live-abilities")
    await waitFor(() => expect(connectOperationMock).toHaveBeenCalled())
    // A non-terminal, running Operation never signals finished=true.
    expect(onExecutionFinished).not.toHaveBeenCalledWith(true)
  })
})

// ---------------------------------------------------------------------------
// Req. 2.2 — activity indicator present for `em_execucao`, absent for terminal.
// ---------------------------------------------------------------------------

describe("Stage3View — per-status activity indicator (Req. 2.2)", () => {
  it("shows the activity indicator only for an Ability in em_execucao", async () => {
    // Seed a persisted Operation so the live list renders from the snapshot
    // (idempotent re-display path, no auto-start).
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation(
            [
              { ability_id: "a1", status: "em_execucao", command_output: "" },
              { ability_id: "a2", status: "sucesso", command_output: "ok" },
            ],
            { state: "em_execucao" },
          ),
        ],
      }),
    )

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const list = await screen.findByTestId("stage6-live-abilities")
    const cards = within(list).getAllByTestId("stage6-ability-card")
    const running = cards.find((c) => c.getAttribute("data-status") === "em_execucao")!
    const succeeded = cards.find((c) => c.getAttribute("data-status") === "sucesso")!

    // The running Ability has the activity indicator; the succeeded one does not.
    expect(within(running).getByTestId("stage6-ability-activity")).toBeInTheDocument()
    expect(within(succeeded).queryByTestId("stage6-ability-activity")).toBeNull()
  })

  it("removes the activity indicator when an Ability transitions to a terminal state via a live frame", async () => {
    const connection = captureConnection()
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation([{ ability_id: "a1", status: "em_execucao", command_output: "" }], {
            state: "em_execucao",
          }),
        ],
      }),
    )

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const list = await screen.findByTestId("stage6-live-abilities")
    expect(within(list).getByTestId("stage6-ability-activity")).toBeInTheDocument()

    // Drive a terminal frame for a1 → the activity indicator must disappear (2.2).
    act(() => {
      connection.handlers.onFrame({
        type: "ability",
        ability_id: "a1",
        status: "sucesso",
        command_output: "done",
      })
    })

    await waitFor(() =>
      expect(within(list).queryByTestId("stage6-ability-activity")).toBeNull(),
    )
  })
})

// ---------------------------------------------------------------------------
// Req. 2.5 — explicit "não disponível" label for null/empty per-Ability data.
// ---------------------------------------------------------------------------

describe("Stage3View — 'não disponível' for missing data (Req. 2.5)", () => {
  it("shows the explicit not-available label for null command/target/host/logs", async () => {
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation(
            [
              {
                ability_id: "a1",
                status: "em_execucao",
                command_output: "",
                command: null,
                target_container: null,
                attacker_host: null,
              },
            ],
            { state: "em_execucao" },
          ),
        ],
      }),
    )

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const list = await screen.findByTestId("stage6-live-abilities")
    const card = within(list).getAllByTestId("stage6-ability-card")[0]
    // Command, target, attacker host and logs are each absent → one explicit
    // "não disponível" label per datum, never a blank unlabeled field (2.5).
    const notAvailable = within(card).getAllByTestId("stage6-not-available")
    expect(notAvailable.length).toBe(4)
    for (const el of notAvailable) {
      expect(el).toHaveTextContent("não disponível")
    }
  })

  it("fills a datum from a live frame, dropping its not-available label", async () => {
    const connection = captureConnection()
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation(
            [
              {
                ability_id: "a1",
                status: "em_execucao",
                command_output: "",
                command: null,
                target_container: null,
                attacker_host: null,
              },
            ],
            { state: "em_execucao" },
          ),
        ],
      }),
    )

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const list = await screen.findByTestId("stage6-live-abilities")
    const a1Card = () =>
      within(list)
        .getAllByTestId("stage6-ability-card")
        .find((c) => within(c).queryByTitle("a1"))!
    // The a1 card starts with all four data missing (command/target/host/logs).
    expect(within(a1Card()).getAllByTestId("stage6-not-available").length).toBe(4)

    act(() => {
      connection.handlers.onFrame({
        type: "ability",
        ability_id: "a1",
        status: "em_execucao",
        command_output: "stdout line",
        command: "whoami",
        target_container: "nginx (192.168.20.30)",
        attacker_host: "kali",
      })
    })

    // All four data arrived → no not-available labels remain for the a1 card.
    await waitFor(() =>
      expect(within(a1Card()).queryAllByTestId("stage6-not-available").length).toBe(0),
    )
    const card = a1Card()
    expect(within(card).getByText("whoami")).toBeInTheDocument()
    expect(within(card).getByText("nginx (192.168.20.30)")).toBeInTheDocument()
    expect(within(card).getByText("kali")).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Req. 5.4 — read-only Stage-5 panel, no provision/verify controls.
// ---------------------------------------------------------------------------

describe("Stage3View — read-only Stage-5 panel (Req. 5.4)", () => {
  it("renders the Stage-5 verdict read-only with no actionable controls", async () => {
    const stage5: Stage5Verdict = {
      concluded: true,
      isolated: true,
      isolation_unavailable: false,
      reason: null,
    }
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation([{ ability_id: "a1", status: "sucesso", command_output: "ok" }], {
            state: "concluida",
          }),
        ],
        stage5,
      }),
    )

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const panel = await screen.findByTestId("stage6-stage5-panel")
    // The read-only verdict label is shown and the "ready" state is reflected.
    expect(panel).toHaveTextContent("Ambiente (Estágio 5)")
    expect(panel).toHaveTextContent("Pronto")
    expect(panel).toHaveTextContent(/somente leitura/i)

    // No provision/verify controls: the panel carries no buttons or inputs.
    expect(within(panel).queryByRole("button")).toBeNull()
    expect(panel.querySelector("button")).toBeNull()
    expect(panel.querySelector("input")).toBeNull()
  })

  it("surfaces the not-ready reason without any action controls", async () => {
    const stage5: Stage5Verdict = {
      concluded: true,
      isolated: false,
      isolation_unavailable: true,
      reason: "Isolamento não pôde ser verificado",
    }
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation([{ ability_id: "a1", status: "falha", command_output: "x" }], {
            state: "falha",
          }),
        ],
        stage5,
      }),
    )

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const panel = await screen.findByTestId("stage6-stage5-panel")
    expect(panel).toHaveTextContent("Não pronto")
    expect(panel).toHaveTextContent("Isolamento não pôde ser verificado")
    expect(within(panel).queryByRole("button")).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Req. 2.8 — connection-lost indicator on WS drop, cleared on reconnect.
// ---------------------------------------------------------------------------

describe("Stage3View — connection-lost indicator (Req. 2.8)", () => {
  it("shows the connection-lost banner on onClose and clears it on onOpen", async () => {
    const connection = captureConnection()
    getStage3Mock.mockResolvedValue(
      makeStage3Result({
        output: [
          makeOperation([{ ability_id: "a1", status: "em_execucao", command_output: "" }], {
            state: "em_execucao",
          }),
        ],
      }),
    )

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    // We are following a live Operation; the channel starts connected, so no
    // banner is shown initially.
    await screen.findByTestId("stage6-live-abilities")
    await waitFor(() => expect(connectOperationMock).toHaveBeenCalled())
    expect(screen.queryByTestId("stage6-connection-lost")).toBeNull()

    // A CLEAN close (code 1000: the Operation reached a terminal state and the
    // backend ended the stream normally) is NOT a lost connection — the banner
    // must stay hidden (Req. 2.8; fixes the false "connection lost" shown when a
    // finished Operation's channel closed normally).
    act(() => connection.handlers.onClose?.(true))
    await waitFor(() =>
      expect(screen.queryByTestId("stage6-connection-lost")).toBeNull(),
    )

    // An ABNORMAL close (network drop, clean === false) → the banner appears.
    act(() => connection.handlers.onClose?.(false))
    await screen.findByTestId("stage6-connection-lost")

    // The channel reconnects → the banner clears, without reloading the page.
    act(() => connection.handlers.onOpen?.())
    await waitFor(() =>
      expect(screen.queryByTestId("stage6-connection-lost")).toBeNull(),
    )
  })

  it("does not show the connection-lost banner when no Operation is being followed", async () => {
    // No persisted Operation → no live channel → the banner never applies even
    // while the automatic start is still in flight.
    getStage3Mock.mockResolvedValue(makeStage3Result())
    startExecutionMock.mockReturnValue(new Promise(() => {}))

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    await screen.findByTestId("stage6-view")
    expect(screen.queryByTestId("stage6-connection-lost")).toBeNull()
    expect(connectOperationMock).not.toHaveBeenCalled()
  })
})
