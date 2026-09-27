import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

/**
 * Placeholder pipeline overview (Req. 1). Shows the three stages side by side,
 * each with labeled Entrada / Processamento / Saída sections. The real,
 * data-driven view (states, aggregate progress, live WebSocket updates) is
 * implemented in task 12.
 */
const STAGES = [
  { id: 1, name: "Estágio 1 — Modelagem Estrutural" },
  { id: 2, name: "Estágio 2 — Tradução Curada" },
  { id: 3, name: "Estágio 3 — Emulação de Adversário" },
] as const

const SECTIONS = ["Entrada", "Processamento", "Saída"] as const

export function PipelineOverview() {
  return (
    <section className="grid gap-4 md:grid-cols-3">
      {STAGES.map((stage) => (
        <Card key={stage.id}>
          <CardHeader>
            <CardTitle className="text-base">{stage.name}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {SECTIONS.map((section) => (
              <div
                key={section}
                className="rounded-md border border-dashed p-3 text-sm text-muted-foreground"
              >
                {section}
              </div>
            ))}
          </CardContent>
        </Card>
      ))}
    </section>
  )
}
