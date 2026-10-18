import { act, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { renderWithI18n } from "@/test/render"
import { Stage3View } from "@/components/Stage3View"
import type {
  AbilityResult,
  OperationChannelHandlers,
  OperationResultView,
  Stage3Result,
  Stage5Verdict,
  Stage6StartResult,
  WsConnection,
} from "@/lib/stages"
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
// Req. 1.3 — no modal/Dialog; the confirmation is the embedded inline section.
// ---------------------------------------------------------------------------

describe("Stage3View — embedded confirmation, no modal (Req. 1.3)", () => {
  it("renders the inline confirmation section and NO role=dialog is in the document", async () => {
    getStage3Mock.mockResolvedValue(makeStage3Result())

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    // The embedded inline confirmation section is present...
    const inline = await screen.findByTestId("stage6-inline-confirmation")
    expect(inline.tagName).toBe("SECTION")

    // ...and nothing in the document is a modal/dialog (Req. 1.3).
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(screen.queryByRole("alertdialog")).toBeNull()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Req. 1.6 — inline confirmation renders preview + explicit consent control;
// clicking it (or the ≤3s auto-start) calls startExecution(campaign, true).
// ---------------------------------------------------------------------------

describe("Stage3View — inline consent control (Req. 1.6)", () => {
  it("renders the command/target preview and the explicit consent control", async () => {
    getStage3Mock.mockResolvedValue(makeStage3Result())
    getPreviewMock.mockResolvedValue({
      campaign_id: CAMPAIGN,
      has_external: false,
      abilities: [
        {
          ability_id: "a1",
          ability_name: "Exploit T1190",
          has_external: false,
          commands: [
            {
              command: "curl http://nginx:8265/exploit",
              local: false,
              has_external: false,
              destinations: [],
              targets: ["nginx (192.168.20.30)"],
            },
          ],
        },
      ],
    })
    // Hold auto-start off by never resolving the start call for this assertion.
    startExecutionMock.mockReturnValue(new Promise(() => {}))

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const inline = await screen.findByTestId("stage6-inline-confirmation")
    // The concrete command and its target container are previewed (Req. 1.6, 6.5).
    await within(inline).findByText("curl http://nginx:8265/exploit")
    expect(within(inline).getByText("nginx (192.168.20.30)")).toBeInTheDocument()
    // The explicit consent control is present and enabled before consent.
    const confirm = within(inline).getByTestId("stage6-confirm-execution")
    expect(confirm).toBeEnabled()
  })

  it("calls startExecution(campaign, true) when the researcher clicks consent", async () => {
    getStage3Mock.mockResolvedValue(makeStage3Result())
    startExecutionMock.mockResolvedValue({
      campaign_id: CAMPAIGN,
      executed: true,
      blocked: false,
      reused: false,
      output: makeOperation([{ ability_id: "a1", status: "em_execucao", command_output: "" }]),
      operation_id: 1,
    } satisfies Stage6StartResult)

    const user = userEvent.setup()
    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    const confirm = await screen.findByTestId("stage6-confirm-execution")
    await user.click(confirm)

    await waitFor(() =>
      expect(startExecutionMock).toHaveBeenCalledWith(CAMPAIGN, true),
    )
  })

  it("auto-starts within 3s (≤3s) with confirmed=true even without a click", async () => {
    vi.useFakeTimers()
    getStage3Mock.mockResolvedValue(makeStage3Result())
    startExecutionMock.mockResolvedValue({
      campaign_id: CAMPAIGN,
      executed: true,
      blocked: false,
      reused: false,
      output: makeOperation([{ ability_id: "a1", status: "em_execucao", command_output: "" }]),
      operation_id: 1,
    } satisfies Stage6StartResult)

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    // Let the snapshot promise resolve so phase becomes "confirming".
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(startExecutionMock).not.toHaveBeenCalled()

    // Advance the 3s auto-start delay — the embedded consent fires on its own.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(startExecutionMock).toHaveBeenCalledWith(CAMPAIGN, true)
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

    // The WS drops → the connection-lost indicator appears (Req. 2.8).
    act(() => connection.handlers.onClose?.())
    await screen.findByTestId("stage6-connection-lost")

    // The channel reconnects → the banner clears, without reloading the page.
    act(() => connection.handlers.onOpen?.())
    await waitFor(() =>
      expect(screen.queryByTestId("stage6-connection-lost")).toBeNull(),
    )
  })

  it("does not show the connection-lost banner when no Operation is being followed", async () => {
    // No persisted Operation → no live channel → the banner never applies even
    // though the inline confirmation is showing.
    getStage3Mock.mockResolvedValue(makeStage3Result())
    startExecutionMock.mockReturnValue(new Promise(() => {}))

    renderWithI18n(<Stage3View campaignId={CAMPAIGN} />)

    await screen.findByTestId("stage6-inline-confirmation")
    expect(screen.queryByTestId("stage6-connection-lost")).toBeNull()
    expect(connectOperationMock).not.toHaveBeenCalled()
  })
})
