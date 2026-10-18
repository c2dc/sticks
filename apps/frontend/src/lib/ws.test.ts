import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { connectStages } from "@/lib/campaigns"
import { connectWs } from "@/lib/ws"

class FakeWebSocket {
  static readonly OPEN = 1
  static instances: FakeWebSocket[] = []

  readonly url: string
  readyState = FakeWebSocket.OPEN
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  send() {}

  close() {
    this.onclose?.(new CloseEvent("close", { code: 1000 }))
  }
}

describe("WebSocket stage subscriptions", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    vi.stubGlobal("WebSocket", FakeWebSocket)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it("subscribes to the requested stage explicitly", () => {
    const connection = connectStages("shadowray", 3, vi.fn())

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].url).toContain(
      "/ws/stages/shadowray?stage=3",
    )
    connection.close()
  })

  it("does not reconnect after the backend closes a terminal stream normally", () => {
    connectWs("/ws/stages/shadowray?stage=1", {
      onMessage: vi.fn(),
      reconnectDelayMs: 10,
    })

    FakeWebSocket.instances[0].onclose?.(
      new CloseEvent("close", { code: 1000 }),
    )
    vi.advanceTimersByTime(100)

    expect(FakeWebSocket.instances).toHaveLength(1)
  })
})
