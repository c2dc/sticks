"""Tool wrappers over existing backend services (lazy, argv-style, no shell).

Hosts thin wrappers used by the agents (e.g. ``tool_read_attack``,
``tool_atomic_lookup``, ``tool_parse_command``, ``tool_compose``,
``tool_caldera_load``) over the database read services (``StixPackageReadService``,
``CommandBase``), the containment parser, ``ComposeExecutor`` and
``CalderaClient`` (tasks 7.1, 10.1, 11.1, 14.1).

Each wrapper imports and instantiates its underlying service lazily, uses argv
lists (never a shell), and performs no I/O at import time (Requirement 1.7).

Database-sourced Agent_Input (task 14.1): the flow/operationalization context
readers (``tool_read_attack``/``tool_atomic_lookup``) resolve their context from the
**database** — ``StixPackageReadService.campaign_overview`` for the STIX/ATT&CK
base context and ``CommandBase.commands_for_technique`` for the Atomic Red Team
command pool — never from the legacy CLI data directory on disk (Req. 5.1, 5.2, 5.4).
Both take a request-scoped SQLAlchemy ``Session`` so the read shares the run's
unit of work; without a session they degrade to an empty context rather than
falling back to disk, preserving the no-disk guarantee.
"""

from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, no import-time cost
    from sqlalchemy.orm import Session

# --- flow-phase tool wrappers (task 10.1, 14.1) ---

# The ATT&CK Enterprise base version the AI pipeline resolves campaigns against
# (the curated version declared by the seed). Taken from the light seed manifest
# (a constant module: no I/O, no heavy import at module import time, Req. 1.7);
# the same value as ``app.services.stix.stix_service.CURATED_VERSION``.
from app.seeds.manifest import CURATED_STIX_VERSION as _DEFAULT_VERSION


def tool_read_attack(
    campaign: str,
    *,
    version: str = _DEFAULT_VERSION,
    session: "Optional[Session]" = None,
) -> dict:
    """STIX/ATT&CK base-context reader from the DATABASE (no disk — Req. 5.1, 5.2, 5.4).

    Thin, lazy, **read-only** tool used by the flow-phase ``FlowAgent`` to
    gather the ATT&CK/STIX base context of a campaign — its identity, techniques
    and actors — from the persisted STIX, for ANY of the 52 campaigns (curated or
    not). It reads from the ``Campaign_Overview`` served by
    :meth:`~app.services.stix.stix_package_read_service.StixPackageReadService.campaign_overview`
    and never opens the legacy CLI data directory on disk: the Agent_Input base context is
    DB-sourced (Req. 5.1, 5.2, 5.4).

    Laziness (Requirement 1.7): the read service is imported and instantiated
    *inside* this function, so importing :mod:`app.services.ai.tools` performs no
    I/O and pulls in no heavy dependency. This wrapper performs no network egress
    and no writes.

    Args:
        campaign: The campaign key (``curated_slug`` when present, else
            ``stix_id``) addressing the campaign in the persisted STIX package.
        version: The ATT&CK Enterprise base version to resolve the campaign
            against; defaults to the curated version (``"18.0"``).
        session: The request-scoped SQLAlchemy ``Session`` the read runs on. When
            ``None`` the tool returns an empty context (``attack=None``) rather
            than falling back to disk, preserving the no-disk guarantee (Req. 5.2).

    Returns:
        A plain ``dict`` with:

        * ``campaign`` — the requested campaign key (echoed for traceability);
        * ``version`` — the resolved ATT&CK base version;
        * ``attack`` — the campaign base context from the database
          (:class:`~app.services.stix.stix_package_read_service.StixCampaignOverview`
          serialized: identity, aliases, description, techniques and actors), or
          ``None`` when no session is available or the campaign is not persisted;
        * ``curated`` — retained for shape compatibility with the trace
          serialization and downstream consumers; it mirrors the campaign's
          ``curated`` flag (``{"curated": bool}``) rather than a disk-read curated
          case, or ``None`` when the base context is unavailable.

        The two keys are preserved so the Audit_Trace ``agent_input`` shape is
        unchanged; the base context is sourced entirely from the database.
    """
    # Lazy imports: no I/O and no heavy deps at module import time (Req. 1.7).
    if session is None:
        # No session to read the DB with. Do NOT fall back to disk — the
        # Agent_Input path must never read the legacy CLI data directory (Req. 5.2). An empty
        # base context lets generation proceed (and degrade) without a disk read.
        return {
            "campaign": campaign,
            "version": version,
            "attack": None,
            "curated": None,
        }

    from app.services.stix.stix_package_read_service import (
        CampaignNotFoundError,
        StixPackageNotPersistedError,
        StixPackageReadService,
    )

    attack: dict | None = None
    curated: dict | None = None
    try:
        overview = StixPackageReadService(session).campaign_overview(
            version, campaign
        )
        attack = overview.model_dump()
        # Preserve the legacy ``curated`` key shape without a disk read: carry the
        # campaign's curated flag so trace consumers keep a stable shape (Req. 5.2).
        curated = {"curated": overview.curated}
    except (CampaignNotFoundError, StixPackageNotPersistedError):
        # Not persisted for this version: degrade to an empty base context rather
        # than raising, so generation can still proceed (and fail cleanly if the
        # model returns nothing usable).
        attack = None
        curated = None

    return {
        "campaign": campaign,
        "version": version,
        "attack": attack,
        "curated": curated,
    }

