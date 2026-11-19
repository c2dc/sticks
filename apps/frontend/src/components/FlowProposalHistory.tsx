import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import type { AiFlowProposal } from "@/lib/ai"
import { cn } from "@/lib/utils"

/**
 * Attack flow proposals shown in the run. The current one is marked; every other
 * one can be selected through `onSelect(proposal_id)`. `disabled` blocks the
 * buttons while a request is in flight.
 */
export function FlowProposalHistory({
  proposals,
  onSelect,
  disabled = false,
}: {
  proposals: AiFlowProposal[]
  onSelect: (proposalId: number) => void
  disabled?: boolean
}) {
  const { t } = useTranslation("pipeline")
  if (proposals.length === 0) return null

  return (
    <div className="flex flex-col gap-2 rounded-xl border bg-muted/30 p-4 text-sm" data-section="flow-proposal-history">
      <p className="font-medium">{t("guided.history.title")}</p>
      <p className="text-xs text-muted-foreground">{t("guided.history.hint")}</p>
      <ul className="flex flex-col gap-1.5">
        {proposals.map((proposal) => (
          <li
            key={proposal.proposal_id}
            className={cn(
              "flex items-center justify-between gap-3 rounded-lg border bg-card px-3 py-1.5",
              proposal.current && "border-team-purple/40 bg-team-purple/5",
            )}
            data-proposal-id={proposal.proposal_id}
            data-current={proposal.current}
          >
            <span className="font-mono text-xs">
              #{proposal.proposal_id}
              {proposal.flow_id != null ? ` · flow ${proposal.flow_id}` : ""}
            </span>
            {proposal.current ? (
              <span className="text-xs font-medium text-team-purple">{t("guided.history.current")}</span>
            ) : (
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={disabled}
                onClick={() => onSelect(proposal.proposal_id)}
              >
                {t("guided.history.use")}
              </Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
