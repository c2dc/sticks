"""Command_Base - consultable command index shared by attack flow and operationalization.

The Command_Base is the queryable command index that both the attack flow generation
agent and the operationalization agent consume (Req. 11.2, 12.1). It is composed
of two halves following the two-memory rule (see
:mod:`app.services.ai.memory`):

* **Exact half (relational).** The ``technique_commands`` table maps an ATT&CK
  ``technique_id`` to concrete commands, each carrying its executor, platform
  and provenance (``atomic_red_team`` | ``threat_intel``). These are the exact,
  factual mappings and are returned first (Req. 7.4, 12.1).
* **Similarity half (vector).** When no exact relational mapping exists for a
  technique, the Command_Base falls back to associative command affinities
  retrieved by similarity from the ``experience_memory`` pgvector store, via the
  Session-injected :class:`app.services.ai.memory.ExperienceMemory` (Req. 7.5,
  12.3).

:meth:`CommandBase.has_command_for` signals whether a technique has any backing
command at all (exact or by similarity) so the attack flow generation agent can flag a
gap instead of inventing an unsupported command (Req. 11.4).

This module performs no I/O at import time: the ORM model and the experience
memory are resolved lazily inside the methods, matching the conventions of the
other Session-injected services (Req. 1.7).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory

# Sentinel campaign id for technique-level (campaign-agnostic) retrieval.
# ``ExperienceMemory.retrieve`` excludes the running campaign from its results;
# the Command_Base queries affinities by technique rather than by campaign, so
# it passes a sentinel that cannot collide with any real campaign id, keeping
# every stored affinity eligible for the similarity fallback.
_NO_CAMPAIGN = "__command_base_no_campaign__"


@dataclass(frozen=True)
class CommandSuggestion:
    """One command suggested for a technique, exact or by similarity.

    ``exact`` distinguishes the two halves of the Command_Base: ``True`` means
    the suggestion came from the relational ``technique_commands`` mapping;
    ``False`` means it was recovered by similarity from the Experience_Memory.
    """

    technique_id: str
    command: str
    executor: str  # "sh" | "powershell" | "cmd" | ...
    platform: str  # "linux" | "windows" | ...
    source: str  # "atomic_red_team" | "threat_intel"
    exact: bool  # True = relational mapping; False = similarity
    # Primary key of the ``technique_commands`` row (exact suggestions only).
    id: int | None = None
    # Atomic input arguments ``{name: {default, ...}}`` (exact batch lookup only).
    input_arguments: dict[str, Any] | None = None


class CommandBase:
    """Queryable command index: exact relational mapping + similarity fallback.

    Session-injected like the other services; the Experience_Memory is injected
    too (or lazily constructed over the same session) so the similarity fallback
    shares the request-scoped unit of work.
    """

    def __init__(self, session: Session, *, memory: ExperienceMemory | None = None) -> None:
        self._session = session
        self._memory = memory

    # ---- public API --------------------------------------------------------

    def commands_for_technique(self, technique_id: str, *, k: int = 5) -> list[CommandSuggestion]:
        """Return commands backing a technique: exact first, else by similarity.

        Reads the ``technique_commands`` rows mapped to ``technique_id`` and maps
        them to :class:`CommandSuggestion` with ``exact=True`` (Req. 7.4, 12.1).
        When at least one exact mapping exists, only the exact suggestions are
        returned. Otherwise the method falls back to the Experience_Memory and
        returns up to ``k`` command affinities retrieved by similarity for the
        technique, each with ``exact=False`` (Req. 7.5, 12.3).
        """
        exact = self._exact_commands(technique_id)
        if exact:
            return exact
        return self._similar_commands(technique_id, k=k)

    def has_command_for(self, technique_id: str) -> bool:
        """True if the technique has at least one backing command.

        Returns ``True`` when there is at least one exact relational mapping OR
        at least one similarity suggestion for ``technique_id``; ``False`` only
        when neither half of the Command_Base can back the technique, letting the
        Attack flow generation agent flag the gap (Req. 11.4).
        """
        return bool(self.commands_for_technique(technique_id, k=1))

    def exact_commands_for(
        self, technique_ids: Sequence[str]
    ) -> dict[str, list[CommandSuggestion]]:
        """Batch exact lookup: one ``WHERE technique_id IN (...)`` query.

        Returns a mapping with one key per requested technique id; a technique
        with no row maps to an empty list. Never consults the Experience_Memory
        (Req. 6.2, 6.6).
        """
        from app.models.domain import TechniqueCommand

        unique_ids = list(dict.fromkeys(technique_ids))
        result: dict[str, list[CommandSuggestion]] = {tid: [] for tid in unique_ids}
        if not unique_ids:
            return result
        stmt = (
            select(TechniqueCommand)
            .where(TechniqueCommand.technique_id.in_(unique_ids))
            .order_by(TechniqueCommand.technique_id, TechniqueCommand.id)
        )
        for row in self._session.execute(stmt).scalars().all():
            result[row.technique_id].append(
                CommandSuggestion(
                    technique_id=row.technique_id,
                    command=row.command,
                    executor=row.executor,
                    platform=row.platform,
                    source=row.source,
                    exact=True,
                    id=row.id,
                    input_arguments=row.input_arguments,
                )
            )
        return result

    # ---- exact (relational) half -------------------------------------------

    def _exact_commands(self, technique_id: str) -> list[CommandSuggestion]:
        """Map ``technique_commands`` rows for the technique to suggestions."""
        # Imported lazily to avoid import-time I/O and circular imports with
        # app.models (Req. 1.7).
        from app.models.domain import TechniqueCommand

        stmt = (
            select(TechniqueCommand)
            .where(TechniqueCommand.technique_id == technique_id)
            .order_by(TechniqueCommand.id)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [
            CommandSuggestion(
                technique_id=row.technique_id,
                command=row.command,
                executor=row.executor,
                platform=row.platform,
                source=row.source,
                exact=True,
                id=row.id,
                input_arguments=row.input_arguments,
            )
            for row in rows
        ]

    # ---- similarity (vector) half ------------------------------------------

    def _similar_commands(self, technique_id: str, *, k: int) -> list[CommandSuggestion]:
        """Retrieve command affinities for the technique from Experience_Memory.

        Queries the Experience_Memory by the technique id and projects each hit
        into a :class:`CommandSuggestion` with ``exact=False`` (Req. 7.5, 12.3).
        Hits that carry no usable command payload are skipped. If the memory is
        unavailable it degrades to an empty set (handled inside
        :meth:`ExperienceMemory.retrieve`).
        """
        memory = self._resolve_memory()
        hits = memory.retrieve(campaign_id=_NO_CAMPAIGN, query=technique_id, k=k)

        suggestions: list[CommandSuggestion] = []
        for hit in hits:
            suggestion = self._hit_to_suggestion(technique_id, hit)
            if suggestion is not None:
                suggestions.append(suggestion)
        return suggestions

    def _resolve_memory(self) -> ExperienceMemory:
        """Return the injected Experience_Memory or build one over the session.

        The memory built here carries the Ollama embedder when the embedding model
        is installed; without it the similarity fallback would always degrade to an
        empty set because retrieval has nothing to embed the query with.
        """
        if self._memory is None:
            from app.services.ai.embedder import build_experience_embedder

            self._memory = ExperienceMemory(self._session, embedder=build_experience_embedder())
        return self._memory

    @staticmethod
    def _hit_to_suggestion(technique_id: str, hit: dict[str, Any]) -> CommandSuggestion | None:
        """Project a retrieval hit into a similarity :class:`CommandSuggestion`.

        Command affinities store the concrete command plus its executor/platform/
        source in the hit metadata; the human-readable ``content`` is used as a
        last-resort command text. Hits without any command payload are skipped so
        unrelated experience rows do not masquerade as commands.
        """
        metadata = hit.get("metadata") or {}

        command = metadata.get("command")
        if not command:
            content = hit.get("content")
            command = content if isinstance(content, str) and content.strip() else None
        if not command:
            return None

        return CommandSuggestion(
            technique_id=metadata.get("technique_id") or technique_id,
            command=command,
            executor=metadata.get("executor", ""),
            platform=metadata.get("platform", ""),
            source=metadata.get("source", "threat_intel"),
            exact=False,
        )
