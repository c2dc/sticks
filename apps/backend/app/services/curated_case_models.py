"""Typed, in-memory models of a curated case (no disk access).

These Pydantic models describe the curated content that is persisted in the
database (``campaigns``, ``abilities``, ``adversaries`` and ``attack_flows``
rows) and rebuilt by :mod:`app.services.curated_case_reader`. Field names mirror
the ORM models in :mod:`app.models.domain`, so the mapping is 1:1.

This module performs no I/O of any kind: no disk, no network, no database.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import TranslationSource


class Executor(BaseModel):
    """A single executor of an Ability: a concrete command on a platform.

    Mirrors the objects inside ``Ability.executors`` (JSON list of
    ``{name, platform, command}``).
    """

    name: Optional[str] = None
    platform: Optional[str] = None
    command: Optional[str] = None


class AbilityData(BaseModel):
    """One curated Ability, aligned with the ``Ability`` ORM model.

    ``executors`` is kept as a list of typed :class:`Executor`; it corresponds to
    the ORM ``executors`` JSON column.
    """

    ability_id: str
    name: Optional[str] = None
    tactic: Optional[str] = None
    technique_name: Optional[str] = None
    technique_id: Optional[str] = None
    description: Optional[str] = None
    executors: list[Executor] = Field(default_factory=list)


class AdversaryData(BaseModel):
    """The curated Adversary, aligned with the ``Adversary`` ORM model.

    ``atomic_ordering`` is the ordered list of ability ids that composes the
    adversary/campaign.
    """

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class FlowNode(BaseModel):
    """A structural node of the attack flow input.

    Only the identity and technique fields are typed. Any other key found in
    persisted content (for example legacy ``parent_nodes``/``child_nodes``,
    ``provides`` or ``attacker_commands``) is ignored on read.
    """

    model_config = ConfigDict(extra="ignore")

    node_id: Optional[str] = None
    node_type: Optional[str] = None
    technique_id: Optional[str] = None
    technique_name: Optional[str] = None
    tactic: Optional[str] = None


class FlowData(BaseModel):
    """The structural graph input rebuilt from ``attack_flows.content``.

    Exposes the campaign name and the structural nodes. Any other key of
    legacy content (``edges`` included, which is read elsewhere) is ignored.
    """

    model_config = ConfigDict(extra="ignore")

    campaign_name: Optional[str] = None
    structural_nodes: list[FlowNode] = Field(default_factory=list)

class CaseData(BaseModel):
    """Full result for one curated case, assembled from the database.

    Bundles the case metadata (slug, display name and the explicit/extensible
    translation source), the curated Abilities, the Adversary with its
    ``atomic_ordering``, and the attack flow input.
    """

    id: str  # slug, e.g. "shadowray"
    name: str  # display name, e.g. "ShadowRay"
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION

    abilities: list[AbilityData] = Field(default_factory=list)
    adversary: Optional[AdversaryData] = None
    flow: Optional[FlowData] = None

    @property
    def atomic_ordering(self) -> list[str]:
        """Convenience accessor for the adversary's ordering."""
        return self.adversary.atomic_ordering if self.adversary else []

    @property
    def has_abilities(self) -> bool:
        """Whether this case has any curated Abilities.

        ``False`` means "no abilities": the absence of an Output, not an error.
        """
        return len(self.abilities) > 0


__all__ = [
    "AbilityData",
    "AdversaryData",
    "CaseData",
    "Executor",
    "FlowData",
    "FlowNode",
]
