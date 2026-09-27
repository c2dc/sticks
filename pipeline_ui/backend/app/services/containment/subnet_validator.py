"""Destination subnet validation (task 4.2).

Second stage of the ``ContainmentValidator`` security core. Where
:mod:`app.services.containment.command_parser` (task 4.1) *extracts* the
destinations of a command, this module *classifies* each destination as
**internal** (belonging to one of the three lab subnets) or **external**, and
then applies the containment rule of the design:

    Property 1 — Nenhuma emulação inicia com destino externo.
    For any Adversary with any set of Abilities and commands, if at least one
    command has a destination that is not within the internal subnets
    (172.20.0.0/24, 172.21.0.0/24, 172.22.0.0/24), the Backend refuses the
    Ability and does not start the Operation, identifying the Ability and the
    command with the external destination.

    _Requisitos: 6.1, 6.2_

Classification rules (pure Python, **no network access**):

- **IP destinations** (:data:`DestinationKind.IP`): tested for membership in the
  three internal ``/24`` networks with the standard library :mod:`ipaddress`.
  A well-formed IPv4 literal inside a subnet is INTERNAL; anything else
  (including a malformed literal that ``ipaddress`` cannot parse) is EXTERNAL.
- **Hostname destinations** (:data:`DestinationKind.HOST`): treated as EXTERNAL
  by definition here. A hostname such as ``nmap.org`` is not a literal internal
  IP and we deliberately do **not** perform DNS resolution — a non-internal-IP
  host is external. This is the safe default for a containment gate: unknown
  means refused.
- **URL destinations** (:data:`DestinationKind.URL`): the parser already reduced
  a URL to its bare host/IP in ``Destination.value``, so URLs are classified by
  the same IP-vs-host logic above.

Scope boundary:

- This module only reasons about **command destinations** (Property 1). It does
  not inspect container network configuration — that is task 4.4 (Property 2),
  which reuses :data:`INTERNAL_SUBNETS` defined here.
- The overall verdict is a pure decision over parsed input; nothing here starts,
  aborts or mutates an Operation. The OperationRunner (task 7) consumes the
  :class:`AbilityContainmentReport` verdict to gate execution.

_Requisitos: 6.1, 6.2_
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.services.containment.command_parser import (
    CommandDestinations,
    Destination,
    DestinationKind,
    parse_command,
)
from app.services.containment.subnets import (
    CALDERA_KALI_SUBNET,
    INTERNAL_NETWORKS,
    KALI_NGINX_SUBNET,
    NGINX_DB_SUBNET,
    is_internal_ip,
)

# ---------------------------------------------------------------------------
# Named subnet constants — the single source of truth lives in
# ``app.services.containment.subnets``. They are re-exported here so existing
# imports (task 4.4 isolation, tests) keep working unchanged.
# ---------------------------------------------------------------------------

#: The three internal lab subnets. A destination is INTERNAL if and only if it
#: is an IPv4 literal contained in one of these networks. Alias of
#: :data:`app.services.containment.subnets.INTERNAL_NETWORKS`; shared with task 4.4.
INTERNAL_SUBNETS = INTERNAL_NETWORKS

__all__ = [
    "CALDERA_KALI_SUBNET",
    "KALI_NGINX_SUBNET",
    "NGINX_DB_SUBNET",
    "INTERNAL_SUBNETS",
    "DestinationClass",
    "ClassifiedDestination",
    "CommandClassification",
    "AbilityContainmentReport",
    "OperationContainmentReport",
    "is_internal_ip",
    "classify_destination",
    "validate_command_destinations",
    "validate_commands",
    "validate_ability",
    "validate_abilities",
]


class DestinationClass(str, enum.Enum):
    """Whether a destination is inside the internal lab subnets or not.

    Subclasses ``str`` so it serializes to its value over JSON, consistent with
    the other domain enums in :mod:`app.models.enums` and with
    :class:`DestinationKind`.
    """

    INTERNAL = "interno"
    EXTERNAL = "externo"


# ---------------------------------------------------------------------------
# Core classification of a single destination.
# ---------------------------------------------------------------------------


def classify_destination(destination: Destination) -> DestinationClass:
    """Classify a single :class:`Destination` as INTERNAL or EXTERNAL.

    - :data:`DestinationKind.IP` and :data:`DestinationKind.URL` (already reduced
      to a bare host/IP by the parser) are INTERNAL only when the value is an
      IPv4 literal inside an internal subnet.
    - :data:`DestinationKind.HOST` (a hostname such as ``nmap.org``) is EXTERNAL
      by definition: it is not a literal internal IP and DNS resolution is not
      performed here.

    Args:
        destination: The destination produced by the command parser.

    Returns:
        The :class:`DestinationClass` for the destination.
    """
    if destination.kind is DestinationKind.HOST:
        # Hostnames are external by definition (no DNS resolution).
        return DestinationClass.EXTERNAL
    # IP or URL: value is a bare IP/host; internal only if an internal IPv4.
    return (
        DestinationClass.INTERNAL
        if is_internal_ip(destination.value)
        else DestinationClass.EXTERNAL
    )


# ---------------------------------------------------------------------------
# Typed result structures (per-destination, per-command, per-ability).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifiedDestination:
    """A destination paired with its internal/external classification.

    Attributes:
        destination: The original parsed :class:`Destination`.
        classification: INTERNAL or EXTERNAL per :func:`classify_destination`.
    """

    destination: Destination
    classification: DestinationClass

    @property
    def is_external(self) -> bool:
        """True when this destination is external to the lab subnets."""
        return self.classification is DestinationClass.EXTERNAL


@dataclass(frozen=True)
class CommandClassification:
    """Per-command classification of every destination it reaches.

    Attributes:
        command: The original command string, unchanged.
        classified: One :class:`ClassifiedDestination` per detected destination,
            in the order the parser reported them. Empty for purely local
            commands.
    """

    command: str
    classified: list[ClassifiedDestination] = field(default_factory=list)

    @property
    def external_destinations(self) -> list[Destination]:
        """The subset of destinations classified as EXTERNAL (offending ones)."""
        return [c.destination for c in self.classified if c.is_external]

    @property
    def has_external(self) -> bool:
        """True when the command reaches at least one external destination."""
        return any(c.is_external for c in self.classified)


@dataclass(frozen=True)
class AbilityContainmentReport:
    """Overall containment verdict for a single Ability's commands.

    This is the structured, typed result the OperationRunner (task 7) consumes to
    decide whether the Ability — and therefore the Operation — may start.

    Attributes:
        ability_id: Identifier of the Ability whose commands were classified
            (``None`` when validating a bare list of commands without an Ability
            context).
        ability_name: Human-readable Ability name for error messages
            (``None`` when unknown).
        commands: Per-command classification, in input order.
        allowed: ``True`` iff **every** command is internal-only (no external
            destination). When ``False`` the Ability is REFUSED and the Operation
            cannot start.
    """

    ability_id: str | None
    ability_name: str | None
    commands: list[CommandClassification] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        """True iff no command reaches an external destination."""
        return not any(cmd.has_external for cmd in self.commands)

    @property
    def refused(self) -> bool:
        """True iff at least one command reaches an external destination."""
        return not self.allowed

    @property
    def offending_commands(self) -> list[CommandClassification]:
        """Commands (in order) that carry at least one external destination."""
        return [cmd for cmd in self.commands if cmd.has_external]

    def describe_violations(self) -> list[str]:
        """Human-readable lines identifying each offending command + destination.

        Suitable for the 409 error body (Req. 6.2) and the confirmation modal.
        Each line names the Ability (when known), the offending command and the
        specific external destination(s).
        """
        label = self.ability_name or self.ability_id or "(ability desconhecida)"
        lines: list[str] = []
        for cmd in self.offending_commands:
            externals = ", ".join(
                dest.raw for dest in cmd.external_destinations
            )
            lines.append(
                f"Ability '{label}': comando com destino externo "
                f"[{externals}] -> {cmd.command}"
            )
        return lines


@dataclass(frozen=True)
class OperationContainmentReport:
    """Aggregate containment verdict over all Abilities of an Operation.

    Attributes:
        abilities: One :class:`AbilityContainmentReport` per Ability, in order.
        allowed: ``True`` iff **every** Ability is allowed. If any Ability is
            refused, the whole Operation cannot start (Property 1).
    """

    abilities: list[AbilityContainmentReport] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        """True iff every Ability is internal-only."""
        return all(report.allowed for report in self.abilities)

    @property
    def refused(self) -> bool:
        """True iff at least one Ability is refused."""
        return not self.allowed

    @property
    def refused_abilities(self) -> list[AbilityContainmentReport]:
        """The Ability reports that were refused (external destination present)."""
        return [report for report in self.abilities if report.refused]

    def describe_violations(self) -> list[str]:
        """Flatten every offending Ability/command/destination into text lines."""
        lines: list[str] = []
        for report in self.refused_abilities:
            lines.extend(report.describe_violations())
        return lines


# ---------------------------------------------------------------------------
# Public validation entry points.
# ---------------------------------------------------------------------------


def _classify_command(parsed: CommandDestinations) -> CommandClassification:
    """Classify every destination of one already-parsed command."""
    classified = [
        ClassifiedDestination(
            destination=dest,
            classification=classify_destination(dest),
        )
        for dest in parsed.destinations
    ]
    return CommandClassification(command=parsed.command, classified=classified)


def validate_command_destinations(
    command_destinations: Iterable[CommandDestinations],
    *,
    ability_id: str | None = None,
    ability_name: str | None = None,
) -> AbilityContainmentReport:
    """Validate a batch of parsed commands against the internal subnets.

    This is the primary entry point that consumes the output of
    :func:`app.services.containment.command_parser.extract_destinations`. Use it
    when the caller already parsed the commands (e.g. task 4.6 preview shares the
    parse).

    Args:
        command_destinations: Parsed commands (``CommandDestinations``).
        ability_id: Optional Ability identifier for the report.
        ability_name: Optional Ability name for error messages.

    Returns:
        An :class:`AbilityContainmentReport`. ``allowed`` is ``True`` only when
        every command is internal-only.
    """
    commands = [_classify_command(parsed) for parsed in command_destinations]
    return AbilityContainmentReport(
        ability_id=ability_id,
        ability_name=ability_name,
        commands=commands,
    )


def validate_commands(
    commands: Sequence[str],
    *,
    ability_id: str | None = None,
    ability_name: str | None = None,
) -> AbilityContainmentReport:
    """Parse and validate raw command strings for a single Ability.

    Convenience wrapper that runs the parser (task 4.1) then the subnet
    classification for callers holding raw command strings.

    Args:
        commands: Concrete shell command strings.
        ability_id: Optional Ability identifier for the report.
        ability_name: Optional Ability name for error messages.

    Returns:
        An :class:`AbilityContainmentReport` for the Ability's commands.
    """
    parsed = [parse_command(command) for command in commands]
    return validate_command_destinations(
        parsed, ability_id=ability_id, ability_name=ability_name
    )


def _ability_commands(ability: object) -> list[str]:
    """Extract the concrete commands from an Ability-like object.

    Accepts either the SQLAlchemy :class:`app.models.domain.Ability` (whose
    ``executors`` is a list of ``{name, platform, command}`` dicts) or any
    duck-typed object exposing the same ``executors`` shape. Missing/empty
    executors yield no commands (an internal-only, trivially allowed Ability).
    """
    executors = getattr(ability, "executors", None) or []
    commands: list[str] = []
    for executor in executors:
        if isinstance(executor, dict):
            command = executor.get("command")
        else:
            command = getattr(executor, "command", None)
        if isinstance(command, str) and command.strip():
            commands.append(command)
    return commands


def validate_ability(ability: object) -> AbilityContainmentReport:
    """Validate all executor commands of a single Ability.

    Args:
        ability: An :class:`app.models.domain.Ability` (or duck-typed equivalent)
            exposing ``ability_id``, ``name`` and ``executors``.

    Returns:
        An :class:`AbilityContainmentReport` identifying the Ability and any
        offending command/destination when refused.
    """
    ability_id = getattr(ability, "ability_id", None)
    ability_name = getattr(ability, "name", None)
    return validate_commands(
        _ability_commands(ability),
        ability_id=ability_id,
        ability_name=ability_name,
    )


def validate_abilities(abilities: Iterable[object]) -> OperationContainmentReport:
    """Validate every Ability of an Operation and aggregate the verdict.

    If ANY command of ANY Ability reaches an external destination, the resulting
    :class:`OperationContainmentReport` is ``refused`` and the Operation cannot
    start, with the offending ability/command/destination available for error
    messages (Property 1, Req. 6.1/6.2).

    Args:
        abilities: The Abilities to validate (typically an Adversary's set).

    Returns:
        An :class:`OperationContainmentReport` aggregating each Ability report.
    """
    reports = [validate_ability(ability) for ability in abilities]
    return OperationContainmentReport(abilities=reports)
