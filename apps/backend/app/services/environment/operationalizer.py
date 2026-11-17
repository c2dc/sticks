"""Operationalization seam: ``OperationalizerByOrigin`` + curation.

This module defines the **per-origin extension point** of the operationalization phase
(Operationalization) in the agentic pipeline. It separates *how* a Campaign is
operationalized (today by curation, reading the curated artifacts from disk;
tomorrow by an AI agent) from the *rest* of the system, so a ``gerado_ia``
implementation can be plugged in later **without** changing this signature (Req.
8.6, 16.2, 16.4), following the precedent of ``FlowGenerator`` (the flow phase) and
``EnvironmentProvisioner`` (the environment phase).

Dual Output (two **separate** typed artifacts — Req. 8.4, 8.7):

``ProducedOperationalization``
    The typed (Pydantic) result of the operationalization phase — carries **the ordered Abilities**
    (as a ``OperationalizationOutput``, with
    ``atomic_ordering`` and ``translation_source``) and **the
    ``EnvironmentParameters``** derived from the commands and techniques. The two
    artifacts live in distinct fields, never mixed.

``OperationalizerByOrigin``
    A :class:`typing.Protocol` ``@runtime_checkable`` with two operations —
    ``origin() -> TranslationSource`` and
    ``operationalize(campaign, flow) -> ProducedOperationalization``. Any object
    exposing the two signatures satisfies the protocol (structural duck typing),
    with no explicit inheritance.

The AI implementation is ``AiOperationalizer`` (`services/ai/agents/op_agent.py`).

_Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 11.3, 11.4, 16.2_
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from app.models.enums import TranslationSource
from app.services.emulation.views import OperationalizationOutput
from app.services.environment.operationalization import (
    EnvironmentParameters,
    RequiredService,
)
from app.services.flow.flow_view import FlowView

# ---------------------------------------------------------------------------
# Typed DTO of the operationalization phase output (task 5.2)
# ---------------------------------------------------------------------------


class ProducedOperationalization(BaseModel):
    """The operationalization phase output — the **two** separate typed artifacts (Req. 8.4, 8.7).

    the operationalization phase has a **dual** output: the Abilities ordered by
    the attack flow and the environment parameters derived from the commands. These
    live in **distinct** fields of this result, never mixed (Req. 8.7):

    Attributes:
        campaign_id: *Slug* of the operationalized Campaign.
        abilities: 1st artifact — the :class:`~app.services.emulation.views.OperationalizationOutput`
            with the Abilities (command per executor), the order
            (``atomic_ordering``), the Adversary and ``translation_source``
            (``curadoria_humana``) — (Req. 8.2, 8.5, 8.6). ``None`` when the operationalization is
            suppressed (no upstream technique — Req. 8.1).
        parameters: 2nd artifact — the
            :class:`~app.services.environment.operationalization.EnvironmentParameters`
            derived from the commands (hosts/services/``network_context``),
            destined for the environment phase (Req. 8.3, 8.4).
        origin: Operationalization origin — ``curadoria_humana`` today;
            extensible to ``gerado_ia`` without changing the model (Req. 8.6,
            16.2, 16.4).
    """

    campaign_id: str
    abilities: OperationalizationOutput | None = None
    parameters: EnvironmentParameters
    origin: TranslationSource = TranslationSource.HUMAN_CURATION


# ---------------------------------------------------------------------------
# Per-origin seam (Protocol) — the operationalization phase (task 5.2)
# ---------------------------------------------------------------------------


@runtime_checkable
class OperationalizerByOrigin(Protocol):
    """Per-**origin** operationalization phase seam — Req. 8.6, 16.2.

    A ``@runtime_checkable Protocol`` that defines the **extension point** of the
    operationalization, following the precedent of the other per-origin seams
    (``FlowGenerator`` in the flow phase, ``EnvironmentProvisioner`` in the environment phase): each
    origin (``curadoria_humana`` today, ``gerado_ia`` in the future) is an
    implementation interchangeable by injection, **without** redesign (Req. 16.2,
    16.4).

    Any object that exposes these operations satisfies the protocol — no explicit
    inheritance is required (structural duck typing):

    - ``origin()`` — the :class:`~app.models.enums.TranslationSource` this
      implementation stamps on the output (``ProducedOperationalization.origin``
      and ``EnvironmentParameters.origin``).
    - ``operationalize(campaign, flow)`` — produces the **two** typed operationalization phase
      artifacts (ordered Abilities + ``EnvironmentParameters``) from the
      Campaign's *slug* and the attack flow (flow phase output).

    The AI implementation is ``AiOperationalizer`` (Req. 16.2).
    """

    def origin(self) -> TranslationSource:
        """The :class:`~app.models.enums.TranslationSource` of this implementation."""
        ...

    def operationalize(self, campaign: str, flow: FlowView) -> ProducedOperationalization:
        """Produce the two operationalization phase artifacts (Abilities + ``EnvironmentParameters``).

        ``flow`` is the flow phase output (the chosen attack flow); the curation
        implementation uses its presence as a signal of the curated path and
        derives the parameters from the persisted data (no network, no AI —
        Req. 11.3, 11.4).
        """
        ...


# ---------------------------------------------------------------------------
# Pure service-inference heuristic from the commands (Req. 8.3)
# ---------------------------------------------------------------------------

# Patterns (regex) → readable description of the service/vulnerability to
# provision. Pure and deterministic; applied over the attacker commands and the
# target setup commands of a technique. The order is stable for a
# deterministic output.
_SERVICE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bflask\b", re.IGNORECASE), "servidor Flask (HTTP)"),
    (re.compile(r"\bport=\"?(\d+)\"?|:(\d{2,5})\b"), "serviço HTTP em porta exposta"),
    (re.compile(r"\bsshpass\b|\bssh\b", re.IGNORECASE), "acesso SSH"),
    (re.compile(r"\bsudo\b|\bNOPASSWD\b", re.IGNORECASE), "sudo sem senha"),
    (re.compile(r"\bnc\b|\bnetcat\b|/dev/tcp/", re.IGNORECASE), "listener/reverse shell (netcat)"),
    (re.compile(r"\bchmod \+s\b|setuid\(", re.IGNORECASE), "binário SUID (escalonamento)"),
    (re.compile(r"\bdnsmasq\b|\bdig\b|dnsutils", re.IGNORECASE), "serviço DNS"),
    (re.compile(r"docker\.sock", re.IGNORECASE), "socket Docker exposto"),
    (re.compile(r"\bxmrig\b", re.IGNORECASE), "minerador (xmrig)"),
)


def _infer_services(commands: list[str], technique: str | None) -> list[RequiredService]:
    """Infer services/vulnerabilities to provision from ``commands``.

    Pure and deterministic: matches each pattern of :data:`_SERVICE_PATTERNS`
    against the concatenated text of the commands (attacker commands + target
    setup commands of a technique) and returns one :class:`RequiredService` per
    unique matched pattern, stamping ``origin_technique`` with the technique's
    ``technique_id`` when available (Req. 8.3). It accesses neither network nor AI.
    """
    if not commands:
        return []
    text = "\n".join(commands)
    services: list[RequiredService] = []
    seen: set[str] = set()
    for pattern, description in _SERVICE_PATTERNS:
        if description in seen:
            continue
        if pattern.search(text):
            services.append(RequiredService(description=description, origin_technique=technique))
            seen.add(description)
    return services


__all__ = [
    "OperationalizerByOrigin",
    "ProducedOperationalization",
]