# --- operationalization-phase tool wrappers (task 7.1, 14.1) ---
#
# Two thin wrappers used by the ``AiOperationalizer`` (operationalization phase):
#
#   * ``tool_atomic_lookup`` — the Atomic Red Team command source: the pool of
#     concrete executor commands the agent may select/parametrize for a given
#     ATT&CK technique. The source is the DATABASE ``Command_Base`` (the
#     ``technique_commands`` relational mapping plus the pgvector similarity
#     fallback), queried via :class:`app.services.command_base.CommandBase`, NOT
#     the curated cases on disk (Req. 12.1, 12.3). Read-only.
#   * ``tool_parse_command`` — wraps ``services.containment.parse_command`` so the
#     agent can self-check a proposed command's destinations (and their
#     internal/external classification) BEFORE the unchanged downstream
#     containment gate would reject it.
#
# Both are lazy (all imports happen inside the function bodies), perform no I/O at
# import time (Requirement 1.7), and never build or run a shell string: the atomic
# lookup reads the DB-backed Command_Base and the parser is pure Python. Commands
# are kept as argv-friendly data (plain strings the caller turns into argv lists);
# nothing here spawns a subprocess or touches the network (Requirements 2.2, 8.1,
# 9.2) and nothing reads the legacy CLI data directory from disk (Req. 5.2).


def tool_atomic_lookup(
    technique_id: str,
    *,
    session: "Optional[Session]" = None,
) -> list[dict]:
    """Return the Atomic Red Team command pool for a technique from the DATABASE.

    The operationalization-phase agent calls this to discover the concrete
    commands it may select and parametrize for an attack flow node. The command source is
    the DB-backed ``Command_Base`` — the ``technique_commands`` relational mapping
    first, then the pgvector similarity fallback — resolved via
    :meth:`app.services.command_base.CommandBase.commands_for_technique`, NOT the
    curated cases on disk (Req. 12.1, 12.3). This wrapper is a thin, read-only,
    lazy adapter; it never reimplements a catalogue, opens a socket, spawns a
    subprocess, or reads the legacy CLI data directory (Req. 5.2).

    Args:
        technique_id: The ATT&CK technique id to look up (e.g. ``"T1190"``).
            Matching is tolerant of surrounding whitespace; a blank/``None`` id
            yields an empty pool rather than an error.
        session: The request-scoped SQLAlchemy ``Session`` the Command_Base reads
            on. When ``None`` the tool returns an empty pool rather than falling
            back to disk, preserving the no-disk guarantee (Req. 5.2).

    Returns:
        One dict per command backing the technique, each shaped as::

            {
                "technique_id": str,        # the technique id
                "technique_name": str | None,
                "ability_id": str,          # synthetic source id (technique+index)
                "ability_name": str | None,
                "tactic": str | None,
                "name": str | None,         # executor name
                "platform": str | None,     # executor platform
                "command": str,             # concrete command (argv-friendly)
            }

        The dict shape is preserved for byte-compatibility with the consuming
        ``AiOperationalizer`` and the existing fakes. A technique with no backing
        command (exact or by similarity) returns an empty list.
    """
    normalized = (technique_id or "").strip()
    if not normalized:
        return []
    if session is None:
        # No session to read the DB with. Do NOT fall back to disk — the
        # Agent_Input/command-pool path must never read the legacy CLI data directory
        # (Req. 5.2). An empty pool lets the agent degrade cleanly.
        return []

    # Lazy import: no service construction or DB access happens at module import.
    from app.services.command_base import CommandBase

    suggestions = CommandBase(session).commands_for_technique(normalized)
    pool: list[dict] = []
    for index, suggestion in enumerate(suggestions):
        command = (suggestion.command or "").strip()
        if not command:
            continue
        pool.append(
            {
                "technique_id": suggestion.technique_id,
                "technique_name": None,
                # The Command_Base is keyed by technique, not by ability; derive a
                # stable synthetic ability id so the consumer can group commands
                # and keep a reproducible ordering.
                "ability_id": f"{suggestion.technique_id}-cmd-{index}",
                "ability_name": None,
                "tactic": None,
                "name": suggestion.executor or None,
                "platform": suggestion.platform or None,
                "command": command,
            }
        )
    return pool


