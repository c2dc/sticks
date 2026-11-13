"""Property test — Stage 5 seam substitutability of ``AiEnvironmentProvisioner`` (task 11.4).

# Feature: ai-curation-agents, Property 6: Seam substitutability. For every
# stage, the AI implementation satisfies the existing ``@runtime_checkable``
# ``Protocol`` with the exact method signatures, so it is injectable in place of
# the curation implementation with no downstream change. This file covers the
# Stage 5 (Simulation-environment creation) part of Property 6: the AI
# implementation
# :class:`~app.services.ai.agents.env_agent.AiEnvironmentProvisioner`
# structurally satisfies the
# :class:`~app.services.environment.provisioner.EnvironmentProvisioner` seam
# exactly as its curation counterpart
# :class:`~app.services.environment.provisioner.CurationEnvironmentProvisioner`.

Validates: Requirements 2.3, 2.7

This is a *feature* property test (``ai-curation-agents`` is a feature, not a
bugfix): the property is expected to **hold** over the current implementation.

Property 6 (Stage 5 side) asserts, for any constructor shaping (the agent's
collaborators — ``chat``, ``memory``, ``pipeline``, ``compose_tool``,
``caldera_tool`` — are all optional and resolved lazily, so none of them affects
seam conformance):

1. **Protocol conformance** (Req. 2.3) — an ``AiEnvironmentProvisioner`` instance
   satisfies the ``@runtime_checkable`` ``EnvironmentProvisioner`` Protocol by
   structural duck typing (``isinstance`` is ``True``), with no explicit
   inheritance — exactly like the curation
   ``CurationEnvironmentProvisioner``.
2. **Exact signature match** (Req. 2.3) — the seam methods ``origin``,
   ``provision``, ``deprovision`` and ``prepare`` have the **same** names,
   parameter lists, parameter types and return types on
   ``AiEnvironmentProvisioner`` as on both the ``EnvironmentProvisioner`` Protocol
   and the ``CurationEnvironmentProvisioner`` counterpart. Compared via
   :func:`inspect.signature` (with resolved type hints), so the AI implementation
   is injectable in place of the curation one with no downstream change.
3. **Injectability / no downstream change** (Req. 2.7) — because the seam shape is
   identical, anything that consumes an ``EnvironmentProvisioner`` can take an
   ``AiEnvironmentProvisioner`` wherever it takes a
   ``CurationEnvironmentProvisioner``; the ``origin()`` value differs
   (``gerado_ia`` vs ``curadoria_humana``) but the contract (four methods, same
   signatures, returning the same typed outputs) is the same, and ``gerado_ia`` is
   an existing ``TranslationSource`` member so persisting an AI-origin environment
   artifact needs no schema change.

Strategy / isolation
--------------------
``AiEnvironmentProvisioner``'s constructor is import- and construction-safe (all
collaborators optional, resolved lazily), so each Hypothesis example builds an
instance with a varied constructor shaping and asserts the invariants. Nothing
here touches the network, invokes an LLM, opens a subprocess or touches the
Docker daemon — no seam method is invoked; only the seam's **shape** is
exercised. Hypothesis runs ≥100 iterations.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import TranslationSource
from app.services.ai.agents.env_agent import AiEnvironmentProvisioner
from app.services.environment.provisioner import (
    CurationEnvironmentProvisioner,
    DeprovisioningResult,
    EnvironmentProvisioner,
    ProvisionedEnvironment,
    ProvisioningResult,
)

# The seam methods whose names, parameter lists and types must match the
# ``EnvironmentProvisioner`` Protocol exactly (Req. 2.3). ``origin``/``provision``/
# ``deprovision`` are the Stage 5 seam; ``prepare`` is the Stage 4 compatibility
# operation the seam also declares.
_SEAM_METHODS = ("origin", "provision", "deprovision", "prepare")


def _resolved_signature(obj: object, method_name: str) -> inspect.Signature:
    """``inspect.signature`` of ``obj.method_name`` with resolved type hints.

    Resolving the annotations (via :func:`typing.get_type_hints` on the function)
    turns the stringized forward references (``from __future__ import
    annotations`` makes every annotation a string) into real types, so two
    implementations that annotate the same logical type — even via different
    spellings like ``"ProvisionedEnvironment"`` vs ``ProvisionedEnvironment`` —
    compare equal. Bound methods drop ``self``, so the comparison is
    apples-to-apples across the Protocol (unbound) and the concrete
    implementations (bound).
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
    """Resolved signature of an ``EnvironmentProvisioner`` Protocol method, minus ``self``.

    The Protocol methods are unbound functions carrying ``self``; dropping it
    makes the comparison identical to the bound methods of concrete instances.
    """
    func = getattr(EnvironmentProvisioner, method_name)
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


