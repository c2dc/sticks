import { useTranslation } from "react-i18next"

import type { AiCommandParameter } from "@/lib/ai"
import { cn } from "@/lib/utils"

const SOURCE_STYLES: Record<string, string> = {
  agent_inferred: "border-team-purple/40 bg-team-purple/10",
  atomic_default: "border-team-blue/40 bg-team-blue/10",
  unresolved: "border-team-red/40 bg-team-red/10",
}

/** Chip for one command parameter, colored by where its value came from. */
export function ParameterChip({ parameter }: { parameter: AiCommandParameter }) {
  const { t } = useTranslation("pipeline")
  const label = t(`guided.operationalization.parameterSource.${parameter.source}`, {
    defaultValue: parameter.source,
  })
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs",
        SOURCE_STYLES[parameter.source] ?? "bg-muted",
      )}
      title={label}
      data-section="parameter-chip"
      data-source={parameter.source}
    >
      <span className="font-mono font-medium">{parameter.name}</span>
      <span className="truncate font-mono text-muted-foreground">{parameter.value ?? "—"}</span>
      <span className="shrink-0 opacity-70">· {label}</span>
    </span>
  )
}
