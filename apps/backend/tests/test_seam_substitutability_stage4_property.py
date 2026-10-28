"""Property test for Stage 4 seam substitutability (task 7.5).

# Feature: ai-curation-agents, Property 6: Seam substitutability — the AI Stage 4
# implementation :class:`app.services.ai.agents.op_agent.AiOperationalizer`
# structurally satisfies the existing ``@runtime_checkable``
# :class:`~app.services.environment.operationalizer.OperationalizerByOrigin`
# Protocol with the EXACT method signatures the seam declares (same method names,
# parameter lists, parameter types and return types), so it is injectable in
# place of the curation counterpart
# (:class:`~app.services.environment.operationalizer.CurationOperationalizer`)
# with NO downstream change.

Validates: Requirements 2.2, 2.7

What the property pins down
---------------------------
Requirement 2.2 demands the Operationalization_Agent implement the
``OperationalizerByOrigin`` seam "exposing every method the seam declares, each
with matching name, parameter list, parameter types, and return type".
Requirement 2.7 demands the substituted agent be processed by the downstream
pipeline "using the same code path and produce the same output structure" with no
code modification — which is exactly what a structural (duck-typed) match of the
seam guarantees, since the downstream only ever sees the two seam operations.

So, for ANY instance of :class:`AiOperationalizer` (constructed with arbitrary
combinations of its optional lazily-injected collaborators — ``chat``, ``memory``,
``atomic_lookup``, ``parser``), this test asserts:

1. ``isinstance(agent, OperationalizerByOrigin)`` is ``True`` — the
   ``@runtime_checkable`` Protocol accepts the agent structurally (it exposes
   ``origin`` and ``operationalize`` as callables). This is the runtime seam
   check the design calls for (Property 6).
2. The agent exposes EVERY method the Protocol declares (``origin`` and
   ``operationalize``), each a callable.
3. For every seam method, the agent's ``inspect.signature`` matches the seam's
   signature EXACTLY — identical parameter names, kinds, order, per-parameter
   annotations and return annotation — compared against BOTH the
   ``OperationalizerByOrigin`` Protocol method AND the ``CurationOperationalizer``
   method it substitutes. Matching the curation counterpart's signature too is
   what makes the agent drop-in injectable with no downstream change (Req. 2.7).

Why ``inspect.signature`` and not just ``isinstance``
-----------------------------------------------------
``@runtime_checkable`` ``isinstance`` only checks that the named attributes
*exist* and are callable — it does NOT compare signatures. Requirement 2.2 is
explicitly about matching names, parameter lists, parameter types and return
types, so the signature comparison below is the substantive check;
``isinstance`` is the necessary (but not sufficient) structural gate.

Everything runs in-process: no network, no Docker, no PostgreSQL, no LLM — the
agent is constructed with its collaborators left unset or replaced by trivial
fakes, and no seam method is actually invoked (we only introspect signatures), so
the lazy-at-import contract (Req. 1.7) is preserved.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.ai.agents.op_agent import AiOperationalizer
from app.services.environment.operationalizer import (
    CurationOperationalizer,
    OperationalizerByOrigin,
)

# The two operations the ``OperationalizerByOrigin`` seam declares. Any seam
# implementation must expose exactly these, with matching signatures.
_SEAM_METHODS = ("origin", "operationalize")


# ---------------------------------------------------------------------------
# Trivial fakes for the agent's optional collaborators.
#
# The property is about the agent's *shape* (its method signatures), not its
# behaviour, so the collaborators never need to do anything real — and no seam
# method is invoked here. These fakes exist only so Hypothesis can exercise the
# full cross-product of "collaborator injected / left as default" construction
# paths, proving the agent's seam conformance is independent of how it was wired.
# ---------------------------------------------------------------------------


class _FakeChat:
    """A stand-in chat model (never invoked by this test)."""

    def invoke(self, prompt: str) -> Any:  # pragma: no cover - never called
        return "[]"


class _FakeMemory:
    """A stand-in RAG memory (never invoked by this test)."""

    def retrieve(self, *, campaign_id: str, query: str) -> list[dict]:  # pragma: no cover
        return []


def _fake_atomic_lookup(technique_id: str) -> list[dict]:  # pragma: no cover
    return []


def _fake_parser(command: str) -> dict:  # pragma: no cover
    return {}


def _normalize_annotation(annotation: Any) -> Any:
    """Collapse a type annotation to a quoting-insensitive canonical form.

    Under ``from __future__ import annotations`` every annotation is a *string*
    of its source form, so a forward reference the seam writes as a quoted
    literal (``"DagView"``) stringifies to ``"'DagView'"`` while the same type
    written unquoted elsewhere stringifies to ``"DagView"``. Both name the SAME
    type — the only difference is redundant source quoting — so comparing the
    raw strings would flag a mismatch that does not exist at the type level.

    This strips any surrounding single/double quotes (repeatedly, to handle the
    doubly-quoted forward-reference case) and trims whitespace, yielding a
    canonical form in which equal types compare equal regardless of how the
    forward reference was written. Non-string annotations (e.g. an actual type
    object or ``inspect.Parameter.empty``) pass through unchanged.
    """
    if not isinstance(annotation, str):
        return annotation
    text = annotation.strip()
    while len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
        text = text[1:-1].strip()
    return text


def _signatures_match(impl_method: Callable[..., Any], seam_method: Callable[..., Any]) -> bool:
    """Return ``True`` when two unbound methods share an identical signature.

    Compares, via :func:`inspect.signature`, the FULL parameter list (names,
    kinds, order and per-parameter annotations) and the return annotation. This
    is the Requirement 2.2 criterion: matching name (the caller already pairs by
    name), parameter list, parameter types and return type.

    Annotations are compared through :func:`_normalize_annotation` so that a
    forward reference remains a match whether the seam wrote it quoted
    (``"DagView"``) or the implementation wrote it unquoted (``DagView``) — the
    quoting is a source-level artifact of ``from __future__ import annotations``,
    not a difference in the referenced type (which is what Req. 2.2 cares about).

    Signatures are taken from the *unbound* functions on the class so that
    ``self`` participates uniformly on both sides (both are plain functions here),
    avoiding any asymmetry between a bound instance method and a Protocol method.
    """
    impl_sig = inspect.signature(impl_method)
    seam_sig = inspect.signature(seam_method)

    impl_params = list(impl_sig.parameters.values())
    seam_params = list(seam_sig.parameters.values())

    if len(impl_params) != len(seam_params):
        return False

    for impl_p, seam_p in zip(impl_params, seam_params):
        if impl_p.name != seam_p.name:
            return False
        if impl_p.kind != seam_p.kind:
            return False
        if _normalize_annotation(impl_p.annotation) != _normalize_annotation(
            seam_p.annotation
        ):
            return False
        if impl_p.default != seam_p.default:
            return False

    return _normalize_annotation(impl_sig.return_annotation) == _normalize_annotation(
        seam_sig.return_annotation
    )


@settings(max_examples=50)
@given(
    with_chat=st.booleans(),
    with_memory=st.booleans(),
    with_atomic=st.booleans(),
    with_parser=st.booleans(),
)
def test_ai_operationalizer_is_seam_substitutable(
    with_chat: bool,
    with_memory: bool,
    with_atomic: bool,
    with_parser: bool,
) -> None:
    """``AiOperationalizer`` structurally satisfies the Stage 4 seam (Property 6).

    For ANY combination of injected/omitted collaborators, the agent must:

    * pass the ``@runtime_checkable`` Protocol ``isinstance`` gate,
    * expose every seam method as a callable, and
    * match the seam's AND the curation counterpart's method signatures exactly
      (names, parameter list, parameter types, return type),

    so the downstream pipeline consumes it on the same code path as the curation
    operationalizer, with no modification (Req. 2.2, 2.7).
    """
    agent = AiOperationalizer(
        chat=_FakeChat() if with_chat else None,
        memory=_FakeMemory() if with_memory else None,  # type: ignore[arg-type]
        atomic_lookup=_fake_atomic_lookup if with_atomic else None,
        parser=_fake_parser if with_parser else None,
    )

    # 1. Runtime structural Protocol check (the design's `isinstance(agent, Protocol)`).
    assert isinstance(agent, OperationalizerByOrigin)

    for name in _SEAM_METHODS:
        # 2. Every declared seam method is present and callable on the agent.
        assert hasattr(agent, name), f"agent missing seam method {name!r}"
        assert callable(getattr(agent, name)), f"agent.{name} is not callable"

        impl_method = getattr(AiOperationalizer, name)
        protocol_method = getattr(OperationalizerByOrigin, name)
        curation_method = getattr(CurationOperationalizer, name)

        # 3a. The agent's signature matches the Protocol's declared signature.
        assert _signatures_match(impl_method, protocol_method), (
            f"AiOperationalizer.{name} signature does not match the "
            f"OperationalizerByOrigin seam: "
            f"{inspect.signature(impl_method)} vs {inspect.signature(protocol_method)}"
        )

        # 3b. The agent's signature matches the curation counterpart it replaces,
        # so it is drop-in injectable with no downstream change (Req. 2.7).
        assert _signatures_match(impl_method, curation_method), (
            f"AiOperationalizer.{name} signature does not match "
            f"CurationOperationalizer.{name} (not drop-in substitutable): "
            f"{inspect.signature(impl_method)} vs {inspect.signature(curation_method)}"
        )