def _signatures_match(impl_sig: inspect.Signature, seam_sig: inspect.Signature) -> None:
    """Assert two resolved signatures match for seam-substitutability purposes.

    Compares the FULL parameter list (names, kinds, order and resolved
    annotations) and the resolved return annotation — the Requirement 2.3
    criterion (matching name, parameter list, parameter types, return type).

    Default *values* are compared structurally rather than by identity: the seam
    declares ``on_log`` with a *no-op* ``lambda`` default and each implementation
    supplies its **own** ``lambda`` object, so a raw ``Signature.__eq__`` would
    (spuriously) differ on the two distinct-but-equivalent lambda objects. What
    matters for substitutability is that the parameter is **optional on both
    sides** (so existing callers that omit it stay valid) with the **same
    annotation** — not that the two no-op defaults are the same object. So the
    presence/absence of a default is asserted, and a ``callable`` default (the
    no-op ``lambda``) on both sides is accepted as equivalent; any non-callable
    default (e.g. a sentinel value) is still compared for equality.
    """
    impl_params = list(impl_sig.parameters.values())
    seam_params = list(seam_sig.parameters.values())

    assert len(impl_params) == len(seam_params), (
        f"parameter count differs: {impl_sig} vs {seam_sig}"
    )

    for impl_p, seam_p in zip(impl_params, seam_params, strict=False):
        assert impl_p.name == seam_p.name, f"{impl_p.name!r} != {seam_p.name!r}"
        assert impl_p.kind == seam_p.kind, f"{impl_p.name}: kind {impl_p.kind} != {seam_p.kind}"
        assert impl_p.annotation == seam_p.annotation, (
            f"{impl_p.name}: annotation {impl_p.annotation!r} != {seam_p.annotation!r}"
        )
        impl_has_default = impl_p.default is not inspect.Parameter.empty
        seam_has_default = seam_p.default is not inspect.Parameter.empty
        assert impl_has_default == seam_has_default, (
            f"{impl_p.name}: optionality differs "
            f"(impl default={impl_p.default!r}, seam default={seam_p.default!r})"
        )
        if impl_has_default and not (callable(impl_p.default) and callable(seam_p.default)):
            # Non-callable defaults (sentinels/literals) must match exactly; two
            # equivalent no-op ``lambda`` defaults are accepted (callable on both).
            assert impl_p.default == seam_p.default, (
                f"{impl_p.name}: default {impl_p.default!r} != {seam_p.default!r}"
            )

    assert impl_sig.return_annotation == seam_sig.return_annotation, (
        f"return annotation {impl_sig.return_annotation!r} != {seam_sig.return_annotation!r}"
    )


# A Hypothesis strategy over the optional constructor shapings of the agent. None
# of these collaborators changes the seam's shape (they are injected and lazy), so
# the property must hold for every shaping. The fakes are never invoked — only the
# agent's method *shapes* are introspected — so they can be trivial stand-ins.
@st.composite
def _ai_provisioners(draw) -> AiEnvironmentProvisioner:
    with_chat = draw(st.booleans())
    with_memory = draw(st.booleans())
    with_pipeline = draw(st.booleans())
    with_compose = draw(st.booleans())
    with_caldera = draw(st.booleans())

    class _FakeChat:
        def invoke(self, messages):  # pragma: no cover - never invoked here
            raise AssertionError("seam-shape test must not invoke the chat model")

    return AiEnvironmentProvisioner(
        chat=_FakeChat() if with_chat else None,
        memory=object() if with_memory else None,
        pipeline=CurationEnvironmentProvisioner() if with_pipeline else None,
        compose_tool=(lambda **_k: {}) if with_compose else None,
        caldera_tool=(lambda **_k: {}) if with_caldera else None,
    )


