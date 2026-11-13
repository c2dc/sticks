"""Property test: the agent context carries the template, not the substituted command.

Feature: agentic-only-flow-redesign, Property 21: The agent context carries the
template and never the substituted command.

**Validates: Requirements 12.2, 12.3, 12.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.ai.command_context import (
    build_agent_command_context,
    render_candidates,
)
from app.services.atomic_arguments import resolve_command

_names = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
_text = st.text(alphabet="abc XYZ./-=;\n\t$\\", max_size=12)
_default = st.one_of(
    st.none(),
    _text,
    st.just("$PathToAtomicsFolder/payload.bin"),
    st.just("PathToAtomicsFolder\\payload.exe"),
)


@st.composite
def _arg_specs(draw):
    return {
        "description": draw(_text),
        "type": draw(st.sampled_from(["string", "path", "url", "integer"])),
        "default": draw(_default),
    }


@st.composite
def _entries(draw):
    names = draw(st.lists(_names, max_size=4, unique=True))
    args = {n: draw(_arg_specs()) for n in names}
    placeholders = " ".join("#{" + n + "}" for n in names)
    # The "cmd " prefix keeps the template non-blank; the rest is arbitrary text.
    template = "cmd " + draw(_text) + placeholders + draw(_text)
    return {
        "command": template,
        "platform": draw(st.sampled_from(["linux", "windows", "macos"])),
        "name": draw(st.sampled_from(["sh", "bash", "powershell", "command_prompt"])),
        "input_arguments": args,
    }


@settings(max_examples=100, deadline=None)
@given(pool=st.lists(_entries(), max_size=5))
def test_context_carries_template_and_arguments(pool):
    """Feature: agentic-only-flow-redesign, Property 21: The agent context
    carries the template and never the substituted command."""
    candidates = build_agent_command_context(pool)

    assert len(candidates) == len(pool)
    rendered = render_candidates(candidates)
    for entry, candidate in zip(pool, candidates, strict=False):
        assert candidate.template == entry["command"]
        assert set(candidate.arguments) == set(entry["input_arguments"])
        for name, spec in entry["input_arguments"].items():
            got = candidate.arguments[name]
            assert got is not None
            assert got["description"] == spec["description"]
            assert got["type"] == spec["type"]
            assert got["default"] == spec["default"]
        assert entry["command"] in rendered


@settings(max_examples=100, deadline=None)
@given(
    count=st.integers(min_value=1, max_value=4),
    prefixes=st.lists(_text, min_size=4, max_size=4),
)
def test_context_never_contains_substituted_command(count, prefixes):
    """Feature: agentic-only-flow-redesign, Property 21: The agent context
    carries the template and never the substituted command."""
    pool = []
    for i in range(count):
        marker = f"MARKER_{i}_UNIQUE"
        pool.append(
            {
                "command": f"run {prefixes[i]} #{{arg}} done",
                "platform": "linux",
                "name": "sh",
                "input_arguments": {
                    "arg": {"description": "d", "type": "string", "default": marker}
                },
            }
        )

    rendered = render_candidates(build_agent_command_context(pool))

    for entry in pool:
        substituted = resolve_command(entry["command"], entry["input_arguments"]).command
        assert substituted != entry["command"]
        assert substituted not in rendered
        assert entry["command"] in rendered
