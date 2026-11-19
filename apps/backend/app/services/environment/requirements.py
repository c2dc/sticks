"""Per-technique environment requirements (design D13, task 16.3).

:class:`TechniqueEnvironmentRequirements` is what one technique needs from the lab:
hosts, operating system, a best-effort image hint, ports, services, file paths and
dependencies on other techniques. The operationalization agent builds one per
technique from the final commands, before the environment phase runs, so the
environment agent can object (design D14) without provisioning anything.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from app.services.environment.operationalization import TargetHost

# Absolute POSIX paths, Windows drive paths and the Atomic path token.
_FILE_PATH_RE = re.compile(
    r"(?<![\w:/.\-])(?:/(?:[\w.\-]+/)*[\w.\-]+"
    r"|[A-Za-z]:\\(?:[^\s\\\"'|<>]+\\)*[^\s\\\"'|<>]+"
    r"|\$?PathToAtomicsFolder(?:[\\/][^\s\"'|<>]+)?)"
)

# Paths that are not payload files: device nodes and pseudo-files.
_IGNORED_PATH_PREFIXES = ("/dev/", "/proc/", "/sys/")
_IGNORED_PATHS = frozenset({"/", "/dev/null"})


class TechniqueEnvironmentRequirements(BaseModel):
    """Environment needs of a single technique.

    Attributes:
        technique_id: Technique this record belongs to.
        hosts: Internal target hosts resolved from the commands' destinations.
        target_os: ``"linux"`` or ``"windows"``.
        image_hint: Image resolved ahead of time; ``None`` when not resolved
            (best-effort, never blocks).
        ports: Ports referenced by the final commands (sorted, unique).
        services: Descriptions of the services the commands need.
        files: File paths cited by the final commands.
        depends_on: ``technique_id`` of earlier techniques whose environment this
            one shares (same file path).
    """

    technique_id: str
    hosts: list[TargetHost] = Field(default_factory=list)
    target_os: str = "linux"
    image_hint: str | None = None
    ports: list[int] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class ObjectionCode(StrEnum):
    """Codes the environment agent raises when it cannot meet a requirement (design D14).

    The values double as ``Emulability_Reason_Code`` when a technique ends up
    ``not_emulable`` after the negotiation.
    """

    PORT_CONFLICT = "port_conflict"
    IMAGE_UNAVAILABLE = "image_unavailable"
    OS_UNSUPPORTED = "os_unsupported"
    REQUIRES_VM = "requires_vm"


class Objection(BaseModel):
    """One objection from the environment agent to a technique's requirements."""

    technique_id: str
    code: ObjectionCode
    detail: str | None = None


class NegotiationRound(BaseModel):
    """One technique's evaluation in one negotiation round (design D14, Req. 21.6).

    ``round`` counts evaluation passes starting at 1. ``objections`` is empty when
    the environment agent accepted the requirements in that round.
    """

    round: int
    technique_id: str
    environment_requirements: TechniqueEnvironmentRequirements
    objections: list[Objection] = Field(default_factory=list)


class TechniqueNegotiationStatus(BaseModel):
    """Planned emulability of a technique at the end of the negotiation.

    ``status`` is ``planned_emulable`` or ``not_emulable``; ``reason_code`` is the
    code of the last objection when ``not_emulable``, otherwise ``None``.
    """

    status: str
    reason_code: ObjectionCode | None = None


class NegotiationResult(BaseModel):
    """Outcome of the ``op_agent`` / ``env_agent`` negotiation, kept for the screen.

    Attributes:
        max_rounds: Configured limit of adjustment rounds.
        rounds_used: Adjustment rounds actually consumed.
        rounds: Every evaluation made, in order.
        techniques: Final status per ``technique_id``.
    """

    max_rounds: int = 0
    rounds_used: int = 0
    rounds: list[NegotiationRound] = Field(default_factory=list)
    techniques: dict[str, TechniqueNegotiationStatus] = Field(default_factory=dict)


def extract_file_paths(command: str | None) -> list[str]:
    """Return the file paths cited in ``command`` (ordered, unique)."""
    if not command:
        return []
    paths: list[str] = []
    for match in _FILE_PATH_RE.finditer(command):
        path = match.group(0).rstrip(".,;)")
        if path in _IGNORED_PATHS or path.startswith(_IGNORED_PATH_PREFIXES):
            continue
        if path not in paths:
            paths.append(path)
    return paths


__all__ = [
    "NegotiationResult",
    "NegotiationRound",
    "Objection",
    "ObjectionCode",
    "TechniqueEnvironmentRequirements",
    "TechniqueNegotiationStatus",
    "extract_file_paths",
]
