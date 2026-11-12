import { beforeEach, describe, expect, it, vi } from "vitest"
import fc from "fast-check"

// Mock the HTTP client so no real network is used. Each wrapper is exercised
// against the mock to assert (a) the exact URL it builds and (b) that the
// payload returned by `api` is passed through untransformed.
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = "ApiError"
      this.status = status
    }
  },
  api: {
    get: vi.fn(() => Promise.resolve(undefined)),
    post: vi.fn(() => Promise.resolve(undefined)),
    put: vi.fn(() => Promise.resolve(undefined)),
    delete: vi.fn(() => Promise.resolve(undefined)),
  },
}))

import { api } from "@/lib/api"
import * as stix from "@/lib/stix"
import {
  downloadBase,
  getCampaignOverview,
  getCampaigns,
  getPackageAnalytics,
  getPackageOverview,
  type StixCampaignOverview,
  type StixCampaignSummary,
  type StixPackageAnalytics,
  type StixPackageOverview,
} from "@/lib/stix"

const getMock = vi.mocked(api.get)
const postMock = vi.mocked(api.post)

/**
 * Feature: stix-package-persistence-and-overview — the single, coherent STIX
 * API surface wrapped by `lib/stix.ts`.
 * Validates: Requirements 6.1, 6.2, 6.3
 *
 * After tasks 7.1–7.3 the module exposes `getBases`, `downloadBase`,
 * `getPackageOverview`, `getCampaigns` and `getCampaignOverview`, and the
 * legacy `getDashboard`/`getAdversaries` wrappers are gone. These tests pin the
 * URL each wrapper builds (percent-encoding the path segments), the
 * pass-through of the payload, and the absence of the removed exports.
 */

const overviewFixture: StixPackageOverview = {
  version: "18.0",
  name: "Enterprise ATT&CK v18.0",
  downloaded_at: "2024-01-02T03:04:05Z",
  counts: [
    {
      label: "Techniques",
      stix_type: "attack-pattern",
      count: 652,
      description: "ATT&CK techniques and sub-techniques",
    },
  ],
  campaigns_total: 24,
  actors_total: 160,
  techniques_total: 652,
  tactics_total: 14,
  malware_total: 700,
  relationships_total: 23000,
}

const campaignsFixture: StixCampaignSummary[] = [
  {
    campaign_key: "c0001",
    stix_id: "campaign--1111",
    name: "Operation Alpha",
    external_id: "C0001",
    curated: true,
    curated_slug: "c0001",
  },
  {
    campaign_key: "campaign--2222",
    stix_id: "campaign--2222",
    name: "Operation Beta",
    external_id: null,
    curated: false,
    curated_slug: null,
  },
]

const campaignOverviewFixture: StixCampaignOverview = {
  campaign_key: "c0001",
  stix_id: "campaign--1111",
  name: "Operation Alpha",
  external_id: "C0001",
  description: "A curated campaign.",
  aliases: ["Alpha", "Op A"],
  curated: true,
  curated_slug: "c0001",
  techniques: [
    {
      technique_id: "T1059",
      technique_name: "Command and Scripting Interpreter",
      tactic: "execution",
      description: null,
    },
  ],
  actors: [{ stix_id: "intrusion-set--aaaa", name: "GroupA" }],
}

const analyticsFixture: StixPackageAnalytics = {
  version: "18.0",
  curated_total: 6,
  uncurated_total: 18,
  top_actors: [
    { stix_id: "intrusion-set--aaaa", name: "GroupA", campaigns_total: 4 },
    { stix_id: "intrusion-set--bbbb", name: "GroupB", campaigns_total: 2 },
  ],
  top_campaigns: [
    {
      campaign_key: "c0001",
      name: "Operation Alpha",
      curated: true,
      techniques_total: 31,
    },
    {
      campaign_key: "campaign--2222",
      name: "Operation Beta",
      curated: false,
      techniques_total: 12,
    },
  ],
  tactics: [
    { tactic: "execution", techniques_total: 40 },
    { tactic: "persistence", techniques_total: 25 },
  ],
  top_techniques: [
    { technique_id: "T1059", technique_name: "Command and Scripting Interpreter", campaigns_total: 5 },
    { technique_id: "T1105", technique_name: "Ingress Tool Transfer", campaigns_total: 3 },
  ],
}

