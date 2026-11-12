import { cn } from "@/lib/utils"

/**
 * OrchestratorAvatar — the friendly orchestrator robot that conducts the AI Run.
 *
 * It is the visual anchor of the guided Run screen: a small multi-agent
 * "conductor" that speaks to the Researcher, reveals each phase (attack flow,
 * operationalization, environment) and asks permission to proceed. The avatar
 * is a self-contained inline SVG (no external asset) so it renders instantly,
 * scales crisply and adapts to Modo_Claro / Modo_Escuro via `currentColor` and
 * the team palette.
 *
 * `mood` nudges the expression so the robot can feel like it is thinking,
 * speaking, celebrating a pass or flagging a problem — purely cosmetic.
 * `pulse` adds a soft ambient animation (disabled for reduced-motion by the
 * caller simply not passing it).
 */
export type OrchestratorMood = "idle" | "thinking" | "talking" | "happy" | "alert"

export function OrchestratorAvatar({
  mood = "idle",
  pulse = false,
  className,
}: {
  mood?: OrchestratorMood
  pulse?: boolean
  className?: string
}) {
  // Accent ring colour per mood, mapped to the team palette.
  const ring =
    mood === "alert"
      ? "text-team-red"
      : mood === "happy"
        ? "text-team-blue"
        : "text-team-purple"

  return (
    <div
      className={cn(
        "relative inline-flex items-center justify-center",
        className,
      )}
      data-section="orchestrator-avatar"
      data-mood={mood}
      aria-hidden
    >
      {/* Soft ambient glow behind the robot. */}
      <span
        className={cn(
          "absolute inset-0 rounded-full blur-xl opacity-30",
          ring,
          pulse && "animate-pulse",
        )}
        style={{ backgroundColor: "currentColor" }}
      />
      <svg
        viewBox="0 0 96 96"
        className={cn("relative size-full", pulse && "animate-[float_3s_ease-in-out_infinite]")}
        role="img"
      >
        {/* Antenna */}
        <line x1="48" y1="10" x2="48" y2="22" stroke="currentColor" strokeWidth="3" className={ring} strokeLinecap="round" />
        <circle cx="48" cy="9" r="4" className={cn("fill-current", ring)} />

        {/* Head capsule */}
        <rect
          x="20"
          y="22"
          width="56"
          height="46"
          rx="16"
          className="fill-card stroke-current text-team-purple"
          strokeWidth="3"
        />

        {/* Visor */}
        <rect x="27" y="30" width="42" height="26" rx="11" className="fill-background" />

        {/* Eyes — shape varies with mood. */}
        {mood === "happy" ? (
          <>
            <path d="M34 43 q5 -7 10 0" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" className="text-team-blue" />
            <path d="M52 43 q5 -7 10 0" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" className="text-team-blue" />
          </>
        ) : mood === "alert" ? (
          <>
            <circle cx="39" cy="43" r="4.5" className="fill-current text-team-red" />
            <circle cx="57" cy="43" r="4.5" className="fill-current text-team-red" />
          </>
        ) : (
          <>
            <circle cx="39" cy="43" r="4.5" className={cn("fill-current", mood === "thinking" ? "text-team-purple" : "text-team-blue")}>
              {mood === "talking" || mood === "thinking" ? (
                <animate attributeName="opacity" values="1;0.4;1" dur="1.4s" repeatCount="indefinite" />
              ) : null}
            </circle>
            <circle cx="57" cy="43" r="4.5" className={cn("fill-current", mood === "thinking" ? "text-team-purple" : "text-team-blue")}>
              {mood === "talking" || mood === "thinking" ? (
                <animate attributeName="opacity" values="1;0.4;1" dur="1.4s" begin="0.2s" repeatCount="indefinite" />
              ) : null}
            </circle>
          </>
        )}

        {/* Mouth / speaker grille */}
        {mood === "talking" ? (
          <rect x="42" y="60" width="12" height="4" rx="2" className="fill-current text-team-purple">
            <animate attributeName="width" values="6;14;6" dur="0.7s" repeatCount="indefinite" />
            <animate attributeName="x" values="45;41;45" dur="0.7s" repeatCount="indefinite" />
          </rect>
        ) : (
          <rect x="43" y="60" width="10" height="3" rx="1.5" className="fill-current text-muted-foreground opacity-60" />
        )}

        {/* Side ears */}
        <rect x="14" y="38" width="6" height="14" rx="3" className="fill-current text-team-purple" />
        <rect x="76" y="38" width="6" height="14" rx="3" className="fill-current text-team-purple" />

        {/* Neck + body hint */}
        <rect x="42" y="68" width="12" height="6" className="fill-current text-team-purple opacity-70" />
        <path
          d="M28 90 q0 -14 20 -14 q20 0 20 14"
          className="fill-card stroke-current text-team-purple"
          strokeWidth="3"
        />
      </svg>
    </div>
  )
}
