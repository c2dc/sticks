import { useTranslation } from "react-i18next"
import {
  CheckCircle2,
  CircleDashed,
  Loader2,
  Lock,
  Sparkles,
  XCircle,
} from "lucide-react"

import { cn } from "@/lib/utils"
import type { EstadoEstagio } from "@/lib/casos"

/**
 * Per-stage state indicator (Req. 1.4). Renders exactly one of the four states
 * — nao_iniciado / em_andamento / concluido / erro — each visually distinct,
 * with the error state clearly set apart from the others (Req. 1.6). A blocked
 * flag renders a distinct locked style (Req. 5.4).
 *
 * When `estado === "concluido"` and `preConcluido` is set, the badge switches to
 * the "pré-concluído (curado)" adornment (Req. 2.1, 7.4): a distinct icon and
 * team-blue color variation set it apart from the "concluded by you" state, with
 * the `state.preConcluido` label (and matching aria-label) and
 * `data-state="pre_concluido"`. `preConcluido` is ignored for every other state.
 */
export function StageStateBadge({
  estado,
  bloqueado = false,
  preConcluido = false,
  compact = false,
  className,
}: {
  estado: EstadoEstagio
  bloqueado?: boolean
  preConcluido?: boolean
  /** Icon-only mode: hides the text label (kept as aria-label/title). */
  compact?: boolean
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
        {!compact && t("state.bloqueado")}
      </span>
    )
  }

  // Pré-concluído (curado): only takes effect when the stage is "concluido".
  // For every other state `preConcluido` is ignored, so existing states keep
  // their behavior (no regression — Req. 7.4).
  if (estado === "concluido" && preConcluido) {
    const label = t("state.preConcluido")
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
          "border-team-blue/40 bg-team-blue/10 text-team-blue",
          className,
        )}
        data-state="pre_concluido"
        aria-label={label}
      >
        <Sparkles className="size-3.5" aria-hidden />
        {!compact && label}
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
      {!compact && t(`state.${estado}`)}
    </span>
  )
}
