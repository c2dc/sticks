"""Tool wrappers over existing backend services (lazy, argv-style, no shell).

Will host thin wrappers used by the agents (e.g. ``tool_read_attack``,
``tool_atomic_lookup``, ``tool_parse_command``, ``tool_compose``,
``tool_caldera_load``) over ``StixService``/``CaseService``, the containment
parser, ``ComposeExecutor`` and ``CalderaClient`` (tasks 7.1, 10.1, 11.1).

Each wrapper imports and instantiates its underlying service lazily, uses argv
lists (never a shell), and performs no I/O at import time (Requirement 1.7).
"""
# --- Stage 3 (DAG) tool wrappers (task 10.1) ---


def tool_read_attack(
    campaign: str,
    *,
    version: str = "18.0",
) -> dict:
    """STIX/ATT&CK + curated context reader (wraps ``StixService``/``CaseService``).

    Thin, lazy, **read-only** tool used by the Stage 3 ``AiDagGenerator`` to
    gather the ATT&CK/STIX view of a curated campaign together with its curated
    case context (the structural DAG input, abilities and adversary). It is the
    agent's single read seam over the project's existing services: it never
    reimplements STIX parsing or case loading, and it performs no network egress
    and no writes (Requirement 2.1).

    Laziness (Requirement 1.7): both services are imported and instantiated
    *inside* this function, so importing :mod:`app.services.ai.tools` performs no
    I/O and pulls in no heavy dependency. The underlying reads
    (:meth:`StixService.get_campaign_by_slug`, :meth:`CaseService.load_case`)
    only ever read from ``sticks/`` — this wrapper adds no side effects.

    Args:
        campaign: The curated campaign slug (e.g. ``"shadowray"``), matching a
            registered :data:`CaseService.CURATED_CASES` slug.
        version: The ATT&CK Enterprise base version to resolve the STIX campaign
            against; defaults to the curated version (``"18.0"``), the only base
            carrying the 8 curated campaigns.

    Returns:
        A plain ``dict`` with:

        * ``campaign`` — the requested slug (echoed for traceability);
        * ``version`` — the resolved ATT&CK base version;
        * ``attack`` — the STIX campaign detail
          (:meth:`StixService.get_campaign_by_slug` serialized), or ``None`` when
          no campaign in the base resolves to the slug;
        * ``curated`` — the curated case context
          (:meth:`CaseService.load_case` serialized: DAG input, abilities and
          adversary), or ``None`` when the slug is not a curated case or its
          files are missing/unreadable.

        Both sides are optional so a partial read (STIX present but no curated
        case, or vice versa) still yields a usable, serializable context instead
        of raising.
    """
    # Lazy imports: no I/O and no heavy deps at module import time (Req. 1.7).
    from app.services.campaign_service import CaseFileError, CaseService
    from app.services.stix.stix_service import StixFileError, StixService

    # ATT&CK/STIX side — tolerant: a missing/unreadable base yields attack=None
    # rather than breaking the agent's read.
    attack: dict | None = None
    try:
        detail = StixService().get_campaign_by_slug(version, campaign)
        if detail is not None:
            attack = detail.model_dump()
    except StixFileError:
        attack = None

    # Curated-case side — tolerant: unknown slug or missing/malformed case files
    # yield curated=None instead of raising.
    curated: dict | None = None
    try:
        curated = CaseService().load_case(campaign).model_dump()
    except (KeyError, CaseFileError):
        curated = None

    return {
        "campaign": campaign,
        "version": version,
        "attack": attack,
        "curated": curated,
    }

