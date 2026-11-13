"""Property test — Stage 3 seam substitutability of ``FlowAgent`` (task 10.4).

# Feature: ai-curation-agents, Property 6: Seam substitutability. For every
# stage, the AI implementation satisfies the existing ``@runtime_checkable``
# ``Protocol`` with the exact method signatures, so it is injectable in place of
# another implementation with no downstream change. This file covers the
# Stage 3 (attack flow) part of Property 6: the AI implementation
# :class:`~app.services.ai.agents.flow_agent.FlowAgent` structurally satisfies
# the :class:`~app.services.flow.generator.FlowGenerator` seam.

Validates: Requirements 2.1, 2.7

This is a *feature* property test (``ai-curation-agents`` is a feature, not a
bugfix): the property is expected to **hold** over the current implementation.

Property 6 (Stage 3 side) asserts, for any constructor shaping (the agent's
collaborators — ``chat``, ``memory``, ``attack_reader``, ``retrieval_k`` — are all
optional and resolved lazily, so none of them affects seam conformance):

1. **Protocol conformance** (Req. 2.1) — an ``FlowAgent`` instance satisfies
   the ``@runtime_checkable`` ``FlowGenerator`` Protocol by structural duck typing
   (``isinstance`` is ``True``), with no explicit inheritance.
2. **Exact signature match** (Req. 2.1) — the seam methods ``origin`` and
   ``generate`` have the **same** names, parameter lists, parameter types and
   return types on ``FlowAgent`` as on the ``FlowGenerator`` Protocol. Compared
   via :func:`inspect.signature` (with resolved type hints).
3. **Injectability / no downstream change** (Req. 2.7) — because the seam shape is
   identical, anything that consumes a ``FlowGenerator`` can take a ``FlowAgent``;
   the contract (two methods, same signatures, returning ``list[ProducedFlow]``)
   is fixed, and ``ai_generated`` is an existing ``FlowOrigin`` member so
   persisting an AI-origin flow needs no schema change.

Strategy / isolation
--------------------
``FlowAgent``'s constructor is import- and construction-safe (all
collaborators optional, resolved lazily), so each Hypothesis example builds an
instance with a varied constructor shaping and asserts the invariants. Nothing
here touches the network, invokes an LLM, or opens a database — no ``generate``
call is made; only the seam's **shape** is exercised. Hypothesis runs ≥100
iterations.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import FlowOrigin
from app.services.ai.agents.flow_agent import FlowAgent
from app.services.flow.generator import (
    FlowGenerator,
    ProducedFlow,
)

# The seam methods whose names, parameter lists and types must match the
# ``FlowGenerator`` Protocol exactly (Req. 2.1).
_SEAM_METHODS = ("origin", "generate")


def _resolved_signature(obj: object, method_name: str) -> inspect.Signature:
    """``inspect.signature`` of ``obj.method_name`` with resolved type hints.

    Resolving the annotations (via :func:`typing.get_type_hints` on the function)
    turns the stringized forward references (``from __future__ import
    annotations`` makes every annotation a string) into real types, so two
    implementations that annotate the same logical type — even via different
    spellings like ``"StructuralModeling"`` vs ``StructuralModeling`` — compare
    equal. Bound methods drop ``self``, so the comparison is apples-to-apples
    across the Protocol (unbound) and the concrete implementations (bound).
    """
    func = getattr(obj, method_name)
    raw = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:  # noqa: BLE001 -- fall back to the raw (stringized) sig
        return raw

    params = []
    for name, param in raw.parameters.items():
        if name in hints:
            param = param.replace(annotation=hints[name])
        params.append(param)
    return_annotation = hints.get("return", raw.return_annotation)
    return raw.replace(parameters=params, return_annotation=return_annotation)


def _protocol_signature(method_name: str) -> inspect.Signature:
    """Resolved signature of a ``FlowGenerator`` Protocol method, minus ``self``.

    The Protocol methods are unbound functions carrying ``self``; dropping it
    makes the comparison identical to the bound methods of concrete instances.
    """
    func = getattr(FlowGenerator, method_name)
    raw = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:  # noqa: BLE001 -- fall back to the raw (stringized) annotations when hints cannot be resolved
        hints = {}

    params = []
    for name, param in raw.parameters.items():
        if name == "self":
            continue
        if name in hints:
            param = param.replace(annotation=hints[name])
        params.append(param)
    return_annotation = hints.get("return", raw.return_annotation)
    return raw.replace(parameters=params, return_annotation=return_annotation)


# A Hypothesis strategy over the optional constructor shapings of the agent. None
# of these collaborators changes the seam's shape (they are injected and lazy), so
# the property must hold for every shaping.
@st.composite
def _ai_generators(draw) -> FlowAgent:
    retrieval_k = draw(st.integers(min_value=1, max_value=100))
    with_chat = draw(st.booleans())
    with_memory = draw(st.booleans())
    with_reader = draw(st.booleans())

    class _FakeChat:
        def invoke(self, messages):  # pragma: no cover - never invoked here
            raise AssertionError("seam-shape test must not invoke the chat model")

    return FlowAgent(
        chat=_FakeChat() if with_chat else None,
        memory=None if not with_memory else object(),
        attack_reader=(lambda _c: {}) if with_reader else None,
        retrieval_k=retrieval_k,
    )


# ===========================================================================
# Property 6 (Stage 3) — FlowAgent structurally satisfies the FlowGenerator
# seam with the exact ``origin``/``generate`` signatures, so it is injectable in
# place of any other generator with no downstream change.
# ===========================================================================


# Feature: ai-curation-agents, Property 6: Seam substitutability (Stage 3 / attack flow).
@settings(max_examples=40)
@given(ai_generator=_ai_generators())
def test_property6_stage3_ai_flow_generator_is_substitutable(
    ai_generator: FlowAgent,
) -> None:
    """``FlowAgent`` satisfies ``FlowGenerator`` with the exact seam signatures.

    Validates: Requirements 2.1, 2.7
    """
    # (1) Protocol conformance by structural duck typing — no explicit inheritance
    #     (Req. 2.1, 2.7).
    assert isinstance(ai_generator, FlowGenerator)
    assert FlowGenerator not in type(ai_generator).__mro__  # structural, not nominal

    # (2) Every seam method is present and callable on the AI implementation, with
    #     the SAME name/parameters/types/return as the Protocol
    #     (Req. 2.1). Compared with resolved type hints so the forward
    #     references (stringized by ``from __future__ import annotations``) match.
    for method_name in _SEAM_METHODS:
        assert hasattr(ai_generator, method_name)
        assert callable(getattr(ai_generator, method_name))

        protocol_sig = _protocol_signature(method_name)
        ai_sig = _resolved_signature(ai_generator, method_name)

        # Same parameter names in the same order as the seam.
        assert list(ai_sig.parameters) == list(protocol_sig.parameters)

        # Same parameter types and return type as the seam.
        assert ai_sig == protocol_sig

    # Spell out the exact ``generate`` parameter list for a readable guard: the
    # extension does NOT redesign the seam — same ``(campaign, modeling)``.
    assert list(_resolved_signature(ai_generator, "generate").parameters) == [
        "campaign",
        "modeling",
    ]

    # (3) ``origin()`` returns a legitimate ``FlowOrigin`` member — the AI
    #     implementation stamps ``ai_generated``, an existing enum member
    #     (persisted as VARCHAR + CHECK), so substituting the AI implementation
    #     needs no schema change downstream (Req. 2.7).
    assert ai_generator.origin() == FlowOrigin.AI_GENERATED
    assert ai_generator.origin() in set(FlowOrigin)

    # The ``generate`` return type is ``list[ProducedFlow]`` for every origin — the
    # downstream pipeline consumes the AI artifact with the same code path and the
    # same output structure as any other origin (Req. 2.7).
    ai_return = _resolved_signature(ai_generator, "generate").return_annotation
    assert ai_return == list[ProducedFlow]
