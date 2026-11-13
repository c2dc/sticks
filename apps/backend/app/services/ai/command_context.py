"""Agent command context: what the operationalization agent is shown.

The context carries the **Command_Template** of each candidate (placeholders
intact, byte for byte) together with its Atomic arguments, so the agent can
adapt the command to the emulated campaign environment. It never carries an
Example_Command: nothing here calls ``resolve_command`` (Req. 12.2-12.4).

Everything in this module is pure: no I/O, no database, no network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.containment.subnets import (
    INTERNAL_SUBNET_CIDRS,
    STATIC_IP_CONTAINERS,
)

__all__ = [
    "CandidateContext",
    "build_agent_command_context",
    "render_campaign_facts",
    "render_candidates",
]


@dataclass(frozen=True)
class CandidateContext:
    """One candidate command shown to the agent."""

    command_ref: int | None
    template: str
    platform: str | None
    executor: str | None
    # name -> {"description", "type", "default"} or ``None`` when only the
    # placeholder name is known (similarity candidates).
    arguments: dict[str, dict[str, Any] | None] = field(default_factory=dict)


def build_agent_command_context(pool: list[dict[str, Any]]) -> list[CandidateContext]:
    """Project the technique command pool into candidate contexts.

    The template is taken from the pool untouched; ``input_arguments`` is copied
    as is (``None``/missing becomes an empty mapping). Entries without a command
    are skipped.
    """
    candidates: list[CandidateContext] = []
    for entry in pool:
        template = entry.get("command")
        if not isinstance(template, str) or not template.strip():
            continue
        raw_args = entry.get("input_arguments")
        arguments: dict[str, dict[str, Any] | None] = {}
        if isinstance(raw_args, Mapping):
            for name, spec in raw_args.items():
                arguments[str(name)] = dict(spec) if isinstance(spec, Mapping) else None
        command_ref = entry.get("command_ref")
        candidates.append(
            CandidateContext(
                command_ref=command_ref if isinstance(command_ref, int) else None,
                template=template,
                platform=entry.get("platform"),
                executor=entry.get("name"),
                arguments=arguments,
            )
        )
    return candidates


def render_candidates(candidates: list[CandidateContext]) -> str:
    """Render the candidates as prompt text (template verbatim + arguments)."""
    blocks: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        lines = [
            f"Candidate {index} "
            f"(platform: {candidate.platform or 'unknown'}, "
            f"executor: {candidate.executor or 'unknown'})",
            "template:",
            candidate.template,
        ]
        if candidate.arguments:
            lines.append("arguments (the default is only a suggestion):")
            for name, spec in candidate.arguments.items():
                if spec is None:
                    lines.append(f"- {name}: no metadata available")
                else:
                    lines.append(f"- {name}: {json.dumps(spec, ensure_ascii=False)}")
        else:
            lines.append("arguments: none")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_campaign_facts(
    campaign: str | None,
    flow_name: str | None,
    nodes: list[dict[str, Any]],
) -> str:
    """Render only the environment facts known when operationalizing.

    These are the campaign slug and flow name, the flow nodes, and the lab
    containment rules (internal subnets and the fixed container addresses). No
    other topology is invented here; the environment phase owns the rest.
    """
    lines = [
        f"Campaign: {campaign or 'unknown'}",
        f"Flow: {flow_name or 'unknown'}",
        "Flow nodes:",
    ]
    for node in nodes:
        lines.append(
            f"- {node.get('technique_id') or '?'} "
            f"{node.get('technique_name') or ''} "
            f"(tactic: {node.get('tactic') or 'unknown'})".replace("  ", " ")
        )
    lines.append(
        "Containment rules: every destination must be an IPv4 literal in one of "
        "these internal subnets: " + ", ".join(INTERNAL_SUBNET_CIDRS) + "."
    )
    lines.append(
        "Known lab containers by address: "
        + ", ".join(f"{ip}={name}" for ip, name in STATIC_IP_CONTAINERS.items())
        + "."
    )
    return "\n".join(lines)
