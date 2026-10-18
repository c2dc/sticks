import type { ReactNode } from "react"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { cn } from "@/lib/utils"

/**
 * A labelled, visually-separated stage panel — one of Entrada / Processamento /
 * Saída (Req. 1.2). Shared by Stage1View / Stage2View / Stage3View so each
 * stage renders its three sections consistently.
 *
 * `kind` maps to the blue/red/purple team palette accent used on the panel
 * heading: Entrada leans blue (defensive/source), Processamento purple
 * (combined), Saída neutral by default — Stage3View overrides Saída to red for
 * the offensive emulation result.
 */
export type StageSectionKind = "blue" | "red" | "purple" | "neutral"

const ACCENT: Record<StageSectionKind, string> = {
  blue: "text-team-blue",
  red: "text-team-red",
  purple: "text-team-purple",
  neutral: "text-muted-foreground",
}

export function StageSection({
  label,
  kind = "neutral",
  description,
  children,
  className,
  "data-section": dataSection,
}: {
  label: string
  kind?: StageSectionKind
  description?: ReactNode
  children: ReactNode
  className?: string
  "data-section"?: string
}) {
  return (
    <Card className={cn("flex flex-col", className)} data-section={dataSection}>
      <CardHeader className="gap-1 pb-3">
        <CardTitle
          className={cn(
            "text-xs font-semibold uppercase tracking-wide",
            ACCENT[kind],
          )}
        >
          {label}
        </CardTitle>
        {description ? (
          <CardDescription className="text-xs">{description}</CardDescription>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3 pt-0 text-sm">
        {children}
      </CardContent>
    </Card>
  )
}

/** A muted "empty / absent" note used for absence states across the views. */
export function EmptyNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
      {children}
    </p>
  )
}
