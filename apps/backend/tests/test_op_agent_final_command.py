"""Final_Command validation, atomic_defaults fallback and command_ref linkage.

Covers Requirement 12.7-12.10 (task 45.4). Deterministic: a fake chat model and
an in-memory pool, no network and no database.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.models.enums import TranslationSource
from app.services.ai.agents.flow_agent import FlowAgent
from app.services.ai.agents.op_agent import AiOperationalizer
from app.services.flow.flow_view import FlowNodeView, FlowView

_SLUG = "final-command"
_TEMPLATE = "curl http://#{target}:#{port}/ -o $PathToAtomicsFolder/out"
_STATIC = "curl http://192.168.20.30:80/ -o /tmp/out"


def _pool(_technique_id: str) -> list[dict]:
    return [
        {
            "technique_id": "T1190",
            "technique_name": "Exploit",
            "ability_id": "ab-T1190",
            "tactic": "initial-access",
            "name": "curl-exec",
            "platform": "linux",
            "command": _TEMPLATE,
            "command_ref": 42,
            "input_arguments": {
                "target": {"default": "10.0.0.1", "type": "string"},
                "port": {"default": None, "type": "integer"},
                "PathToAtomicsFolder": {"default": None, "type": "path"},
            },
        },
        {
            "technique_id": "T1190",
            "technique_name": "Exploit",
            "name": "static",
            "platform": "windows",
            "command": _STATIC,
            "command_ref": 43,
            "input_arguments": {},
        },
    ]


def _flow() -> FlowView:
    return FlowView(
        campaign_id=_SLUG,
        name="Final command",
        curated=True,
        can_advance=True,
        nodes=[
            FlowNodeView(
                node_id="n0",
                technique_id="T1190",
                technique_name="Exploit",
                tactic="initial-access",
            )
        ],
        edges=[],
    )


class _Chat:
    def __init__(self, payload: Any) -> None:
        self._text = payload if isinstance(payload, str) else json.dumps(payload)
        self.prompts: list[str] = []

    def invoke(self, prompt: Any) -> Any:
        self.prompts.append(str(prompt))

        class _Reply:
            content = self._text

        return _Reply()


def _run(chat: Any) -> tuple[Any, list[dict]]:
    sink: list[dict] = []
    agent = AiOperationalizer(chat=chat, atomic_lookup=_pool, trace_sink=sink)
    return agent.operationalize(_SLUG, _flow()), sink


def _executors(produced: Any) -> list[Any]:
    return [e for a in produced.abilities.abilities for e in a.executors]


def _log(sink: list[dict]) -> list[dict]:
    return sink[0]["processing"]["command_selection"] or []


def test_final_command_is_accepted_with_command_ref() -> None:
    final = "curl http://192.168.20.30:80/ -o /tmp/x"
    produced, sink = _run(_Chat([{"template": _TEMPLATE, "command": final}]))

    (executor,) = _executors(produced)
    assert executor.command == final
    assert executor.command_ref == 42
    assert produced.origin is TranslationSource.AI_GENERATED
    # Hosts are derived from the Final_Command, not from the template.
    assert [h.address for h in produced.parameters.hosts] == ["192.168.20.30"]
    assert any(e["event"] == "accepted" for e in _log(sink))


@pytest.mark.parametrize(
    "item, reason",
    [
        ({"template": "not in pool", "command": "echo hi"}, "template_not_in_pool"),
        (
            {"template": _TEMPLATE, "command": "curl http://#{target}/"},
            "command_has_unresolved_placeholders",
        ),
        (
            {"template": _TEMPLATE, "command": "cp x $PathToAtomicsFolder/y"},
            "command_has_unresolved_placeholders",
        ),
        ({"template": _TEMPLATE, "command": "  "}, "command_is_empty_or_not_text"),
        (_TEMPLATE, "command_has_unresolved_placeholders"),  # legacy string form
    ],
)
def test_invalid_items_are_discarded_with_reason_and_fall_back(item: Any, reason: str) -> None:
    produced, sink = _run(_Chat([item]))

    log = _log(sink)
    assert any(e["event"] == "discarded" and e["reason"] == reason for e in log)
    assert any(e["event"] == "fallback" and e["reason"] == "no_item_accepted" for e in log)
    selected = [e for e in log if e["event"] == "selected"]
    assert selected and all(e["arguments_source"] == "atomic_defaults" for e in selected)
    assert _executors(produced)  # the fallback still yields a proposal


def test_legacy_string_form_is_accepted_for_placeholder_free_template() -> None:
    produced, _ = _run(_Chat([_STATIC]))

    (executor,) = _executors(produced)
    assert executor.command == _STATIC
    assert executor.command_ref == 43


def test_non_json_reply_is_logged_and_falls_back() -> None:
    _, sink = _run(_Chat("not json"))

    assert any(e.get("reason") == "response_not_json" for e in _log(sink))


def test_fallback_without_chat_applies_defaults_and_records_source() -> None:
    produced, sink = _run(None)

    log = _log(sink)
    assert log[0] == {
        "node_id": "n0",
        "event": "fallback",
        "reason": "no_chat_model",
    }
    selected = [e for e in log if e["event"] == "selected"]
    assert {e["command_ref"] for e in selected} == {42, 43}
    templated = next(e for e in selected if e["command_ref"] == 42)
    assert templated["arguments_source"] == "atomic_defaults"
    assert templated["unresolved_arguments"] == ["port", "PathToAtomicsFolder"]

    by_ref = {e.command_ref: e.command for e in _executors(produced)}
    assert by_ref[42] == "curl http://10.0.0.1:#{port}/ -o $PathToAtomicsFolder/out"
    assert by_ref[43] == _STATIC


def test_flow_agent_prefers_command_ref_over_text_equality() -> None:
    produced, _ = _run(
        _Chat([{"template": _TEMPLATE, "command": "curl http://192.168.20.30/ -o /tmp/x"}])
    )
    op = produced.model_dump(mode="json")

    def resolver(_tid: str, _command: str | None) -> int | None:
        return 999  # would be wrong: text equality no longer holds

    agent = FlowAgent(operationalization=op, command_ref_resolver=resolver)
    index = agent._command_index()
    assert index is not None
    assert index[1]["T1190"] == 42


def test_flow_agent_resolver_still_used_without_command_ref() -> None:
    op = {
        "abilities": {
            "abilities": [{"technique_id": "T1190", "executors": [{"command": "whoami"}]}]
        }
    }
    agent = FlowAgent(operationalization=op, command_ref_resolver=lambda _t, _c: 7)
    index = agent._command_index()
    assert index is not None and index[1]["T1190"] == 7
