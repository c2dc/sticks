import { describe, expect, it, vi, beforeEach } from "vitest"
import { act, screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { ProvisioningConsole } from "@/components/ProvisioningConsole"
import type { StageProgressFrame, LogLineFrame } from "@/lib/campaigns"
import type { WsConnection } from "@/lib/ws"
import type { Stage5LogsResponse, LogLineView } from "@/lib/stages"

// Mock the Stage-5 logs wrapper (buffer recovery on mount) so no network is used.
vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return {
    ...actual,
    getStage5Logs: vi.fn(),
  }
})

// Mock the stage-progress channel: capture `onFrame` so the test can push
// frames (simulating WS frames) and hand back a fake `WsConnection` whose
// `close` can be asserted on unmount.
vi.mock("@/lib/campaigns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/campaigns")>()
  return {
    ...actual,
    connectStages: vi.fn(),
  }
})

import { getStage5Logs } from "@/lib/stages"
import { connectStages } from "@/lib/campaigns"

const getLogsEstagio5Mock = vi.mocked(getStage5Logs)
const connectEstagiosMock = vi.mocked(connectStages)

/** The `onFrame` callback captured from the latest `connectStages` call. */
let capturedOnFrame: ((frame: StageProgressFrame) => void) | null = null
/** The fake connection returned by `connectStages`; its `close` is tracked. */
let fakeConnection: WsConnection

function makeLinha(overrides: Partial<LogLineView>): LogLineView {
  return {
    seq: 0,
    step: "subir_conteineres",
    stream: "stdout",
    text: "linha",
    at: "2024-01-01T00:00:00",
    ...overrides,
  }
}

/** Push a WS frame carrying `logs` through the captured `onFrame` callback. */
function pushFrame(logs: LogLineFrame[]) {
  if (!capturedOnFrame) throw new Error("connectStages onFrame not captured")
  const onFrame = capturedOnFrame
  // Wrap in act(...) — the callback drives React state updates (the buffer).
  act(() => {
    onFrame({
      stage: 5,
      state: "em_andamento",
      progress: 0,
      logs,
    })
  })
}

