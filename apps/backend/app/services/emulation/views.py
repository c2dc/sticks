"""Shared view types and constants used outside the curated pipeline.

These definitions were extracted verbatim from the removed curated phase
service so that modules unrelated to the curated pipeline (environment,
websocket channels, API, agents) can depend on them.
Behavior is unchanged.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from app.models.enums import AbilityResultStatus, AgentPhase, TranslationSource

# Execution phase — execution + measurement + report.
PHASE_EXECUTION: str = AgentPhase.EXECUTION.value


class OperationalizationExecutorView(BaseModel):
    """A single executor of a curated Ability — the concrete command (Req. 3.2)."""

    name: str | None = None
    platform: str | None = None
    command: str | None = None
    # Id of the ``technique_commands`` row of the template this command came
    # from; ``None`` when unknown (additive, Req. 12.10).
    command_ref: int | None = None


class OperationalizationAbilityView(BaseModel):
    """One curated Ability as shown in the operationalization phase Output (Req. 3.2).

    Carries the technique id, tactic and description, plus the concrete command
    of each executor. ``translation_source`` is the explicit, extensible
    translation source (Req. 11.1) — ``"curadoria_humana"`` for the 8 curated
    cases.
    """

    ability_id: str
    name: str | None = None
    technique_id: str | None = None
    technique_name: str | None = None
    tactic: str | None = None
    description: str | None = None
    executors: list[OperationalizationExecutorView] = Field(default_factory=list)
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION


class OperationalizationAdversaryView(BaseModel):
    """The Adversary grouping of Abilities shown in the operationalization phase Output (Req. 3.5).

    ``atomic_ordering`` is the ordered list of ability ids that composes the
    Adversary — the sequence/dependencies between Abilities (Req. 3.3).
    """

    id: str
    name: str | None = None
    description: str | None = None
    atomic_ordering: list[str] = Field(default_factory=list)
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION


class OperationalizationOutput(BaseModel):
    """Operationalization phase Output — the curated translation (Req. 3.2-3.5, 3.7, 3.8, 11.1).

    Holds the curated Abilities (each with the concrete command per executor),
    the ordering/dependencies (``atomic_ordering``), the Adversary grouping and
    the explicit translation source ``translation_source`` (Req. 11.1).

    Absence flags let the UI render the distinct states cleanly:

    * ``has_abilities`` is ``False`` — the case has no curated Abilities, so
      there is no operationalization phase Output (Req. 3.7); ``note`` explains it.
    * ``has_adversary`` is ``False`` — the case has no Adversary grouping, so the
      Abilities are not grouped into an Adversary (Req. 3.8);
      ``adversary_note`` explains it.

    ``curadoria_humana`` (``human_curation`` flag + textual ``translation_source``)
    is the explicit "human curation" indicator associated with the Output
    (Req. 3.4).
    """

    abilities: list[OperationalizationAbilityView] = Field(default_factory=list)
    ordering: list[str] = Field(default_factory=list)
    adversary: OperationalizationAdversaryView | None = None
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION
    human_curation: bool = True
    has_abilities: bool = True
    has_adversary: bool = True
    note: str | None = None
    adversary_note: str | None = None


class OperationAbilityResult(BaseModel):
    """The per-Ability result of the Operation (Req. 4.4).

    ``status`` is one of ``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``
    (:class:`~app.models.enums.AbilityResultStatus`); ``command_output`` is the
    executed command's output.
    """

    ability_id: str
    name: str | None = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    command_output: str | None = None


class LogLineView(BaseModel):
    """A provisioning log line carried over the phases channel (Req. 2.6).

    A single provisioning log line carried over the existing phase channel. Each
    line has a monotonic ``seq`` per case (so the UI can dedupe/reorder on
    reconnect), the pipeline ``step`` that emitted it, the ``stream`` it belongs
    to (``comando`` | ``stdout`` | ``stderr`` | ``info``), the ``text`` itself,
    and ``at`` — a naive-UTC timestamp consistent with the module's
    ``_utcnow`` helper (Req. 2.6).

    For the environment phase the view also carries ``subphase`` — ``"build_online"`` (Build online),
    ``"runtime_isolated"`` (Runtime isolated) or ``None`` for the inherited steps —, propagated
    from the executor's ``LogLine`` so the console can label the subphase
    next to the ``step`` (Req. 6.3/6.4). It defaults to ``None``, so the inherited
    lines and the other phases keep the exact same shape (a pure additive change)
    and the monotonic ``seq`` is preserved.
    """

    seq: int
    step: str
    stream: str
    text: str
    at: dt.datetime
    subphase: str | None = None


__all__ = [
    "PHASE_EXECUTION",
    "LogLineView",
    "OperationAbilityResult",
    "OperationalizationAbilityView",
    "OperationalizationAdversaryView",
    "OperationalizationExecutorView",
    "OperationalizationOutput",
]
