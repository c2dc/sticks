import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { connectEstagios } from "@/lib/casos"
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
    const connection = connectEstagios("shadowray", 3, vi.fn())

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].url).toContain(
      "/ws/estagios/shadowray?estagio=3",
    )
    connection.close()
  })

  it("does not reconnect after the backend closes a terminal stream normally", () => {
    connectWs("/ws/estagios/shadowray?estagio=1", {
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
