"""Shared view types and constants used outside the curated pipeline.

These definitions were extracted verbatim from the removed curated stage
service so that modules unrelated to the curated pipeline (environment,
websocket channels, API, agents) can depend on them.
Behavior is unchanged.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import AbilityResultStatus, TranslationSource

# Stage 6 — Execution + measurement + report (role of the execution stage).
STAGE_EXECUTION: int = 6


class Stage2ExecutorView(BaseModel):
    """A single executor of a curated Ability — the concrete command (Req. 3.2)."""

    name: Optional[str] = None
    platform: Optional[str] = None
    command: Optional[str] = None


class Stage2AbilityView(BaseModel):
    """One curated Ability as shown in the Stage-2 Output (Req. 3.2).

    Carries the technique id, tactic and description, plus the concrete command
    of each executor. ``translation_source`` is the explicit, extensible
    translation source (Req. 11.1) — ``"curadoria_humana"`` for the 8 curated
    cases.
    """

    ability_id: str
    name: Optional[str] = None
    technique_id: Optional[str] = None
    technique_name: Optional[str] = None
    tactic: Optional[str] = None
    description: Optional[str] = None
    executors: list[Stage2ExecutorView] = Field(default_factory=list)
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION


class Stage2AdversaryView(BaseModel):
    """The Adversary grouping of Abilities shown in the Stage-2 Output (Req. 3.5).

    ``atomic_ordering`` is the ordered list of ability ids that composes the
    Adversary — the sequence/dependencies between Abilities (Req. 3.3).
    """

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION


class Stage2Output(BaseModel):
    """Stage 2 Output — the curated translation (Req. 3.2–3.5, 3.7, 3.8, 11.1).

    Holds the curated Abilities (each with the concrete command per executor),
    the ordering/dependencies (``atomic_ordering``), the Adversary grouping and
    the explicit translation source ``translation_source`` (Req. 11.1).

    Absence flags let the UI render the distinct states cleanly:

    * ``has_abilities`` is ``False`` — the case has no curated Abilities, so
      there is no Stage-2 Output (Req. 3.7); ``note`` explains it.
    * ``has_adversary`` is ``False`` — the case has no Adversary grouping, so the
      Abilities are not grouped into an Adversary (Req. 3.8);
      ``adversary_note`` explains it.

    ``curadoria_humana`` (``human_curation`` flag + textual ``translation_source``)
    is the explicit "human curation" indicator associated with the Output
    (Req. 3.4).
    """

    abilities: list[Stage2AbilityView] = Field(default_factory=list)
    ordering: list[str] = Field(default_factory=list)
    adversary: Optional[Stage2AdversaryView] = None
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION
    human_curation: bool = True
    has_abilities: bool = True
    has_adversary: bool = True
    note: Optional[str] = None
    adversary_note: Optional[str] = None


class Stage3AbilityResult(BaseModel):
    """The per-Ability result of the Operation (Req. 4.4).

    ``status`` is one of ``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``
    (:class:`~app.models.enums.AbilityResultStatus`); ``command_output`` is the
    executed command's output.
    """

    ability_id: str
    name: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    command_output: Optional[str] = None


class LogLineView(BaseModel):
    """A provisioning log line carried over the Stages channel (Req. 2.6).

    A single provisioning log line carried over the existing stage channel. Each
    line has a monotonic ``seq`` per case (so the UI can dedupe/reorder on
    reconnect), the pipeline ``step`` that emitted it, the ``stream`` it belongs
    to (``comando`` | ``stdout`` | ``stderr`` | ``info``), the ``text`` itself,
    and ``at`` — a naive-UTC timestamp consistent with the module's
    ``_utcnow`` helper (Req. 2.6).

    For Stage 5 the view also carries ``substage`` — ``"5.1"`` (Build online),
    ``"5.2"`` (Runtime isolated) or ``None`` for the inherited steps —, propagated
    from the executor's ``LogLine`` so the console can label the sub-stage
    next to the ``step`` (Req. 6.3/6.4). It defaults to ``None``, so the inherited
    lines and the other stages keep the exact same shape (a pure additive change)
    and the monotonic ``seq`` is preserved.
    """

    seq: int
    step: str
    stream: str
    text: str
    at: dt.datetime
    substage: Optional[str] = None


__all__ = [
    "STAGE_EXECUTION",
    "LogLineView",
    "Stage2AbilityView",
    "Stage2AdversaryView",
    "Stage2ExecutorView",
    "Stage2Output",
    "Stage3AbilityResult",
]