def tool_parse_command(command: str) -> dict:
    """Parse one command's destinations and classify each internal/external.

    Thin, lazy wrapper over :func:`app.services.containment.parse_command` (the
    project's single command-parsing source of truth) plus
    :func:`app.services.containment.is_internal_ip` for the internal/external
    verdict. The operationalization-phase agent uses this to **self-check** a proposed command
    before the unchanged downstream containment gate would reject it — it only
    inspects the command string, never executes it, and does no I/O.

    Args:
        command: The concrete command string to inspect.

    Returns:
        A dict::

            {
                "command": str,             # the command, unchanged
                "destinations": [
                    {"value": str, "kind": str, "raw": str, "internal": bool},
                    ...
                ],
                "all_internal": bool,       # True iff every destination is internal
                "has_external": bool,       # True iff any destination is external
            }

        A purely local command (no outbound destinations) yields an empty
        ``destinations`` list with ``all_internal=True`` and ``has_external=False``.
    """
    # Lazy import: the containment parser is pure Python and import-safe, but we
    # keep the import inside the function to honour the package's lazy convention.
    from app.services.containment import is_internal_ip, parse_command

    parsed = parse_command(command)
    destinations: list[dict] = []
    has_external = False
    for dest in parsed.destinations:
        internal = is_internal_ip(dest.value)
        if not internal:
            has_external = True
        destinations.append(
            {
                "value": dest.value,
                "kind": dest.kind.value,
                "raw": dest.raw,
                "internal": internal,
            }
        )
    return {
        "command": parsed.command,
        "destinations": destinations,
        "all_internal": not has_external,
        "has_external": has_external,
    }

# --- environment-phase tool wrappers (task 11.1) ---
#
# Thin, lazy wrappers that give the Environment_Agent lab control without
# reimplementing it: ``tool_compose`` dispatches to the ``ComposeExecutor`` seam
# (up/down/exec/network) and ``tool_caldera_load`` wraps ``CalderaClient``
# (load_abilities/load_adversary). Both import and instantiate their underlying
# service lazily (no I/O at import time — Requirement 1.7), route every operation
# through the existing, unchanged containment/isolation machinery (Req. 9.3), and
# — for compose — pass commands as argv **lists**, never a shell string, so the
# injection surface stays closed (Req. 2.3 seam compliance, security steering).

if TYPE_CHECKING:  # pragma: no cover - typing only, no import-time cost
    from app.services.environment.compose_executor import (
        CommandResult,
        ComposeExecutor,
        LogLine,
    )


