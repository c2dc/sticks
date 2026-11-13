import { describe, expect, it } from "vitest"
import fc from "fast-check"

import { campaignDisplayName, humanizeSlug } from "@/lib/campaign"

/** Campaign display name helpers (Req. 10.1, 10.2, 10.3, 10.9). */
describe("campaignDisplayName", () => {
  it("returns the name when present", () => {
    expect(campaignDisplayName("Outer Space", "outer_space")).toBe("Outer Space")
  })

  it("trims the name", () => {
    expect(campaignDisplayName("  Outer Space  ", "outer_space")).toBe("Outer Space")
  })

  it.each([undefined, null, "", "   "])("falls back to the humanized slug for name %j", (name) => {
    expect(campaignDisplayName(name, "outer_space")).toBe("Outer Space")
  })

  it("humanizes dashes and underscores", () => {
    expect(humanizeSlug("shadow-ray_v2")).toBe("Shadow Ray V2")
  })

  it("falls back to the raw slug when humanizing yields nothing", () => {
    expect(campaignDisplayName(null, "___")).toBe("___")
  })

  it("never returns an empty string and never shows a slug separator when the name is absent", () => {
    fc.assert(
      fc.property(
        fc.stringMatching(/^[a-z0-9]+([_-][a-z0-9]+)*$/),
        fc.constantFrom(undefined, null, "", "  "),
        (slug, name) => {
          const shown = campaignDisplayName(name, slug)
          expect(shown.length).toBeGreaterThan(0)
          expect(shown).not.toMatch(/[_-]/)
        },
      ),
    )
  })
})
