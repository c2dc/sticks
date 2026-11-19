"""Pure substitution of Atomic Red Team ``#{name}`` placeholders (Req. 11, 12).

``resolve_command`` replaces every ``#{name}`` of a stored command with the
``default`` of the matching Atomic input argument, in a single regex pass: text
that was just substituted is never scanned again. A placeholder without a usable
default (unknown name, missing/null default) is kept intact and its name is
reported in ``unresolved_arguments``.

``PathToAtomicsFolder`` / ``$PathToAtomicsFolder`` is the synthetic Path_Argument:
it is never substituted, neither in the command nor inside substituted defaults.
When the token appears in the result, ``PathToAtomicsFolder`` is reported once in
``unresolved_arguments`` (the agent decides the folder). ``with_path_argument``
adds the synthetic argument metadata to a copy of the arguments map.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PATH_ARGUMENT_NAME = "PathToAtomicsFolder"
PATH_ARGUMENT_SPEC: dict[str, str | None] = {
    "default": None,
    "description": "Folder where the payload files live in the target container",
    "type": "path",
}

_PATH_RE = re.compile(r"\$?PathToAtomicsFolder")
# One alternation so placeholders and the path token keep their order of appearance.
_TOKEN_RE = re.compile(r"#\{([^{}]+)\}|(\$?PathToAtomicsFolder)")


@dataclass(frozen=True)
class ResolvedCommand:
    """Result of ``resolve_command``."""

    command: str
    arguments_used: dict[str, str] = field(default_factory=dict)
    unresolved_arguments: list[str] = field(default_factory=list)


def _default_of(spec: Any) -> str | None:
    """Return the argument default as text, or ``None`` when absent/null."""
    if not isinstance(spec, Mapping):
        return None
    value = spec.get("default")
    if value is None:
        return None
    return str(value)


def with_path_argument(command: str, input_arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of the arguments plus the synthetic Path_Argument.

    The Path_Argument is added only when the command or any default contains the
    ``PathToAtomicsFolder`` token. The input mapping is never mutated.
    """
    arguments: dict[str, Any] = (
        dict(input_arguments) if isinstance(input_arguments, Mapping) else {}
    )
    present = bool(_PATH_RE.search(command or "")) or any(
        (default := _default_of(spec)) is not None and _PATH_RE.search(default)
        for spec in arguments.values()
    )
    if present and PATH_ARGUMENT_NAME not in arguments:
        arguments[PATH_ARGUMENT_NAME] = dict(PATH_ARGUMENT_SPEC)
    return arguments


def extract_argument_values(template: str, final: str) -> dict[str, str]:
    """Recover the value each argument took in ``final`` from its ``template``.

    Matches ``final`` against the template with every token turned into a lazy
    capture group. Returns the first captured value per argument name, or an empty
    dict when ``final`` does not follow the template shape (for example when the
    agent rewrote text around the placeholders).
    """
    pattern = ""
    names: list[str] = []
    position = 0
    for match in _TOKEN_RE.finditer(template or ""):
        pattern += re.escape(template[position : match.start()]) + "(.*?)"
        names.append(PATH_ARGUMENT_NAME if match.group(2) is not None else match.group(1))
        position = match.end()
    pattern += re.escape((template or "")[position:])
    found = re.fullmatch(pattern, final or "", flags=re.DOTALL)
    if found is None:
        return {}
    values: dict[str, str] = {}
    for name, value in zip(names, found.groups(), strict=True):
        values.setdefault(name, value)
    return values


def detect_argument_names(command: str) -> list[str]:
    """Return the distinct argument names found in ``command``, in order.

    Covers ``#{name}`` placeholders and the synthetic ``PathToAtomicsFolder``.
    """
    names: list[str] = []
    for match in _TOKEN_RE.finditer(command or ""):
        name = PATH_ARGUMENT_NAME if match.group(2) is not None else match.group(1)
        if name not in names:
            names.append(name)
    return names


def resolve_command(
    command: str,
    input_arguments: Mapping[str, Any] | None,
) -> ResolvedCommand:
    """Substitute ``#{name}`` placeholders of ``command`` in one pass."""
    arguments = input_arguments if isinstance(input_arguments, Mapping) else {}
    used: dict[str, str] = {}
    unresolved: list[str] = []

    def mark_unresolved(name: str) -> None:
        if name not in unresolved:
            unresolved.append(name)

    def replace(match: re.Match[str]) -> str:
        if match.group(2) is not None:
            mark_unresolved(PATH_ARGUMENT_NAME)
            return match.group(0)
        name = match.group(1)
        default = _default_of(arguments.get(name))
        if default is None:
            mark_unresolved(name)
            return match.group(0)
        if _PATH_RE.search(default):
            mark_unresolved(PATH_ARGUMENT_NAME)
        used[name] = default
        return default

    resolved = _TOKEN_RE.sub(replace, command or "")
    return ResolvedCommand(command=resolved, arguments_used=used, unresolved_arguments=unresolved)
