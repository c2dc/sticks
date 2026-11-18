import type { ReactNode } from "react"

/**
 * Renders an ATT&CK description for display. MITRE descriptions use markdown
 * links (`[OilRig](https://attack.mitre.org/groups/G0049)`) and inline
 * citation markers (`(Citation: ESET OilRig Campaigns Sep 2023)`). Links become
 * anchors that open in a new tab; citation markers are removed because the
 * source list is not shown here.
 */
const CITATION = /\s*\(Citation:[^)]*\)/g
const LINK = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g

export function formatAttackDescription(text: string): ReactNode[] {
  const clean = text.replace(CITATION, "")
  const nodes: ReactNode[] = []
  let last = 0
  for (const match of clean.matchAll(LINK)) {
    const start = match.index ?? 0
    if (start > last) nodes.push(clean.slice(last, start))
    nodes.push(
      <a
        key={`${start}-${match[2]}`}
        href={match[2]}
        target="_blank"
        rel="noopener noreferrer"
        className="underline underline-offset-2 hover:text-foreground"
      >
        {match[1]}
      </a>,
    )
    last = start + match[0].length
  }
  if (last < clean.length) nodes.push(clean.slice(last))
  return nodes
}
