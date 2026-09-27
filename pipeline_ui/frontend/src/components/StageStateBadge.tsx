import { useTranslation } from "react-i18next"
import { CheckCircle2, CircleDashed, Loader2, Lock, XCircle } from "lucide-react"

import { cn } from "@/lib/utils"
import type { EstadoEstagio } from "@/lib/casos"

/**
 * Per-stage state indicator (Req. 1.4). Renders exactly one of the four states
 * — nao_iniciado / em_andamento / concluido / erro — each visually distinct,
 * with the error state clearly set apart from the others (Req. 1.6). A blocked
 * flag renders a distinct locked style (Req. 5.4).
 */
export function StageStateBadge({
  estado,
  bloqueado = false,
  className,
}: {
  estado: EstadoEstagio
  bloqueado?: boolean
  className?: string
}) {
  const { t } = useTranslation("pipeline")

  if (bloqueado) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
          "border-border bg-muted text-muted-foreground",
          className,
        )}
        data-state="bloqueado"
        aria-label={t("state.bloqueado")}
      >
        <Lock className="size-3.5" aria-hidden />
        {t("state.bloqueado")}
      </span>
    )
  }

  const config: Record<
    EstadoEstagio,
    { icon: typeof CheckCircle2; classes: string; spin?: boolean }
  > = {
    nao_iniciado: {
      icon: CircleDashed,
      classes: "border-border bg-muted text-muted-foreground",
    },
    em_andamento: {
      icon: Loader2,
      classes: "border-team-blue/40 bg-team-blue/10 text-team-blue",
      spin: true,
    },
    concluido: {
      icon: CheckCircle2,
      classes: "border-team-purple/40 bg-team-purple/10 text-team-purple",
    },
    // Error state is visually distinct from the others (Req. 1.6).
    erro: {
      icon: XCircle,
      classes: "border-team-red/50 bg-team-red/15 text-team-red",
    },
  }

  const { icon: Icon, classes, spin } = config[estado]

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        classes,
        className,
      )}
      data-state={estado}
      aria-label={t(`state.${estado}`)}
    >
      <Icon className={cn("size-3.5", spin && "animate-spin")} aria-hidden />
      {t(`state.${estado}`)}
    </span>
  )
}
