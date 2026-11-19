import { useTranslation } from "react-i18next"

import type { AiFlowProposal } from "@/lib/ai"

/**
 * Provenance of one attack flow proposal: how it was obtained, which reference
 * flows were used, why, and how much the best documented flow overlaps the campaign.
 *
 * `proposal.coverage` is a 0..1 share (backend `FlowDecision.coverage`), so it is
 * multiplied by 100 and rounded for the `{{coverage}}%` wording.
 */
export function FlowProvenanceCard({ proposal }: { proposal: AiFlowProposal }) {
  const { t } = useTranslation("pipeline")
  const kind = proposal.kind
  const hasReferences = proposal.references.length > 0

  // The overlap is the similarity of the best documented flow to this campaign. With no
  // reference flow it is 0% and only repeats "no reference was used", so it is hidden.
  const showOverlap = hasReferences && typeof proposal.coverage === "number"

  return (
    <div
      className="flex flex-col gap-1.5 rounded-xl border bg-muted/30 p-4 text-sm"
      data-section="flow-provenance-card"
      data-proposal-id={proposal.proposal_id}
    >
      <p className="font-medium">{t("guided.provenance.title")}</p>
      <p data-section="flow-provenance-kind">
        {kind ? <span className="font-medium">{t(`guided.provenance.${kind}`, { defaultValue: kind })}</span> : null}
        {kind && proposal.reason_code ? " · " : null}
        {proposal.reason_code ? (
          <span className="text-muted-foreground" data-section="flow-provenance-reason">
            {t(`guided.reason.${proposal.reason_code}`, { defaultValue: proposal.reason_code })}
          </span>
        ) : null}
      </p>
      <div data-section="flow-provenance-references">
        {hasReferences ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-muted-foreground">{t("guided.provenance.references")}:</span>
            <ul className="flex flex-wrap gap-1.5">
              {proposal.references.map((id) => (
                <li key={id} className="rounded-md border bg-card px-2 py-0.5 font-mono text-xs">
                  #{id}
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="text-muted-foreground">{t("guided.provenance.noReferences")}</p>
        )}
      </div>
      {showOverlap ? (
        <p data-section="flow-provenance-coverage">
          {t("guided.provenance.coverage", { coverage: Math.round((proposal.coverage ?? 0) * 100) })}
        </p>
      ) : null}
    </div>
  )
}