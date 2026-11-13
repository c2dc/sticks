import { cleanup, screen, waitFor } from "@testing-library/react"
import { createElement } from "react"
import fc from "fast-check"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { OperationalizationScreen } from "@/components/OperationalizationScreen"
import { campaignDisplayName } from "@/lib/campaign"
import { renderWithI18n } from "@/test/render"

/**
 * Feature: agentic-only-flow-redesign, Property 17: Campaign display name
 * never leaks the slug.
 *
 * **Validates: Requirements 10.1, 10.2, 10.3**
 */

vi.mock("@/lib/operationalization", () => ({
  getOperationalization: vi.fn(),
}))

import { getOperationalization } from "@/lib/operationalization"

const getMock = vi.mocked(getOperationalization)

/** A slug with at least one underscore, e.g. `outer_space`. */
const slugArb = fc
  .array(fc.stringMatching(/^[a-z][a-z0-9]{0,7}$/), { minLength: 2, maxLength: 4 })
  .map((words) => words.join("_"))

/** A readable name that never contains an underscore. */
const nameArb = fc
  .array(fc.stringMatching(/^[A-Z][a-z]{1,7}$/), { minLength: 1, maxLength: 3 })
  .map((words) => words.join(" "))

describe("Property 17: campaignDisplayName never leaks the slug", () => {
  beforeEach(() => {
    getMock.mockReset()
  })
  afterEach(() => {
    cleanup()
  })

  it("returns the name when present, otherwise a non-empty humanized slug", () => {
    fc.assert(
      fc.property(
        slugArb,
        nameArb,
        fc.constantFrom(undefined, null, "", "   "),
        (slug, name, emptyName) => {
          expect(campaignDisplayName(name, slug)).toBe(name)

          const fallback = campaignDisplayName(emptyName, slug)
          expect(fallback.length).toBeGreaterThan(0)
          expect(fallback).not.toContain("_")
        },
      ),
      { numRuns: 100 },
    )
  })

  it("renders the display name, never the raw slug, in the Operationalization header", async () => {
    await fc.assert(
      fc.asyncProperty(
        slugArb,
        fc.option(nameArb, { nil: undefined }),
        async (slug, name) => {
          getMock.mockResolvedValue({
            campaign_key: slug,
            atomic_status: { status: "ready", loaded_count: null, expected_count: null },
            techniques: [],
          })
          renderWithI18n(
            createElement(OperationalizationScreen, {
              version: "18.0",
              campaignKey: slug,
              campaignName: name,
            }),
          )
          const header = await waitFor(() => screen.getByTestId("operationalization-campaign"))
          expect(header.textContent).toContain(campaignDisplayName(name, slug))
          expect(header.textContent).not.toContain(slug)
          cleanup()
        },
      ),
      { numRuns: 25 },
    )
  })
})
