import { beforeEach, describe, expect, it, vi } from "vitest"

// Mock the HTTP client so no real network is used; we only assert the URL the
// wrapper builds and hands to `api.get`.
vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(() => Promise.resolve({})),
    post: vi.fn(() => Promise.resolve({})),
    put: vi.fn(() => Promise.resolve({})),
    delete: vi.fn(() => Promise.resolve({})),
  },
}))

import { api } from "@/lib/api"
import { getStage1 } from "@/lib/stages"

const getMock = vi.mocked(api.get)

/**
 * Feature: stage1-campaign-stix input — wrapper `getStage1(campaign, version?)`.
 * Validates: Requirements 5.2, 6.2
 *
 * The wrapper builds the Stage 1 URL with `?version=<encoded>` iff the selected
 * STIX base version is provided; without a version it uses the raw route
 * (backward-compatible backend fallback). Values are always percent-encoded.
 */
describe("getStage1 — URL assembly (with/without ?version=)", () => {
  beforeEach(() => {
    getMock.mockClear()
  })

  it("without version: calls api.get with the raw route (no query string)", () => {
    void getStage1("campaign-x")
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith("/campaigns/campaign-x/stage/1")
  })

  it("with version: appends ?version=<value>", () => {
    void getStage1("campaign-x", "18.0")
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith(
      "/campaigns/campaign-x/stage/1?version=18.0",
    )
  })

  it("percent-encodes the campaign and the version when they need encoding", () => {
    void getStage1("campaign with space", "17.1/rc 2")
    expect(getMock).toHaveBeenCalledWith(
      "/campaigns/campaign%20with%20space/stage/1?version=17.1%2Frc%202",
    )
  })
})
