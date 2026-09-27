/**
 * Minimal typed HTTP client for the FastAPI backend.
 *
 * In dev, requests to `/api/*` are proxied to http://localhost:8000 by Vite
 * (see vite.config.ts). VITE_API_BASE_URL can override the base if needed.
 * Real endpoint wrappers are added in later tasks (task 11+).
 */

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api"

export class ApiError extends Error {
  readonly status: number
  readonly body?: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.body = body
  }
}

type RequestOptions = Omit<RequestInit, "body"> & { body?: unknown }

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, headers, ...rest } = options
  const isJsonBody = body !== undefined && !(body instanceof FormData)

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...rest,
    headers: {
      Accept: "application/json",
      ...(isJsonBody ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    body: isJsonBody ? JSON.stringify(body) : (body as BodyInit | undefined),
  })

  const raw = await response.text()
  const parsed = raw ? safeJsonParse(raw) : undefined

  if (!response.ok) {
    const message =
      (isRecord(parsed) && typeof parsed.detail === "string" && parsed.detail) ||
      `HTTP ${response.status} on ${path}`
    throw new ApiError(response.status, message, parsed)
  }

  return parsed as T
}

function safeJsonParse(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return raw
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "POST", body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "PUT", body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "DELETE" }),
}
