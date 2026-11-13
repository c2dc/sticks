"""Property test: Atomic argument substitution is a single total pass.

Feature: agentic-only-flow-redesign, Property 18: Atomic argument substitution
is a single total pass, and Property 22: the Path_Argument token is never
substituted.

**Validates: Requirements 11.3, 11.5, 11.6, 11.8, 12.11, 12.13**
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.atomic_arguments import (
    PATH_ARGUMENT_NAME,
    resolve_command,
    with_path_argument,
)

_names = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
# Literal text without braces, '#' or the path token, so it never forms placeholders.
_literal = st.text(alphabet="abc XYZ./-=;", max_size=8)
# Default values may themselves look like placeholders (must not be re-scanned).
_default_text = st.one_of(
    _literal,
    _names.map(lambda n: "#{" + n + "}"),
    st.just("$PathToAtomicsFolder/x"),
    st.just("PathToAtomicsFolder\\y"),
)


@st.composite
def _scenarios(draw):
    names = draw(st.lists(_names, min_size=1, max_size=5, unique=True))
    args: dict[str, dict] = {}
    for n in names:
        kind = draw(st.sampled_from(["default", "null", "missing_key"]))
        if kind == "default":
            args[n] = {"default": draw(_default_text)}
        elif kind == "null":
            args[n] = {"default": None}
        else:
            args[n] = {"description": "d"}
    # Some referenced names are not declared at all.
    extra = draw(st.lists(_names, max_size=2))
    refs = draw(st.lists(st.sampled_from(names + extra), min_size=1, max_size=6))
    parts: list[str] = []
    for r in refs:
        parts.append(draw(_literal))
        parts.append("#{" + r + "}")
    parts.append(draw(_literal))
    return "".join(parts), refs, args


def _expected(command: str, args: dict):
    path_re = re.compile(r"\$?PathToAtomicsFolder")
    unresolved: list[str] = []

    def repl(m: re.Match[str]) -> str:
        if m.group(2) is not None:
            if PATH_ARGUMENT_NAME not in unresolved:
                unresolved.append(PATH_ARGUMENT_NAME)
            return m.group(0)
        spec = args.get(m.group(1))
        default = spec.get("default") if isinstance(spec, dict) else None
        if default is None:
            if m.group(1) not in unresolved:
                unresolved.append(m.group(1))
            return m.group(0)
        if path_re.search(str(default)) and PATH_ARGUMENT_NAME not in unresolved:
            unresolved.append(PATH_ARGUMENT_NAME)
        return str(default)

    out = re.sub(r"#\{([^{}]+)\}|(\$?PathToAtomicsFolder)", repl, command)
    return out, unresolved


@settings(max_examples=100, deadline=None)
@given(_scenarios())
def test_substitution_is_single_total_pass(scenario):
    command, refs, args = scenario
    result = resolve_command(command, args)
    expected_cmd, expected_unresolved = _expected(command, args)

    # Single pass: substituted defaults are never re-scanned.
    assert result.command == expected_cmd
    # Unresolved placeholders are reported (deduplicated, in order of appearance).
    assert result.unresolved_arguments == expected_unresolved
    for name in result.unresolved_arguments:
        if name != PATH_ARGUMENT_NAME:
            assert "#{" + name + "}" in result.command
    # Every resolvable placeholder is used, and the path token never survives.
    for name, spec in args.items():
        if name in refs and spec.get("default") is not None:
            assert name in result.arguments_used
            assert name not in result.unresolved_arguments
    # The path token is preserved and reported exactly once iff it is in the result.
    has_token = "PathToAtomicsFolder" in result.command
    assert result.unresolved_arguments.count(PATH_ARGUMENT_NAME) == int(has_token)
    # with_path_argument never mutates the input and adds the argument iff needed.
    before = {k: dict(v) for k, v in args.items()}
    extended = with_path_argument(command, args)
    assert args == before
    assert (PATH_ARGUMENT_NAME in extended) == (
        has_token or any("PathToAtomicsFolder" in str(v.get("default")) for v in args.values())
    )


def test_default_looking_like_placeholder_is_not_rescanned():
    result = resolve_command("run #{a}", {"a": {"default": "#{b}"}, "b": {"default": "X"}})
    assert result.command == "run #{b}"
    assert result.unresolved_arguments == []


def test_path_token_is_kept_intact_and_reported_once():
    result = resolve_command(
        "cp $PathToAtomicsFolder/a #{o} PathToAtomicsFolder\\b",
        {"o": {"default": "$PathToAtomicsFolder/o.bin"}},
    )
    assert result.command == (
        "cp $PathToAtomicsFolder/a $PathToAtomicsFolder/o.bin PathToAtomicsFolder\\b"
    )
    assert result.unresolved_arguments == [PATH_ARGUMENT_NAME]
    assert result.arguments_used == {"o": "$PathToAtomicsFolder/o.bin"}


def test_with_path_argument_adds_synthetic_argument_without_mutating():
    original = {"o": {"default": "x"}}
    extended = with_path_argument("cp $PathToAtomicsFolder/a", original)
    assert original == {"o": {"default": "x"}}
    assert extended[PATH_ARGUMENT_NAME]["default"] is None
    assert extended[PATH_ARGUMENT_NAME]["type"] == "path"
    assert with_path_argument("whoami", None) == {}