describe("lib/stix — endpoint wrappers (new API surface)", () => {
  beforeEach(() => {
    getMock.mockReset()
    postMock.mockReset()
  })

  describe("getPackageOverview", () => {
    it("GETs /stix/bases/{version}/overview and returns the payload as-is", async () => {
      getMock.mockResolvedValueOnce(overviewFixture)

      const result = await getPackageOverview("18.0")

      expect(getMock).toHaveBeenCalledTimes(1)
      expect(getMock).toHaveBeenCalledWith("/stix/bases/18.0/overview")
      expect(result).toBe(overviewFixture)
    })

    it("percent-encodes a version containing reserved characters", async () => {
      getMock.mockResolvedValueOnce(overviewFixture)

      await getPackageOverview("17.1/rc 2")

      expect(getMock).toHaveBeenCalledWith(
        "/stix/bases/17.1%2Frc%202/overview",
      )
    })
  })

  describe("getCampaigns", () => {
    it("GETs /stix/bases/{version}/campaigns and returns the bare array (no unwrap)", async () => {
      getMock.mockResolvedValueOnce(campaignsFixture)

      const result = await getCampaigns("18.0")

      expect(getMock).toHaveBeenCalledTimes(1)
      expect(getMock).toHaveBeenCalledWith("/stix/bases/18.0/campaigns")
      // The endpoint returns a bare array; the wrapper must not reach for a
      // `.campaigns` property or otherwise reshape it.
      expect(result).toBe(campaignsFixture)
      expect(Array.isArray(result)).toBe(true)
    })

    it("percent-encodes the version segment", async () => {
      getMock.mockResolvedValueOnce(campaignsFixture)

      await getCampaigns("17.1/rc 2")

      expect(getMock).toHaveBeenCalledWith(
        "/stix/bases/17.1%2Frc%202/campaigns",
      )
    })
  })

  describe("getCampaignOverview", () => {
    it("GETs /stix/bases/{version}/campaigns/{campaign_key}/overview and returns the payload", async () => {
      getMock.mockResolvedValueOnce(campaignOverviewFixture)

      const result = await getCampaignOverview("18.0", "c0001")

      expect(getMock).toHaveBeenCalledTimes(1)
      expect(getMock).toHaveBeenCalledWith(
        "/stix/bases/18.0/campaigns/c0001/overview",
      )
      expect(result).toBe(campaignOverviewFixture)
    })

    it("percent-encodes both the version and the campaign key", async () => {
      getMock.mockResolvedValueOnce(campaignOverviewFixture)

      await getCampaignOverview("17.1/rc 2", "campaign--1111/x y")

      expect(getMock).toHaveBeenCalledWith(
        "/stix/bases/17.1%2Frc%202/campaigns/campaign--1111%2Fx%20y/overview",
      )
    })
  })

  describe("getPackageAnalytics", () => {
    it("GETs /stix/bases/{version}/analytics and returns the payload as-is", async () => {
      getMock.mockResolvedValueOnce(analyticsFixture)

      const result = await getPackageAnalytics("18.0")

      expect(getMock).toHaveBeenCalledTimes(1)
      expect(getMock).toHaveBeenCalledWith("/stix/bases/18.0/analytics")
      expect(result).toBe(analyticsFixture)
    })

    it("percent-encodes a version containing reserved characters", async () => {
      getMock.mockResolvedValueOnce(analyticsFixture)

      await getPackageAnalytics("17.1/rc 2")

      expect(getMock).toHaveBeenCalledWith(
        "/stix/bases/17.1%2Frc%202/analytics",
      )
    })
  })

  describe("downloadBase", () => {
    it("POSTs to /stix/bases/{version}/download", async () => {
      postMock.mockResolvedValueOnce({
        version: "18.0",
        name: "Enterprise ATT&CK v18.0",
        downloaded: true,
        selectable: true,
        file: "enterprise-attack-18.0.json",
      })

      await downloadBase("18.0")

      expect(postMock).toHaveBeenCalledTimes(1)
      expect(postMock).toHaveBeenCalledWith("/stix/bases/18.0/download")
    })

    it("percent-encodes the version segment", async () => {
      postMock.mockResolvedValueOnce({
        version: "17.1/rc 2",
        name: "n",
        downloaded: true,
        selectable: true,
      })

      await downloadBase("17.1/rc 2")

      expect(postMock).toHaveBeenCalledWith(
        "/stix/bases/17.1%2Frc%202/download",
      )
    })

    it("follows the background ingestion until the base is downloaded", async () => {
      postMock.mockResolvedValueOnce({ version: "18.0", downloaded: false, persisted: false })
      const base = { version: "18.0", name: "n", downloaded: true, selectable: true }
      getMock
        .mockResolvedValueOnce({ bases: [{ ...base, downloaded: false, selectable: false }] })
        .mockResolvedValueOnce([
          { source: "stix_packages", status: "running", error: null },
        ])
        .mockResolvedValueOnce({ bases: [base] })

      await expect(downloadBase("18.0", { intervalMs: 1 })).resolves.toEqual(base)
      expect(getMock).toHaveBeenCalledWith("/stix/bases")
      expect(getMock).toHaveBeenCalledWith("/ingestion/status")
    })

    it("rejects with the ingestion error when the stix_packages source fails", async () => {
      postMock.mockResolvedValueOnce({ version: "18.0", downloaded: false, persisted: false })
      getMock
        .mockResolvedValueOnce({
          bases: [{ version: "18.0", name: "n", downloaded: false, selectable: false }],
        })
        .mockResolvedValueOnce([
          { source: "stix_packages", status: "failed", error: "network: StixDownloadError" },
        ])

      await expect(downloadBase("18.0", { intervalMs: 1 })).rejects.toThrow(
        "network: StixDownloadError",
      )
    })
  })
})

