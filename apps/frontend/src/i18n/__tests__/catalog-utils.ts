// Shared leaf-path / placeholder / plural-suffix helpers for the i18n catalog
// property and parity tests (i18n-key-english-standardization, task 1.2).
//
// This module centralizes the `collectLeafPaths` / `getByPath` path-collection
// style that was previously duplicated inline across the parity tests
// (`pipeline-i18n-parity.property.test.ts`, `airun-i18n-parity.property.test.ts`,
// etc.). It is NOT a new property-based-testing library: the repo already uses
// `fast-check`, and the catalog property tests drive these pure helpers. The
// helpers are value-only (no i18next, no React) so they can run against the raw
// locale JSON before, during, and after the key migration.
//
// The three capabilities this module exposes map directly to the design's
// correctness properties:
//   - leaf-path collection / resolution  -> Property 1, 2, 4, 5, 8 (Req. 5.1)
//   - placeholder extraction             -> Property 6 (Req. 2.1)
//   - plural-suffix parsing              -> Property 7 (Req. 2.2, 2.3)

/** A JSON value as loaded from a locale resource file. */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonObject
  | JsonValue[]

export interface JsonObject {
  [key: string]: JsonValue
}

/**
 * Collect every fully-qualified leaf path (dot-joined) of a nested JSON object.
 * A leaf is any non-object value (strings, numbers, booleans, null). This gives
 * a stable, order-independent view of the key tree that can be compared between
 * locales. An empty `prefix` scopes to the whole object; a non-empty `prefix`
 * namespaces the collected paths.
 */
export function collectLeafPaths(value: JsonValue, prefix = ""): string[] {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return [prefix]
  }
  const paths: string[] = []
  for (const key of Object.keys(value)) {
    const next = prefix ? `${prefix}.${key}` : key
    paths.push(...collectLeafPaths((value as JsonObject)[key], next))
  }
  return paths
}

/**
 * Resolve a dot-joined path to its leaf value within a JSON object, or
 * `undefined` if any segment along the path is absent or crosses a non-object.
 */
export function getByPath(obj: JsonValue, path: string): JsonValue | undefined {
  let current: JsonValue | undefined = obj
  for (const segment of path.split(".")) {
    if (
      current === null ||
      typeof current !== "object" ||
      Array.isArray(current)
    ) {
      return undefined
    }
    current = (current as JsonObject)[segment]
  }
  return current
}

/**
 * Collect the sorted, de-duplicated set of fully-qualified leaf paths for a
 * single namespace/locale resource object. Optionally scope to a subtree by
 * passing `subtree` (e.g. "aiRun"), in which case the returned paths are still
 * fully qualified (they include the subtree prefix).
 *
 * Returns an empty array when the requested subtree does not exist, so callers
 * can assert presence explicitly rather than throwing.
 */
export function collectNamespaceLeafPaths(
  namespace: JsonValue,
  subtree?: string,
): string[] {
  const root = subtree ? getByPath(namespace, subtree) : namespace
  if (root === undefined) {
    return []
  }
  const prefix = subtree ?? ""
  return Array.from(new Set(collectLeafPaths(root, prefix))).sort()
}

/** The i18next interpolation delimiters used across the catalog. */
const PLACEHOLDER_PATTERN = /\{\{\s*([^{}]+?)\s*\}\}/g

/**
 * Extract every `{{...}}` interpolation placeholder from a value, in the order
 * they appear. Each entry records the placeholder's trimmed inner name and its
 * ordinal position (0-based index among the placeholders in the value). This
 * supports the placeholder-conservation property (Property 6): the names,
 * ordinal positions, and count must be identical before and after a rename.
 *
 * Non-string values yield an empty list (they carry no placeholders).
 */
export interface Placeholder {
  /** The trimmed token inside the delimiters, e.g. `versao` for `{{versao}}`. */
  name: string
  /** 0-based ordinal position among the placeholders in the value. */
  position: number
}

export function extractPlaceholders(value: JsonValue): Placeholder[] {
  if (typeof value !== "string") {
    return []
  }
  const placeholders: Placeholder[] = []
  let position = 0
  for (const match of value.matchAll(PLACEHOLDER_PATTERN)) {
    placeholders.push({ name: match[1], position })
    position += 1
  }
  return placeholders
}

/** The i18next plural-variant suffixes recognized by this migration. */
export const PLURAL_SUFFIXES = ["_one", "_other"] as const
export type PluralSuffix = (typeof PLURAL_SUFFIXES)[number]

/**
 * Parse an i18next `_one` / `_other` plural suffix off the final segment of a
 * key path. When the path's last segment ends in a recognized suffix, returns
 * the `base` path (same path with the suffix stripped) and the matched
 * `suffix`; otherwise returns the path unchanged with `suffix: null`.
 *
 * This supports the plural-suffix retention/grouping property (Property 7): all
 * variants of one base must map to a single renamed base differing only by the
 * preserved suffix.
 */
export interface ParsedPlural {
  /** The key path with any plural suffix stripped from its final segment. */
  base: string
  /** The matched plural suffix, or `null` when the path carries none. */
  suffix: PluralSuffix | null
}

export function parsePluralSuffix(path: string): ParsedPlural {
  for (const suffix of PLURAL_SUFFIXES) {
    if (path.endsWith(suffix)) {
      return { base: path.slice(0, -suffix.length), suffix }
    }
  }
  return { base: path, suffix: null }
}
