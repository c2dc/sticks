import { useTranslation } from "react-i18next"

import type { AiOperationalizedTechnique } from "@/lib/ai"

/** One objection raised by the environment agent against a technique. */
export interface NegotiationObjection {
  techniqueId: string
  /** Emulability reason code (`no_command`, `port_conflict`, ...). */
  reasonCode: string
}

/**
 * Derives objections from the typed review: the API has no dedicated negotiation
 * payload yet, so every `not_emulable` technique with a reason code counts as one.
 */
export function objectionsFromTechniques(
  techniques: AiOperationalizedTechnique[],
): NegotiationObjection[] {
  return techniques
    .filter((tech) => tech.emulability_status === "not_emulable" && tech.emulability_reason_code)
    .map((tech) => ({ techniqueId: tech.technique_id, reasonCode: tech.emulability_reason_code as string }))
}

/**
 * Negotiation summary. `rounds` comes from the caller (null hides the round line);
 * `objections` can come from {@link objectionsFromTechniques} or a richer payload later.
 */
export function NegotiationSummary({
  rounds,
  objections,
}: {
  rounds?: number | null
  objections: NegotiationObjection[]
}) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1.5 rounded-xl border bg-muted/30 p-4 text-sm" data-section="negotiation-summary">
      <p className="font-medium">{t("guided.negotiation.title")}</p>
      {typeof rounds === "number" ? (
        <p className="text-muted-foreground">{t("guided.negotiation.rounds", { rounds })}</p>
      ) : null}
      {objections.length === 0 ? (
        <p className="text-muted-foreground">{t("guided.negotiation.noObjections")}</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {objections.map((objection) => (
            <li key={`${objection.techniqueId}:${objection.reasonCode}`}>
              <span className="mr-1.5 font-mono text-xs">{objection.techniqueId}</span>
              {t("guided.negotiation.objection", {
                reason: t(`guided.emulability.reason.${objection.reasonCode}`, {
                  defaultValue: objection.reasonCode,
                }),
              })}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
