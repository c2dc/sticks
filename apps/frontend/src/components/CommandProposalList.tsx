import { useTranslation } from "react-i18next"

import { ParameterChip } from "@/components/ParameterChip"
import type { AiCommandProposal } from "@/lib/ai"

/** Commands proposed for one technique: collapsible template, final command, parameters. */
export function CommandProposalList({ commands }: { commands: AiCommandProposal[] }) {
  const { t } = useTranslation("pipeline")
  return (
    <ul className="flex flex-col gap-3" data-section="command-proposal-list">
      {commands.map((command, index) => (
        <li
          key={command.command_ref ?? `command-${index}`}
          className="flex flex-col gap-2 rounded-lg border bg-card p-3"
          data-command-ref={command.command_ref}
        >
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">
              {t("guided.operationalization.template")}
            </summary>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded-md bg-muted/50 p-2 font-mono text-xs">
              {command.template}
            </pre>
          </details>
          <div>
            <p className="text-xs text-muted-foreground">{t("guided.operationalization.resolvedCommand")}</p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded-md bg-muted/50 p-2 font-mono text-xs">
              {command.resolved_command}
            </pre>
          </div>
          {command.parameters.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {command.parameters.map((parameter) => (
                <ParameterChip key={parameter.name} parameter={parameter} />
              ))}
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  )
}
