/**
 * Campaign display helpers.
 *
 * A campaign is addressed by a machine key (a curated slug such as
 * `outer_space`, or a STIX id) but is shown to the Researcher by its readable
 * name ("Outer Space"). These helpers never return an empty string and never
 * expose raw separators of a slug.
 */

/** Turn a slug such as `outer_space` into a readable `Outer Space`. */
export function humanizeSlug(slug: string): string {
  return slug
    .replace(/[_\-\s]+/g, " ")
    .trim()
    .split(" ")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ")
}

/**
 * The name to show for a campaign: its name when present, otherwise the
 * humanized slug, otherwise the raw slug. Never empty for a non-empty slug.
 */
export function campaignDisplayName(
  name: string | null | undefined,
  slug: string,
): string {
  const trimmed = name?.trim()
  if (trimmed) return trimmed
  return humanizeSlug(slug) || slug
}
