import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, screen, within } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import {
  ATOMIC_POLL_INTERVAL_MS,
  OperationalizationScreen,
} from "@/components/OperationalizationScreen"
import type { OperationalizationListing } from "@/lib/operationalization"

/**
 * Deterministic tests for the OperationalizationScreen: techniques with their
 * commands, an explicit message for a technique without commands, and the
 * not-ready warning that rereads the listing until `atomic_status` is `ready`
 * (Req. 6.3, 6.4, 6.5). The API client is mocked; timers are fake.
 */

vi.mock("@/lib/operationalization", () => ({
  getOperationalization: vi.fn(),
}))

import { getOperationalization } from "@/lib/operationalization"

const getMock = vi.mocked(getOperationalization)

function listing(
  status: "ready" | "running",
  loaded: number | null = null,
): OperationalizationListing {
  return {
    campaign_key: "shadowray",
    atomic_status: { status, loaded_count: loaded, expected_count: loaded === null ? null : 10 },
    techniques: [
      {
        technique_id: "T1059",
        technique_name: "Command and Scripting Interpreter",
        commands: [
          { id: 1, command: "whoami /all", executor: "powershell", platform: "windows", source: "atomic" },
        ],
      },
      { technique_id: "T1003", technique_name: "OS Credential Dumping", commands: [] },
    ],
  }
}

/** Flush pending promise callbacks inside act. */
async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe("OperationalizationScreen", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    getMock.mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("lists commands per technique and flags a technique without commands", async () => {
    getMock.mockResolvedValue(listing("ready"))
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
    await flush()

    expect(getMock).toHaveBeenCalledWith("18.0", "shadowray")
    const items = screen.getAllByTestId("operationalization-technique")
    expect(items).toHaveLength(2)
    expect(within(items[0]).getByText("whoami /all")).toBeInTheDocument()
    expect(within(items[1]).getByTestId("no-commands")).toBeInTheDocument()
    expect(screen.queryByTestId("atomic-notice")).toBeNull()
  })

  it("warns while not ready and rereads until it becomes ready", async () => {
    getMock
      .mockResolvedValueOnce(listing("running", 4))
      .mockResolvedValue(listing("ready", 10))
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
    await flush()

    expect(screen.getByTestId("atomic-notice")).toBeInTheDocument()
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40")
    expect(getMock).toHaveBeenCalledTimes(1)

    await flush(ATOMIC_POLL_INTERVAL_MS)
    expect(getMock).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId("atomic-notice")).toBeNull()

    // Ready: no further polling.
    await flush(ATOMIC_POLL_INTERVAL_MS * 2)
    expect(getMock).toHaveBeenCalledTimes(2)
  })
})
