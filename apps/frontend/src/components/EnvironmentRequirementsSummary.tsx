import { useTranslation } from "react-i18next"

import type { AiEnvironmentRequirements } from "@/lib/ai"

/** Compact view of what one technique needs from the lab: hosts, ports, services, files. */
export function EnvironmentRequirementsSummary({
  requirements,
}: {
  requirements: AiEnvironmentRequirements
}) {
  const { t } = useTranslation("pipeline")
  const tags = [
    ...requirements.hosts.map((host) => ({
      key: `host:${host.role}:${host.os}`,
      text: [host.role, host.os, host.image_hint].filter(Boolean).join(" · "),
    })),
    ...requirements.ports.map((port) => ({ key: `port:${port}`, text: `:${port}` })),
    ...requirements.services.map((service) => ({ key: `service:${service}`, text: service })),
    ...requirements.files.map((file) => ({ key: `file:${file}`, text: file })),
  ]
  if (tags.length === 0) return null

  return (
    <div className="flex flex-col gap-1.5" data-section="environment-requirements-summary">
      <p className="text-xs text-muted-foreground">{t("guided.operationalization.environmentRequirements")}</p>
      <ul className="flex flex-wrap gap-1.5">
        {tags.map((tag) => (
          <li key={tag.key} className="rounded-md border bg-muted/50 px-2 py-0.5 font-mono text-xs">
            {tag.text}
          </li>
        ))}
      </ul>
    </div>
  )
}
