// Helpers for the Locale_Catalog guard test (`catalog-guard.test.ts`): key
// flattening, unused-key detection and legacy phase wording detection. Value-only
// (no i18next, no React), run against the raw locale JSON.

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

/** The i18next interpolation delimiters used across the catalog. */
const PLACEHOLDER_PATTERN = /\{\{\s*([^{}]+?)\s*\}\}/g

/** The i18next plural-variant suffixes recognized by the guard. */
const PLURAL_SUFFIXES = ["_one", "_other"] as const
type PluralSuffix = (typeof PLURAL_SUFFIXES)[number]

/**
 * Parse an i18next `_one` / `_other` plural suffix off the final segment of a
 * key path. When the path's last segment ends in a recognized suffix, returns
 * the `base` path (same path with the suffix stripped) and the matched
 * `suffix`; otherwise returns the path unchanged with `suffix: null`.
 *
 *  */
interface ParsedPlural {
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

// ---------------------------------------------------------------------------
// Unused-key detection (agentic-only-flow-redesign, task 42.1, Req. 9.1)
// ---------------------------------------------------------------------------

/**
 * Flatten a locale resource object into its sorted, de-duplicated list of
 * fully-qualified dot-joined leaf keys. Returns an empty list for an empty
 * object.
 */
export function flattenKeys(value: JsonValue): string[] {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return []
  }
  return Array.from(new Set(collectLeafPaths(value))).sort()
}

// One pattern per quote type, scanned independently so a stray apostrophe in
// JSX text or a comment cannot desynchronize the other quote types. Single and
// double quoted literals are line-bounded; template literals may span lines.
const STRING_LITERAL_PATTERNS: ReadonlyArray<{ quote: string; pattern: RegExp }> = [
  { quote: '"', pattern: /"((?:\\.|[^"\\\n])*)"/g },
  { quote: "'", pattern: /'((?:\\.|[^'\\\n])*)'/g },
  { quote: "`", pattern: /`((?:\\[\s\S]|[^`\\])*)`/g },
]

const BLOCK_COMMENT_PATTERN = /\/\*[\s\S]*?\*\//g
const LINE_COMMENT_PATTERN = /(^|[^:"'`\\])\/\/.*$/gm

function stripComments(source: string): string {
  return source
    .replace(BLOCK_COMMENT_PATTERN, "")
    .replace(LINE_COMMENT_PATTERN, "$1")
}

interface KeyReferences {
  /** Complete string literals (no interpolation) found in the sources. */
  literals: Set<string>
  /** Static prefixes of dynamically composed keys (end with `.`). */
  prefixes: Set<string>
}

/**
 * Extract the key references from a set of source texts. A literal counts as a
 * reference as-is (and without a leading `ns:` namespace qualifier). A
 * template literal containing `${` contributes its static prefix when that
 * prefix ends with a `.`, so `` `aiRun.status.${s}` `` references every key
 * under `aiRun.status.`.
 */
function extractKeyReferences(sources: string[]): KeyReferences {
  const literals = new Set<string>()
  const prefixes = new Set<string>()
  for (const rawSource of sources) {
    const source = stripComments(rawSource)
    for (const { quote, pattern } of STRING_LITERAL_PATTERNS) {
      for (const match of source.matchAll(pattern)) {
        const body = match[1]
        const interpolationAt = quote === "`" ? body.indexOf("${") : -1
        if (interpolationAt >= 0) {
          const prefix = body.slice(0, interpolationAt)
          const afterNamespace = prefix.includes(":")
            ? prefix.slice(prefix.indexOf(":") + 1)
            : prefix
          if (afterNamespace.endsWith(".")) {
            prefixes.add(afterNamespace)
          }
          continue
        }
        literals.add(body)
        if (body.includes(":")) {
          literals.add(body.slice(body.indexOf(":") + 1))
        }
      }
    }
  }
  return { literals, prefixes }
}

/** Numbered-phase wording inherited from the curated pipeline (Req. 9.4). */
export const LEGACY_PHASE_WORDING_PATTERN = /\b(stage|etapa)\b/i // sweep-allow: legacy-wording detector

/**
 * Return the `[key, value]` pairs of a locale resource whose string value
 * contains Legacy_Phase_Wording (numbered or generic legacy terms for a phase).
 * Placeholders such as `{{phase}}` are ignored since they sit inside values
 * as interpolation names, not as rendered copy.
 */
export function findLegacyPhaseWording(
  catalog: JsonValue,
): Array<[string, string]> {
  const offenders: Array<[string, string]> = []
  for (const key of flattenKeys(catalog)) {
    const value = getByPath(catalog, key)
    if (typeof value !== "string") {
      continue
    }
    const rendered = value.replace(PLACEHOLDER_PATTERN, "")
    if (LEGACY_PHASE_WORDING_PATTERN.test(rendered)) {
      offenders.push([key, value])
    }
  }
  return offenders
}

/**
 * Return the keys that no source references: neither literally, nor through
 * the static prefix of a dynamically composed key, nor (for plural variants)
 * through the base key without the `_one` / `_other` suffix. The result is
 * sorted.
 */
export function findUnusedKeys(keys: string[], sources: string[]): string[] {
  const { literals, prefixes } = extractKeyReferences(sources)
  const isUsed = (key: string): boolean => {
    const { base } = parsePluralSuffix(key)
    if (literals.has(key) || literals.has(base)) {
      return true
    }
    for (const prefix of prefixes) {
      if (key.startsWith(prefix)) {
        return true
      }
    }
    return false
  }
  return keys.filter((key) => !isUsed(key)).sort()
}
