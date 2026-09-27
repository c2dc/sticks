/**
 * WebSocket utility helper for the backend real-time channels
 * (e.g. /ws/estagios/{caso}, /ws/operacao/{operacao}).
 *
 * Provides a small reconnecting wrapper with typed JSON message handling.
 * Concrete channel hooks are built in later tasks (task 11+/12).
 */

export interface WsOptions<TMessage> {
  onMessage: (message: TMessage) => void
  onOpen?: () => void
  onClose?: (event: CloseEvent) => void
  onError?: (event: Event) => void
  /** Auto-reconnect with backoff when the socket closes unexpectedly. */
  reconnect?: boolean
  /** Base delay in ms for reconnect backoff. */
  reconnectDelayMs?: number
}

export interface WsConnection {
  send: (data: unknown) => void
  close: () => void
}

/** Resolve a `/ws/...` path to an absolute ws:// or wss:// URL. */
export function resolveWsUrl(path: string): string {
  const base = import.meta.env.VITE_WS_BASE_URL
  if (base) return `${base}${path}`

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${protocol}//${window.location.host}${path}`
}

export function connectWs<TMessage = unknown>(
  path: string,
  options: WsOptions<TMessage>,
): WsConnection {
  const {
    onMessage,
    onOpen,
    onClose,
    onError,
    reconnect = true,
    reconnectDelayMs = 1000,
  } = options

  let socket: WebSocket | null = null
  let closedByCaller = false
  let attempts = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  const open = () => {
    socket = new WebSocket(resolveWsUrl(path))

    socket.onopen = () => {
      attempts = 0
      onOpen?.()
    }

    socket.onmessage = (event) => {
      try {
        onMessage(JSON.parse(event.data) as TMessage)
      } catch {
        // Non-JSON frames are ignored by this typed helper.
      }
    }

    socket.onerror = (event) => onError?.(event)

    socket.onclose = (event) => {
      onClose?.(event)
      if (!closedByCaller && reconnect) {
        attempts += 1
        const delay = reconnectDelayMs * Math.min(attempts, 5)
        reconnectTimer = setTimeout(open, delay)
      }
    }
  }

  open()

  return {
    send: (data: unknown) => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(typeof data === "string" ? data : JSON.stringify(data))
      }
    },
    close: () => {
      closedByCaller = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      socket?.close()
    },
  }
}
