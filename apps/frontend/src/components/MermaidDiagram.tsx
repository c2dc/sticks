import { useEffect, useId, useRef, useState } from "react"

import { useTheme } from "@/components/theme-provider"

/**
 * Renders a Mermaid diagram from a definition string.
 *
 * Mermaid is loaded lazily (dynamic import) so its sizeable bundle is only
 * fetched when a diagram actually needs to be drawn. The diagram re-renders
 * when the definition or the app theme changes, so it stays legible in both
 * Modo_Claro and Modo_Escuro. Rendering is sandboxed to this component's own
 * container and never executes diagram-embedded scripts.
 */
export function MermaidDiagram({
  definition,
  className,
}: {
  definition: string
  className?: string
}) {
  const { theme } = useTheme()
  const containerRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  // Mermaid node ids must be unique and start with a letter.
  const rawId = useId().replace(/[^a-zA-Z0-9]/g, "")
  const renderId = `mermaid-${rawId}`

  useEffect(() => {
    let cancelled = false

    async function render() {
      try {
        const mermaid = (await import("mermaid")).default
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict", // never run scripts embedded in the definition
          theme: theme === "escuro" ? "dark" : "default",
          flowchart: { htmlLabels: true, useMaxWidth: true },
        })
        const { svg } = await mermaid.render(renderId, definition)
        if (!cancelled && containerRef.current) {
          containerRef.current.innerHTML = svg
          setError(null)
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Falha ao renderizar o diagrama.")
        }
      }
    }

    void render()
    return () => {
      cancelled = true
    }
  }, [definition, theme, renderId])

  if (error) {
    // Graceful fallback: show the diagram source so the information is not lost.
    return (
      <pre className="overflow-auto rounded-md bg-muted p-2 text-xs text-muted-foreground">
        {definition}
      </pre>
    )
  }

  return <div ref={containerRef} className={className} aria-label="Diagrama de rede" />
}