describe("lib/stix — removed legacy exports", () => {
  // The design folds the old dashboard endpoint into `getPackageOverview` and
  // drops the unused adversaries endpoint. TypeScript would reject a direct
  // named import of removed symbols, so we assert their absence at runtime via
  // the namespace import.
  it("does not export getDashboard (folded into getPackageOverview)", () => {
    expect((stix as Record<string, unknown>).getDashboard).toBeUndefined()
  })

  it("does not export getAdversaries", () => {
    expect((stix as Record<string, unknown>).getAdversaries).toBeUndefined()
  })
})

/**
 * Property: the GET wrappers are faithful pass-throughs. For any payload the
 * HTTP client resolves with, the wrapper resolves with the deep-equal same
 * value and never drops, adds or transforms a field. This locks in the
 * counts/campaigns mapping contract (the overview's `counts` array and the
 * bare campaigns array survive untouched).
 * Validates: Requirements 6.1, 6.2
 */
describe("lib/stix — pass-through contract (property-based)", () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  const typeCountArb = fc.record({
    label: fc.string(),
    stix_type: fc.string(),
    count: fc.integer({ min: 0 }),
    description: fc.string(),
  })

  const overviewArb: fc.Arbitrary<StixPackageOverview> = fc.record({
    version: fc.string(),
    name: fc.string(),
    downloaded_at: fc.option(fc.string(), { nil: null }),
    counts: fc.array(typeCountArb),
    campaigns_total: fc.integer({ min: 0 }),
    actors_total: fc.integer({ min: 0 }),
    techniques_total: fc.integer({ min: 0 }),
    tactics_total: fc.integer({ min: 0 }),
    malware_total: fc.integer({ min: 0 }),
    relationships_total: fc.integer({ min: 0 }),
  })

  const campaignSummaryArb: fc.Arbitrary<StixCampaignSummary> = fc.record({
    campaign_key: fc.string(),
    stix_id: fc.string(),
    name: fc.string(),
    external_id: fc.option(fc.string(), { nil: null }),
    curated: fc.boolean(),
    curated_slug: fc.option(fc.string(), { nil: null }),
  })

  const analyticsArb: fc.Arbitrary<StixPackageAnalytics> = fc.record({
    version: fc.string(),
    curated_total: fc.integer({ min: 0 }),
    uncurated_total: fc.integer({ min: 0 }),
    top_actors: fc.array(
      fc.record({
        stix_id: fc.string(),
        name: fc.string(),
        campaigns_total: fc.integer({ min: 0 }),
      }),
    ),
    top_campaigns: fc.array(
      fc.record({
        campaign_key: fc.string(),
        name: fc.string(),
        curated: fc.boolean(),
        techniques_total: fc.integer({ min: 0 }),
      }),
    ),
    tactics: fc.array(
      fc.record({
        tactic: fc.string(),
        techniques_total: fc.integer({ min: 0 }),
      }),
    ),
    top_techniques: fc.array(
      fc.record({
        technique_id: fc.string(),
        technique_name: fc.option(fc.string(), { nil: null }),
        campaigns_total: fc.integer({ min: 0 }),
      }),
    ),
  })

  it("getPackageOverview round-trips any package overview payload faithfully", async () => {
    await fc.assert(
      fc.asyncProperty(overviewArb, async (payload) => {
        getMock.mockReset()
        getMock.mockResolvedValueOnce(payload)

        const result = await getPackageOverview("18.0")

        expect(result).toEqual(payload)
        expect(getMock).toHaveBeenCalledWith("/stix/bases/18.0/overview")
      }),
      { numRuns: 100 },
    )
  })

  it("getCampaigns returns the generated campaign array unchanged", async () => {
    await fc.assert(
      fc.asyncProperty(fc.array(campaignSummaryArb), async (payload) => {
        getMock.mockReset()
        getMock.mockResolvedValueOnce(payload)

        const result = await getCampaigns("18.0")

        expect(result).toEqual(payload)
        expect(result).toHaveLength(payload.length)
        expect(getMock).toHaveBeenCalledWith("/stix/bases/18.0/campaigns")
      }),
      { numRuns: 100 },
    )
  })

  it("getPackageAnalytics round-trips any analytics payload faithfully", async () => {
    await fc.assert(
      fc.asyncProperty(analyticsArb, async (payload) => {
        getMock.mockReset()
        getMock.mockResolvedValueOnce(payload)

        const result = await getPackageAnalytics("18.0")

        expect(result).toEqual(payload)
        expect(getMock).toHaveBeenCalledWith("/stix/bases/18.0/analytics")
      }),
      { numRuns: 100 },
    )
  })
})
