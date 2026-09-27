"""Command/destination preview generation (task 4.6) — security core.

Fourth stage of the ``ContainmentValidator``. This module produces the
**complete, concrete list of commands with their target container(s)** that the
confirmation modal (``EmulationConfirmModal``) shows the Pesquisador *before any
confirmation* (Req. 6.5 / design "(d) Preview").

Scope boundary (important):

- The preview **presents**, it does not **enforce**. It never starts, aborts or
  mutates an Operation, and it does not perform the pre-flight refusal itself —
  that is the OperationRunner (task 7) using the task 4.2 verdict. The preview
  merely *flags* any external destination so the modal can surface the violation
  alongside the commands.
- Pure Python, **no network access**. It composes the existing pieces rather than
  reimplementing them:

  * :mod:`app.services.containment.command_parser` (task 4.1) parses each command
    into its :class:`~app.services.containment.command_parser.Destination` list.
  * :mod:`app.services.containment.subnet_validator` (task 4.2) classifies each
    destination as internal/external and yields the typed
    :class:`~app.services.containment.subnet_validator.AbilityContainmentReport`.
  * :mod:`app.services.containment.subnets` (task 4.4 constants source) resolves
    an internal destination IP to its concrete lab container via
    :func:`~app.services.containment.subnets.container_for_ip`.

Container resolution
--------------------
Each internal destination IP is mapped to the container it targets using the
compose static-IP layout (``STATIC_IP_CONTAINERS`` in :mod:`.subnets`):

- 172.20.0.10 = caldera, 172.20.0.20 = kali
- 172.21.0.10 = kali,    172.21.0.20 = nginx
- 172.22.0.10 = nginx,   172.22.0.20 = db

An external destination has no lab container (``container=None``) and is flagged
via :attr:`PreviewDestination.is_external`. A command with **no destination**
(purely local, e.g. ``echo '...'``, ``cat /etc/shadow`` executed in place) has no
per-destination row; its target is represented by the executing host/container
context (:attr:`CommandPreview.local_target`) so the modal can still show a row
instead of crashing on an empty list.

_Requisitos: 6.5_
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.services.containment.command_parser import (
    CommandDestinations,
    Destination,
    parse_command,
)
from app.services.containment.subnets import container_for_ip
from app.services.containment.subnet_validator import (
    AbilityContainmentReport,
    ClassifiedDestination,
    CommandClassification,
    DestinationClass,
    validate_command_destinations,
)

#: Label used as the target of a purely local command (no outbound destination).
#: The concrete executing container is chosen by the caller (the executor's agent
#: host); when unknown we present a neutral, non-crashing placeholder so the modal
#: always has something to show for the command.
DEFAULT_LOCAL_TARGET = "host executor (comando local)"


@dataclass(frozen=True)
class PreviewDestination:
    """One destination of a command, resolved to its target container.

    Attributes:
        value: The bare host/IP the parser reduced the destination to
            (e.g. ``"172.21.0.20"`` for ``http://172.21.0.20:5055/exec``).
        raw: The original token the destination came from (URL, ``user@host``…),
            preserved for display in the modal.
        classification: INTERNAL or EXTERNAL, from task 4.2.
        container: The resolved lab container name for an internal, statically
            mapped IP (e.g. ``"nginx"``); ``None`` for external destinations or
            internal IPs without a static assignment.
    """

    value: str
    raw: str
    classification: DestinationClass
    container: str | None

    @property
    def is_external(self) -> bool:
        """True when this destination lies outside the internal lab subnets."""
        return self.classification is DestinationClass.EXTERNAL

    @property
    def target_label(self) -> str:
        """Human-readable target for the modal (``"nginx (172.21.0.20)"`` style).

        Mirrors the ``AuditLogEntry.container_destino`` convention in the design
        (e.g. ``"nginx (172.21.0.20)"``). External destinations are labelled as
        such since they resolve to no lab container.
        """
        if self.is_external:
            return f"destino externo ({self.raw})"
        if self.container is not None:
            return f"{self.container} ({self.value})"
        # Internal subnet IP with no known static container assignment.
        return f"container interno ({self.value})"


@dataclass(frozen=True)
class CommandPreview:
    """The preview of a single concrete command and its target(s).

    Attributes:
        command: The concrete command string, unchanged.
        destinations: One :class:`PreviewDestination` per detected destination,
            in parser order. Empty for a purely local command.
        local_target: Target context for a command with no destination (purely
            local). ``None`` when the command has at least one destination.
    """

    command: str
    destinations: list[PreviewDestination] = field(default_factory=list)
    local_target: str | None = None

    @property
    def is_local(self) -> bool:
        """True when the command has no outbound destination (runs in place)."""
        return not self.destinations

    @property
    def has_external(self) -> bool:
        """True when any destination of the command is external (flagged)."""
        return any(dest.is_external for dest in self.destinations)

    @property
    def target_containers(self) -> list[str]:
        """Distinct resolved internal container names this command targets.

        First-seen order; excludes external destinations and unresolved IPs.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for dest in self.destinations:
            if dest.container is not None and dest.container not in seen:
                seen.add(dest.container)
                ordered.append(dest.container)
        return ordered

    @property
    def target_labels(self) -> list[str]:
        """Per-destination target labels for the modal.

        For a local command returns a single-element list with the local target
        so the modal never renders an empty target column.
        """
        if self.is_local:
            return [self.local_target or DEFAULT_LOCAL_TARGET]
        return [dest.target_label for dest in self.destinations]


@dataclass(frozen=True)
class AbilityPreview:
    """The preview of every command of a single Ability.

    Attributes:
        ability_id: Identifier of the Ability (``None`` when previewing a bare
            command list without Ability context).
        ability_name: Human-readable Ability name (``None`` when unknown).
        commands: One :class:`CommandPreview` per executor command, in order.
    """

    ability_id: str | None
    ability_name: str | None
    commands: list[CommandPreview] = field(default_factory=list)

    @property
    def has_external(self) -> bool:
        """True when any command of the Ability reaches an external destination.

        Presentation-only flag so the modal can highlight the offending Ability;
        enforcement/refusal remains the OperationRunner's responsibility.
        """
        return any(cmd.has_external for cmd in self.commands)


@dataclass(frozen=True)
class EmulationPreview:
    """Complete preview for the confirmation modal: every Ability and command.

    Attributes:
        abilities: One :class:`AbilityPreview` per Ability, in order.
    """

    abilities: list[AbilityPreview] = field(default_factory=list)

    @property
    def has_external(self) -> bool:
        """True when any Ability of the preview carries an external destination."""
        return any(ability.has_external for ability in self.abilities)

    @property
    def commands(self) -> list[CommandPreview]:
        """Flattened list of every command preview across all Abilities."""
        return [cmd for ability in self.abilities for cmd in ability.commands]


# ---------------------------------------------------------------------------
# Building the preview from already-classified reports (reuses task 4.2).
# ---------------------------------------------------------------------------


def _preview_destination(classified: ClassifiedDestination) -> PreviewDestination:
    """Resolve one classified destination to a :class:`PreviewDestination`.

    Internal destinations are resolved to their static lab container via
    :func:`container_for_ip`; external ones carry no container.
    """
    dest: Destination = classified.destination
    container = (
        container_for_ip(dest.value)
        if classified.classification is DestinationClass.INTERNAL
        else None
    )
    return PreviewDestination(
        value=dest.value,
        raw=dest.raw,
        classification=classified.classification,
        container=container,
    )


def _preview_command(
    classification: CommandClassification,
    *,
    local_target: str | None,
) -> CommandPreview:
    """Turn one task-4.2 :class:`CommandClassification` into a command preview."""
    destinations = [_preview_destination(c) for c in classification.classified]
    return CommandPreview(
        command=classification.command,
        destinations=destinations,
        # Only populate a local target when there is no destination at all.
        local_target=(local_target or DEFAULT_LOCAL_TARGET) if not destinations else None,
    )


def build_ability_preview(
    report: AbilityContainmentReport,
    *,
    local_target: str | None = None,
) -> AbilityPreview:
    """Build an :class:`AbilityPreview` from a task-4.2 containment report.

    Use this when the caller already classified the Ability's commands (the
    preview and the pre-flight can then share a single parse+classify pass).

    Args:
        report: The :class:`AbilityContainmentReport` from task 4.2.
        local_target: Optional executing-container context to display for purely
            local commands (defaults to :data:`DEFAULT_LOCAL_TARGET`).

    Returns:
        An :class:`AbilityPreview` with a per-command, per-destination breakdown.
    """
    commands = [
        _preview_command(cmd, local_target=local_target) for cmd in report.commands
    ]
    return AbilityPreview(
        ability_id=report.ability_id,
        ability_name=report.ability_name,
        commands=commands,
    )


# ---------------------------------------------------------------------------
# Public entry points (parse -> classify -> preview), mirroring task 4.2.
# ---------------------------------------------------------------------------


def preview_command_destinations(
    command_destinations: Iterable[CommandDestinations],
    *,
    ability_id: str | None = None,
    ability_name: str | None = None,
    local_target: str | None = None,
) -> AbilityPreview:
    """Preview a batch of already-parsed commands for a single Ability.

    Reuses :func:`validate_command_destinations` (task 4.2) for classification so
    the internal/external flag and the container resolution stay consistent with
    the enforcement path.

    Args:
        command_destinations: Parsed commands (``CommandDestinations``).
        ability_id: Optional Ability identifier for the preview.
        ability_name: Optional Ability name for the preview.
        local_target: Optional context label for purely local commands.

    Returns:
        An :class:`AbilityPreview`.
    """
    report = validate_command_destinations(
        command_destinations, ability_id=ability_id, ability_name=ability_name
    )
    return build_ability_preview(report, local_target=local_target)


def preview_commands(
    commands: Sequence[str],
    *,
    ability_id: str | None = None,
    ability_name: str | None = None,
    local_target: str | None = None,
) -> AbilityPreview:
    """Parse, classify and preview raw command strings for a single Ability.

    Args:
        commands: Concrete shell command strings.
        ability_id: Optional Ability identifier for the preview.
        ability_name: Optional Ability name for the preview.
        local_target: Optional context label for purely local commands.

    Returns:
        An :class:`AbilityPreview` for the Ability's commands.
    """
    parsed = [parse_command(command) for command in commands]
    return preview_command_destinations(
        parsed,
        ability_id=ability_id,
        ability_name=ability_name,
        local_target=local_target,
    )


def _ability_attr(ability: object, key: str) -> object | None:
    """Read ``key`` from an Ability-like object OR a raw JSON dict.

    The preview accepts both the SQLAlchemy :class:`app.models.domain.Ability`
    (attribute access) and the raw parsed JSON of ``data/api/{caso}_dag-ability``
    (a ``dict``), so callers can preview straight off the curated files without
    first materializing ORM rows.
    """
    if isinstance(ability, dict):
        return ability.get(key)
    return getattr(ability, key, None)


def _ability_commands(ability: object) -> list[str]:
    """Extract concrete executor commands from an Ability-like object.

    Accepts the SQLAlchemy :class:`app.models.domain.Ability` (``executors`` is a
    list of ``{name, platform, command}`` dicts), a raw JSON ``dict``, or any
    duck-typed equivalent. Missing/empty executors yield no commands.
    """
    executors = _ability_attr(ability, "executors") or []
    commands: list[str] = []
    for executor in executors:
        if isinstance(executor, dict):
            command = executor.get("command")
        else:
            command = getattr(executor, "command", None)
        if isinstance(command, str) and command.strip():
            commands.append(command)
    return commands


def preview_ability(
    ability: object,
    *,
    local_target: str | None = None,
) -> AbilityPreview:
    """Preview all executor commands of a single Ability.

    Args:
        ability: An :class:`app.models.domain.Ability`, the raw JSON ``dict`` of a
            curated ability, or any duck-typed equivalent exposing
            ``ability_id``, ``name`` and ``executors``.
        local_target: Optional context label for purely local commands.

    Returns:
        An :class:`AbilityPreview` for the Ability.
    """
    ability_id = _ability_attr(ability, "ability_id")
    ability_name = _ability_attr(ability, "name")
    return preview_commands(
        _ability_commands(ability),
        ability_id=ability_id if isinstance(ability_id, str) else None,
        ability_name=ability_name if isinstance(ability_name, str) else None,
        local_target=local_target,
    )


def preview_abilities(
    abilities: Iterable[object],
    *,
    local_target: str | None = None,
) -> EmulationPreview:
    """Build the complete emulation preview over every Ability of an Operation.

    This is the primary entry point the preview endpoint
    (``POST /api/casos/{caso}/emulacao/preview``) and the ``EmulationConfirmModal``
    consume: the full list of concrete commands, each with its resolved target
    container(s), plus a presentation-only flag on any external destination.

    Args:
        abilities: The Abilities to preview (typically an Adversary's set, in
            ``atomic_ordering``).
        local_target: Optional context label for purely local commands.

    Returns:
        An :class:`EmulationPreview` aggregating one :class:`AbilityPreview` per
        Ability.
    """
    previews = [
        preview_ability(ability, local_target=local_target) for ability in abilities
    ]
    return EmulationPreview(abilities=previews)
