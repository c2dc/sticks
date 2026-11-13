"""Property test: the Path_Argument token is never substituted.

Feature: agentic-only-flow-redesign, Property 22: The Path_Argument token is
never substituted.

**Validates: Requirements 12.7, 12.11, 12.13**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.ai.agents.op_agent import _has_leftover_tokens
from app.services.atomic_arguments import PATH_ARGUMENT_NAME, resolve_command

_names = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
_literal = st.text(alphabet="abc XYZ./-=;\\", max_size=8)
_token = st.sampled_from(["PathToAtomicsFolder", "$PathToAtomicsFolder"])
_default = st.one_of(
    st.none(),
    _literal,
    st.builds(lambda t, s: t + s, _token, _literal),
    st.builds(lambda a, t, b: a + t + b, _literal, _token, _literal),
)


@st.composite
def _scenarios(draw):
    names = draw(st.lists(_names, min_size=1, max_size=4, unique=True))
    args = {}
    for n in names:
        kind = draw(st.sampled_from(["default", "missing"]))
        if kind == "default":
            args[n] = {"default": draw(_default)}
    segments = draw(
        st.lists(
            st.one_of(
                _literal.map(lambda s: ("lit", s)),
                _token.map(lambda t: ("tok", t)),
                st.sampled_from(names).map(lambda n: ("ph", n)),
            ),
            max_size=8,
        )
    )
    return args, segments


@settings(max_examples=100, deadline=None)
@given(scenario=_scenarios())
def test_path_token_preserved_and_reported_once(scenario):
    """Feature: agentic-only-flow-redesign, Property 22: The Path_Argument
    token is never substituted."""
    args, segments = scenario
    template = ""
    expected = ""
    for kind, value in segments:
        if kind == "ph":
            template += "#{" + value + "}"
            default = (args.get(value) or {}).get("default")
            expected += "#{" + value + "}" if default is None else default
        else:
            template += value
            expected += value

    result = resolve_command(template, args)

    # Tokens in the template and in substituted defaults survive byte for byte.
    assert result.command == expected
    has_token = "PathToAtomicsFolder" in result.command
    assert result.unresolved_arguments.count(PATH_ARGUMENT_NAME) == (1 if has_token else 0)


@settings(max_examples=100, deadline=None)
@given(scenario=_scenarios())
def test_validator_rejects_leftover_placeholders_and_token(scenario):
    """Feature: agentic-only-flow-redesign, Property 22: The Path_Argument
    token is never substituted."""
    _args, segments = scenario
    template = "".join(("#{" + v + "}") if k == "ph" else v for k, v in segments)
    expect_rejected = "#{" in template or "PathToAtomicsFolder" in template
    assert _has_leftover_tokens(template) == expect_rejected