# --- Stage 4 (operationalization) tool wrappers (task 7.1) ---
#
# Two thin wrappers used by the ``AiOperationalizer`` (Stage 4):
#
#   * ``tool_atomic_lookup`` — the Atomic Red Team command source: the pool of
#     concrete executor commands the agent may select/parametrize for a given
#     ATT&CK technique. The project's curated cases ARE that source (each curated
#     ``Ability`` carries a ``technique_id`` and its Atomic Red Team ``executors``
#     commands), so this wraps ``CaseService`` rather than reimplementing a
#     catalogue. Read-only.
#   * ``tool_parse_command`` — wraps ``services.containment.parse_command`` so the
#     agent can self-check a proposed command's destinations (and their
#     internal/external classification) BEFORE the unchanged downstream
#     containment gate would reject it.
#
# Both are lazy (all imports happen inside the function bodies), perform no I/O at
# import time (Requirement 1.7), and never build or run a shell string: the atomic
# lookup only reads curated JSON and the parser is pure Python. Commands are kept
# as argv-friendly data (plain strings the caller turns into argv lists); nothing
# here spawns a subprocess or touches the network (Requirements 2.2, 8.1, 9.2).


def tool_atomic_lookup(technique_id: str) -> list[dict]:
    """Return the Atomic Red Team command pool for an ATT&CK technique.

    The Stage 4 operationalization agent calls this to discover the concrete
    commands it may select and parametrize for a DAG node. The command source is
    the project's single source of truth — the curated cases — where each
    ``Ability`` carries a ``technique_id`` and its Atomic Red Team ``executors``.
    This wrapper is a thin, read-only, lazy adapter over
    :class:`app.services.campaign_service.CaseService`; it never reimplements a
    catalogue, opens a socket, or spawns a subprocess.

    Args:
        technique_id: The ATT&CK technique id to look up (e.g. ``"T1190"``).
            Matching is case-insensitive and tolerant of surrounding whitespace;
            a blank/``None`` id yields an empty pool rather than an error.

    Returns:
        One dict per matching curated executor, in curated order, each shaped as::

            {
                "technique_id": str,        # the ability's technique id
                "technique_name": str | None,
                "ability_id": str,          # source ability
                "ability_name": str | None,
                "tactic": str | None,
                "name": str | None,         # executor name
                "platform": str | None,     # executor platform
                "command": str,             # concrete command (argv-friendly)
            }

        Executors without a concrete command are skipped. An unknown technique (or
        one absent from every curated case) returns an empty list.
    """
    normalized = (technique_id or "").strip().casefold()
    if not normalized:
        return []

    # Lazy import: no service construction or file access happens at module import.
    from app.services.campaign_service import CaseService

    service = CaseService()
    pool: list[dict] = []
    # ``load_all_cases_safe`` never raises on a single malformed case; it returns a
    # catalogue of the cases that parsed, which is what a best-effort command pool
    # wants (one bad curated file must not blind the whole lookup).
    catalog = service.load_all_cases_safe()
    for case in catalog.cases.values():
        for ability in case.abilities:
            if (ability.technique_id or "").strip().casefold() != normalized:
                continue
            for executor in ability.executors:
                command = (executor.command or "").strip()
                if not command:
                    continue
                pool.append(
                    {
                        "technique_id": ability.technique_id,
                        "technique_name": ability.technique_name,
                        "ability_id": ability.ability_id,
                        "ability_name": ability.name,
                        "tactic": ability.tactic,
                        "name": executor.name,
                        "platform": executor.platform,
                        "command": command,
                    }
                )
    return pool


def tool_parse_command(command: str) -> dict:
    """Parse one command's destinations and classify each internal/external.

    Thin, lazy wrapper over :func:`app.services.containment.parse_command` (the
    project's single command-parsing source of truth) plus
    :func:`app.services.containment.is_internal_ip` for the internal/external
    verdict. The Stage 4 agent uses this to **self-check** a proposed command
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

# --- Stage 5 (environment) tool wrappers (task 11.1) ---
#
# Thin, lazy wrappers that give the Environment_Agent lab control without
# reimplementing it: ``tool_compose`` dispatches to the ``ComposeExecutor`` seam
# (up/down/exec/network) and ``tool_caldera_load`` wraps ``CalderaClient``
# (load_abilities/load_adversary). Both import and instantiate their underlying
# service lazily (no I/O at import time — Requirement 1.7), route every operation
# through the existing, unchanged containment/isolation machinery (Req. 9.3), and
# — for compose — pass commands as argv **lists**, never a shell string, so the
# injection surface stays closed (Req. 2.3 seam compliance, security steering).

from typing import TYPE_CHECKING, Any, Callable

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