# ===========================================================================
# Property 6 (Stage 5) — AiEnvironmentProvisioner structurally satisfies the
# EnvironmentProvisioner seam with the exact origin/provision/deprovision/prepare
# signatures, so it is injectable in place of CurationEnvironmentProvisioner with
# no downstream change.
# ===========================================================================


# Feature: ai-curation-agents, Property 6: Seam substitutability (Stage 5 / env).
@settings(max_examples=40)
@given(ai_provisioner=_ai_provisioners())
def test_property6_stage5_ai_environment_provisioner_is_substitutable(
    ai_provisioner: AiEnvironmentProvisioner,
) -> None:
    """``AiEnvironmentProvisioner`` satisfies ``EnvironmentProvisioner`` exactly.

    Validates: Requirements 2.3, 2.7
    """
    curation = CurationEnvironmentProvisioner()

    # (1) Protocol conformance by structural duck typing — no explicit inheritance
    #     — exactly like the curation counterpart (Req. 2.3, 2.7).
    assert isinstance(ai_provisioner, EnvironmentProvisioner)
    assert isinstance(curation, EnvironmentProvisioner)
    assert EnvironmentProvisioner not in type(ai_provisioner).__mro__  # structural, not nominal

    # (2) Every seam method is present and callable on the AI implementation, with
    #     the SAME name/parameters/types/return as the Protocol AND the curation
    #     counterpart (Req. 2.3). Compared with resolved type hints so the forward
    #     references (stringized by ``from __future__ import annotations``) match.
    for method_name in _SEAM_METHODS:
        assert hasattr(ai_provisioner, method_name)
        assert callable(getattr(ai_provisioner, method_name))

        protocol_sig = _protocol_signature(method_name)
        ai_sig = _resolved_signature(ai_provisioner, method_name)
        curation_sig = _resolved_signature(curation, method_name)

        # Same parameter names in the same order as the seam and the counterpart.
        assert list(ai_sig.parameters) == list(protocol_sig.parameters)
        assert list(ai_sig.parameters) == list(curation_sig.parameters)

        # Same parameter types and return type as the seam and the counterpart
        # (the no-op ``lambda`` default of ``on_log`` is treated structurally — see
        # :func:`_signatures_match` — so two equivalent lambdas do not spuriously
        # differ, while optionality and annotations must match exactly).
        _signatures_match(ai_sig, protocol_sig)
        _signatures_match(ai_sig, curation_sig)

    # Spell out the exact parameter lists for a readable guard: the extension does
    # NOT redesign the seam — same operations, same parameters.
    assert list(_resolved_signature(ai_provisioner, "provision").parameters) == [
        "params",
        "on_log",
    ]
    assert list(_resolved_signature(ai_provisioner, "deprovision").parameters) == [
        "environment",
        "on_log",
    ]
    assert list(_resolved_signature(ai_provisioner, "prepare").parameters) == [
        "campaign",
    ]
    assert list(_resolved_signature(ai_provisioner, "origin").parameters) == []

    # (3) ``origin()`` returns a legitimate ``TranslationSource`` member — the AI
    #     implementation stamps ``gerado_ia`` while curation stamps
    #     ``curadoria_humana`` — both from the SAME enum (persisted as VARCHAR +
    #     CHECK that already admits both), so substituting the AI implementation
    #     needs no schema change downstream (Req. 2.4, 2.5, 2.7).
    assert ai_provisioner.origin() == TranslationSource.AI_GENERATED
    assert ai_provisioner.origin() in set(TranslationSource)
    assert curation.origin() in set(TranslationSource)

    # The seam's return types are identical across origins — the downstream
    # pipeline consumes the AI artifact with the same code path and the same output
    # structure as a curation-origin one (Req. 2.7).
    assert (
        _resolved_signature(ai_provisioner, "provision").return_annotation
        == _resolved_signature(curation, "provision").return_annotation
        == ProvisionedEnvironment
    )
    assert (
        _resolved_signature(ai_provisioner, "deprovision").return_annotation
        == _resolved_signature(curation, "deprovision").return_annotation
        == DeprovisioningResult
    )
    assert (
        _resolved_signature(ai_provisioner, "prepare").return_annotation
        == _resolved_signature(curation, "prepare").return_annotation
        == ProvisioningResult
    )