def tool_compose(
    action: str,
    *,
    executor: "ComposeExecutor | None" = None,
    on_log: "Callable[[LogLine], None] | None" = None,
    **kw: Any,
) -> dict:
    """Wrap ``ComposeExecutor`` (up/down/exec/network): lab control, argv lists, no shell.

    Dispatches ``action`` to the matching ``ComposeExecutor`` operation so the
    agent drives the contained lab through the project's single seam rather than
    shelling out itself. The ``ComposeExecutor`` is imported and built lazily (the
    real :class:`DockerComposeExecutor` when none is injected), so importing this
    module opens no subprocess and touches no Docker daemon (Requirement 1.7); the
    operations run through the existing, unchanged containment machinery (Req. 9.3).

    Every underlying command is issued in argv-list form by the executor (never a
    shell string): for ``action="exec"`` the caller passes ``argv`` as a **list**
    (e.g. ``["bash", "-lc", cmd]``), keeping command injection closed (Req. 2.3,
    security steering). ``container``/``network`` values are backend-controlled,
    never interpolated from end-user input.

    Args:
        action: The lab operation — one of ``"up"``, ``"down"``, ``"exec"``,
            ``"attach_network"``, ``"detach_network"``, ``"create_network"`` or
            ``"remove_network"``.
        executor: Optional ``ComposeExecutor`` to use (tests inject a fake); when
            ``None`` a real :class:`DockerComposeExecutor` is built lazily.
        on_log: Optional per-line callback forwarded to the executor; when ``None``
            the emitted lines are collected and returned under ``lines``.
        **kw: Operation arguments passed through by name, e.g. ``compose_file`` for
            ``up``/``down``; ``container`` + ``argv`` (a list) for ``exec``;
            ``container`` + ``network`` (+ optional ``ip``) for the network
            actions; ``network`` for ``create_network``/``remove_network``.

    Returns:
        A ``dict`` with the ``action``, the exit ``code`` (``0`` == success) and
        the emitted ``lines`` (each ``{"step", "stream", "text", "substage"}``).
    """
    from app.services.environment.compose_executor import (
        DockerComposeExecutor,
        LogLine,
    )

    executor = executor if executor is not None else DockerComposeExecutor()

    collected: list[LogLine] = []

    def _on_log(line: LogLine) -> None:
        collected.append(line)
        if on_log is not None:
            on_log(line)

    # Map each action to the executor method name; resolve only the one needed
    # (via ``getattr``) so the wrapper touches a single operation per call.
    method_by_action: dict[str, str] = {
        "up": "up",
        "down": "down",
        "exec": "exec_in",
        "attach_network": "attach_network",
        "detach_network": "detach_network",
        "create_network": "create_network",
        "remove_network": "remove_network",
    }
    method_name = method_by_action.get(action)
    if method_name is None:
        raise ValueError(
            f"ação de compose desconhecida: {action!r} "
            f"(esperado um de {sorted(method_by_action)})"
        )
    operation: Callable[..., "CommandResult"] = getattr(executor, method_name)

    result = operation(on_log=_on_log, **kw)

    return {
        "action": action,
        "code": result.code,
        "lines": [
            {
                "step": line.step,
                "stream": line.stream,
                "text": line.text,
                "substage": line.substage,
            }
            for line in result.lines
        ],
    }


def tool_caldera_load(
    abilities: list[dict],
    adversary: dict,
    *,
    client: Any = None,
) -> dict:
    """Wrap ``CalderaClient.load_abilities`` / ``load_adversary``.

    Pushes the curated Abilities and the Adversary into Caldera through the
    project's existing v2 client so the agent reuses the single source of truth
    for Caldera access (no reimplementation). The ``CalderaClient`` is imported and
    built lazily (a real client only opens a socket on first request), so importing
    this module performs no I/O (Requirement 1.7); the load runs through the
    unchanged Caldera machinery (Req. 9.3).

    Abilities are loaded **before** the Adversary that references them, mirroring
    the ordering the operation requires (an Adversary loaded first would reference
    not-yet-present Abilities).

    Args:
        abilities: The curated Ability payloads (v2 schema) to create/load.
        adversary: The curated Adversary payload (v2 schema) to create/load.
        client: Optional ``CalderaClient`` to use (tests inject one backed by a
            mock transport); when ``None`` a real client is built lazily.

    Returns:
        A ``dict`` with ``ability_ids`` (the loaded Abilities' ids) and the loaded
        ``adversary_id``.
    """
    from app.services.caldera.client import CalderaClient

    client = client if client is not None else CalderaClient()

    ability_refs = client.load_abilities(abilities)
    adversary_ref = client.load_adversary(adversary)

    return {
        "ability_ids": [ref.ability_id for ref in ability_refs],
        "adversary_id": adversary_ref.adversary_id,
    }