describe("ProvisioningConsole (Estágio 5 — terminal de logs ao vivo, Req. 2.3/2.5/2.7)", () => {
  beforeEach(() => {
    getLogsEstagio5Mock.mockReset()
    connectEstagiosMock.mockReset()
    capturedOnFrame = null
    fakeConnection = { send: vi.fn(), close: vi.fn() }

    // Default: empty buffer recovery + capture the live callback.
    getLogsEstagio5Mock.mockResolvedValue({
      campaign_id: "c0026",
      logs: [],
    } satisfies Stage5LogsResponse)
    connectEstagiosMock.mockImplementation((_caso, _estagio, onFrame) => {
      capturedOnFrame = onFrame
      return fakeConnection
    })
  })

  /** The ordered `texto` strings of every rendered console line, top → bottom. */
  function renderedTexts(): string[] {
    const log = screen.getByRole("log")
    const items = log.querySelectorAll("li")
    // The last span inside each line carries the `texto` (badge is first span).
    return Array.from(items).map((li) => {
      const spans = li.querySelectorAll("span")
      return spans[spans.length - 1]?.textContent ?? ""
    })
  }

  it("renders lines in ascending seq order even when frames arrive out of order (Req. 2.3)", async () => {
    renderWithI18n(<ProvisioningConsole campaignId="c0026" />)

    await waitFor(() => expect(connectEstagiosMock).toHaveBeenCalledWith("c0026", 5, expect.any(Function)))

    // Frame 1 delivers seq [2, 1] out of order; frame 2 adds seq [3].
    pushFrame([
      makeLinha({ seq: 2, text: "segunda" }),
      makeLinha({ seq: 1, text: "primeira" }),
    ])
    pushFrame([makeLinha({ seq: 3, text: "terceira" })])

    await waitFor(() => expect(renderedTexts()).toEqual(["primeira", "segunda", "terceira"]))
  })

  it("dedupes by seq — a repeated seq replaces, never duplicates (Req. 2.3)", async () => {
    renderWithI18n(<ProvisioningConsole campaignId="c0026" />)
    await waitFor(() => expect(connectEstagiosMock).toHaveBeenCalled())

    pushFrame([makeLinha({ seq: 1, text: "original" })])
    await waitFor(() => expect(renderedTexts()).toEqual(["original"]))

    // Same seq arrives again (e.g. redelivered on reconnect) with new text.
    pushFrame([makeLinha({ seq: 1, text: "atualizada" })])

    await waitFor(() => expect(renderedTexts()).toEqual(["atualizada"]))
    // Exactly one line — no duplicate.
    expect(screen.getByRole("log").querySelectorAll("li")).toHaveLength(1)
  })

  it("colors each line by fluxo (Req. 2.4)", async () => {
    renderWithI18n(<ProvisioningConsole campaignId="c0026" />)
    await waitFor(() => expect(connectEstagiosMock).toHaveBeenCalled())

    pushFrame([
      makeLinha({ seq: 1, stream: "comando", text: "docker compose up" }),
      makeLinha({ seq: 2, stream: "stdout", text: "saida padrao" }),
      makeLinha({ seq: 3, stream: "stderr", text: "erro de saida" }),
      makeLinha({ seq: 4, stream: "info", text: "informacao" }),
    ])

    const comando = await screen.findByText("docker compose up")
    expect(comando).toHaveClass("text-team-blue")
    expect(comando).toHaveClass("font-bold")

    expect(screen.getByText("saida padrao")).toHaveClass("text-zinc-100")
    expect(screen.getByText("erro de saida")).toHaveClass("text-team-red")
    expect(screen.getByText("informacao")).toHaveClass("text-zinc-400")
  })

  it("auto-scrolls the console to the bottom when lines change", async () => {
    renderWithI18n(<ProvisioningConsole campaignId="c0026" />)
    await waitFor(() => expect(connectEstagiosMock).toHaveBeenCalled())

    const log = screen.getByRole("log")
    // jsdom does not layout, so stub a known scrollHeight and start scrollTop at 0.
    Object.defineProperty(log, "scrollHeight", { configurable: true, value: 1234 })
    log.scrollTop = 0

    pushFrame([makeLinha({ seq: 1, text: "primeira" })])

    // The effect must drive scrollTop to the bottom (scrollHeight).
    await waitFor(() => expect(log.scrollTop).toBe(1234))
  })

  it("recovers the current buffer on mount before any WS frame (Req. 2.5/2.7)", async () => {
    getLogsEstagio5Mock.mockResolvedValue({
      campaign_id: "c0026",
      logs: [
        makeLinha({ seq: 1, text: "buffer-um" }),
        makeLinha({ seq: 2, text: "buffer-dois" }),
      ],
    } satisfies Stage5LogsResponse)

    renderWithI18n(<ProvisioningConsole campaignId="c0026" />)

    // Recovered lines render without any frame being pushed.
    await waitFor(() => expect(renderedTexts()).toEqual(["buffer-um", "buffer-dois"]))
    expect(getLogsEstagio5Mock).toHaveBeenCalledWith("c0026")
  })

  it("stays whole with no duplicates when reconnect delivers the full buffer (Req. 2.7)", async () => {
    // Recovery seeds seq [1, 2].
    getLogsEstagio5Mock.mockResolvedValue({
      campaign_id: "c0026",
      logs: [
        makeLinha({ seq: 1, text: "um" }),
        makeLinha({ seq: 2, text: "dois" }),
      ],
    } satisfies Stage5LogsResponse)

    renderWithI18n(<ProvisioningConsole campaignId="c0026" />)
    await waitFor(() => expect(renderedTexts()).toEqual(["um", "dois"]))

    // Reconnect: the channel's first frame carries the complete buffer,
    // including the already-recovered lines plus a new one.
    pushFrame([
      makeLinha({ seq: 1, text: "um" }),
      makeLinha({ seq: 2, text: "dois" }),
      makeLinha({ seq: 3, text: "tres" }),
    ])

    await waitFor(() => expect(renderedTexts()).toEqual(["um", "dois", "tres"]))
    // Dedupe by seq: history whole, no duplicates.
    expect(screen.getByRole("log").querySelectorAll("li")).toHaveLength(3)
  })

  it("closes the WsConnection on unmount (Req. 2.7)", async () => {
    const { unmount } = renderWithI18n(<ProvisioningConsole campaignId="c0026" />)
    await waitFor(() => expect(connectEstagiosMock).toHaveBeenCalled())

    unmount()

    expect(fakeConnection.close).toHaveBeenCalledTimes(1)
  })
})
