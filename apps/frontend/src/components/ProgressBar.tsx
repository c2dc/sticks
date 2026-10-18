import { cn } from "@/lib/utils"

/**
 * Minimal accessible progress bar (0–100). Used both for per-stage processing
 * progress (Req. 1.5) and for the aggregate replication progress (Req. 5.5).
 */
export function ProgressBar({
  value,
  label,
  className,
  indicatorClassName,
}: {
  value: number
  label?: string
  className?: string
  indicatorClassName?: string
}) {
  const clamped = Math.max(0, Math.min(100, Math.round(value)))

  return (
    <div
      className={cn("h-2 w-full overflow-hidden rounded-full bg-muted", className)}
      role="progressbar"
      aria-valuenow={clamped}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <div
        className={cn(
          "h-full rounded-full bg-team-blue transition-[width] duration-500",
          indicatorClassName,
        )}
        style={{ width: `${clamped}%` }}
      />
    </div>
  )
}
