import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, screen } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { IngestionStatusBar } from "@/components/IngestionStatusBar"
import type { IngestionSourceStatus } from "@/lib/ingestion"

/**
 * IngestionStatusBar: per-source states, polling while a source is running and
 * the resume button on failure (Req. 7.1, 7.2, 7.9). The client is mocked and
 * timers are fake.
 */

vi.mock("@/lib/ingestion", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/ingestion")>()
  return {
    ...actual,
    verifyIngestion: vi.fn(),
    getIngestionStatus: vi.fn(),
    startIngestion: vi.fn(),
  }
})

import { getIngestionStatus, startIngestion, verifyIngestion } from "@/lib/ingestion"

const verifyMock = vi.mocked(verifyIngestion)
const statusMock = vi.mocked(getIngestionStatus)
const startMock = vi.mocked(startIngestion)

function source(
  name: IngestionSourceStatus["source"],
  status: IngestionSourceStatus["status"],
  error: string | null = null,
): IngestionSourceStatus {
  return {
    source: name,
    status,
    loaded_count: null,
    embedded_count: null,
    expected_count: null,
    error,
    skipped_count: 0,
    updated_at: null,
  }
}

async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe("IngestionStatusBar", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    verifyMock.mockReset()
    statusMock.mockReset()
    startMock.mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("shows each source state and polls while one is running, then stops", async () => {
    verifyMock.mockResolvedValue([source("atomic", "running"), source("docker_catalog", "ready")])
    statusMock.mockResolvedValue([source("atomic", "ready"), source("docker_catalog", "ready")])
    renderWithI18n(<IngestionStatusBar />)
    await flush()

    expect(verifyMock).toHaveBeenCalledTimes(1)
    expect(document.querySelector('[data-source="atomic"]')).toHaveAttribute("data-status", "running")
    expect(screen.getByText("Carregando")).toBeInTheDocument()

    await flush(3000)
    expect(statusMock).toHaveBeenCalledTimes(1)
    expect(document.querySelector('[data-source="atomic"]')).toHaveAttribute("data-status", "ready")

    await flush(9000)
    expect(statusMock).toHaveBeenCalledTimes(1)
  })

  it("offers Retomar for a failed source and restarts only that source", async () => {
    verifyMock.mockResolvedValue([source("atomic", "failed", "boom"), source("attack_flow", "ready")])
    startMock.mockResolvedValue(source("atomic", "ready"))
    renderWithI18n(<IngestionStatusBar />)
    await flush()

    expect(screen.getByText("Causa: boom")).toBeInTheDocument()
    const resume = screen.getByRole("button", { name: "Retomar" })
    await act(async () => {
      resume.click()
    })
    await flush()

    expect(startMock).toHaveBeenCalledWith("atomic")
    expect(screen.queryByRole("button", { name: "Retomar" })).toBeNull()
  })
})
