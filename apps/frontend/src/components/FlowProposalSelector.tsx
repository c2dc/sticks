import { useState } from "react"
import { Check, RefreshCw } from "lucide-react"
import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import { Popover } from "@/components/ui/popover"
import type { AiFlowProposal } from "@/lib/ai"
import { cn } from "@/lib/utils"

/**
 * Compact attack flow attempt selector: the current attempt number (click to pick
 * another one) and an icon button that generates another attack flow. The icon is
 * disabled, with a hover hint, once `maxProposals` attempts exist.
 */
export interface FlowProposalSelectorProps {
  proposals: AiFlowProposal[]
  maxProposals: number
  onSelect: (proposalId: number) => void
  onRegenerate: () => void
  /** Blocks both controls while a request is in flight. */
  busy?: boolean
}

export function FlowProposalSelector({
  proposals,
  maxProposals,
  onSelect,
  onRegenerate,
  busy = false,
}: FlowProposalSelectorProps) {
  const { t } = useTranslation("pipeline")
  const [open, setOpen] = useState(false)
  if (proposals.length === 0) return null

  const current = proposals.find((proposal) => proposal.current) ?? proposals[proposals.length - 1]
  const limitReached = maxProposals > 0 && proposals.length >= maxProposals
  const regenerateLabel = t("guided.generateAnotherFlow")

  return (
    <div className="flex items-center gap-1" data-section="flow-proposal-selector">
      <div className="relative">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="font-mono"
          disabled={busy}
          aria-label={t("guided.history.selectAriaLabel")}
          aria-haspopup="dialog"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          #{current.proposal_id}
        </Button>
        <Popover
          open={open}
          onOpenChange={setOpen}
          aria-label={t("guided.history.selectAriaLabel")}
          className="w-auto min-w-20 p-1"
        >
          <ul className="flex flex-col gap-0.5">
            {proposals.map((proposal) => (
              <li key={proposal.proposal_id}>
                <button
                  type="button"
                  className={cn(
                    "flex w-full items-center justify-between gap-3 rounded-md px-2 py-1 font-mono text-sm hover:bg-accent",
                    proposal.current && "bg-team-purple/10 text-team-purple",
                  )}
                  aria-current={proposal.current ? "true" : undefined}
                  onClick={() => {
                    setOpen(false)
                    if (!proposal.current) onSelect(proposal.proposal_id)
                  }}
                >
                  #{proposal.proposal_id}
                  {proposal.current ? <Check className="size-3.5" aria-hidden /> : null}
                </button>
              </li>
            ))}
          </ul>
        </Popover>
      </div>
      {/* The hint lives on a wrapper: a disabled button does not receive pointer events. */}
      <span title={limitReached ? t("guided.history.limitReached") : regenerateLabel}>
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="h-8 w-8"
          disabled={busy || limitReached}
          aria-label={regenerateLabel}
          onClick={onRegenerate}
        >
          <RefreshCw aria-hidden />
        </Button>
      </span>
    </div>
  )
}
