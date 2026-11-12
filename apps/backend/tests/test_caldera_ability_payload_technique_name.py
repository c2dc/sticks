"""Bug-condition exploration test for the Caldera ability payload ``technique_name``.

# Feature: ai-run-caldera-ability-technique-name
# Task 1: Bug condition exploration test (EXPECTED TO FAIL on the unfixed code)

**Property 1: Bug Condition** — Caldera ability payload always carries a non-empty
``technique_name``.

This module encodes the *expected* behavior of the ``_ability_payloads`` projection: for every
ability source that satisfies ``isBugCondition`` (no non-None, non-empty ``technique_name``
attribute), the projection MUST still produce a payload whose ``"technique_name"`` key is present
and non-empty (not ``None``, not an empty/whitespace-only string).

On the **current (unfixed)** code this test is EXPECTED TO FAIL, and that failure confirms the bug
described in ``design.md`` (Hypothesized Root Cause): both copies of ``_ability_payloads`` copy
``technique_name`` only when ``getattr(ability, "technique_name", None)`` is not ``None``, and
``Stage2AbilityView`` has no such field, so the key is silently dropped. When the fix lands
(Task 3), the SAME test should PASS.

The projection is deterministic and needs no live Caldera, so the bug condition is exercised
directly against both copies of ``_ability_payloads``:
  - ``OperationRunner._ability_payloads``
    (``apps/backend/app/services/operation/runner.py``)
  - ``CurationEnvironmentProvisioner._ability_payloads``
    (``apps/backend/app/services/environment/provisioner.py``)

**Validates: Requirements 1.1, 1.2**
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.operation.runner import OperationRunner
from app.services.emulation.views import Stage2AbilityView

# The two duplicated projections under test. Both are static methods, so each can be
# invoked directly on a sequence of ability objects. Parametrizing across both proves the
# bug (and, after the fix, the guarantee) holds identically in both copies.
_PROJECTIONS: list[tuple[str, Callable[[Any], list[Any]]]] = [
    ("OperationRunner", OperationRunner._ability_payloads),
    ("CurationEnvironmentProvisioner", CurationEnvironmentProvisioner._ability_payloads),
]
_PROJECTION_IDS = [name for name, _ in _PROJECTIONS]


def _assert_non_empty_technique_name(payload: dict) -> None:
    """Assert the projected payload carries a present, non-empty ``technique_name``.

    This is the invariant the fix must guarantee (design Property 1): the key is present,
    the value is a non-``None`` string, and it is not empty/whitespace-only.
    """
    assert "technique_name" in payload, (
        "counterexample: projected Caldera ability payload OMITS the 'technique_name' "
        f"key; Caldera raises KeyError: 'technique_name'. payload={payload!r}"
    )
    value = payload["technique_name"]
    assert value is not None, (
        f"counterexample: 'technique_name' is None in the payload. payload={payload!r}"
    )
    assert isinstance(value, str) and value.strip() != "", (
        "counterexample: 'technique_name' is empty/whitespace-only and unusable for "
        f"Caldera. payload={payload!r}"
    )


class _DuckAbility:
    """A minimal duck-typed ability source (not a ``Stage2AbilityView``).

    Only the attributes passed at construction time are exposed; everything else is absent,
    letting us model sources that either expose ``technique_name = ""`` or omit it entirely.
    """

    def __init__(self, **attrs: Any) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)


# -- Explicit bug-condition test cases (from design Testing Strategy) ---------------------


@pytest.mark.parametrize("projection", [p for _, p in _PROJECTIONS], ids=_PROJECTION_IDS)
def test_stage2_ability_view_payload_carries_technique_name(projection) -> None:
    """Case 1: a ``Stage2AbilityView`` (no ``technique_name`` field) must yield a non-empty one.

    EXPECTED to FAIL on unfixed code: ``Stage2AbilityView`` has no ``technique_name`` attribute,
    so ``getattr(..., "technique_name", None)`` is ``None`` and the key is omitted.
    """
    ability = Stage2AbilityView(
        ability_id="ab-1",
        name="Web Service",
        technique_id="T1102",
        tactic="command-and-control",
        description="An AI-operationalized ability.",
    )

    payloads = projection([ability])

    assert len(payloads) == 1
    _assert_non_empty_technique_name(dict(payloads[0]))


@pytest.mark.parametrize("projection", [p for _, p in _PROJECTIONS], ids=_PROJECTION_IDS)
def test_empty_string_source_payload_carries_non_empty_technique_name(projection) -> None:
    """Case 2: a duck-typed source with ``technique_name = ""`` must be treated as missing.

    EXPECTED to FAIL on unfixed code: the empty string is not ``None``, so it is copied verbatim
    into the payload — still unusable for Caldera.
    """
    ability = _DuckAbility(
        ability_id="ab-2",
        name="Web Service",
        technique_id="T1102",
        tactic="command-and-control",
        description="Duck-typed source with empty technique_name.",
        technique_name="",
    )

    payloads = projection([ability])

    assert len(payloads) == 1
    _assert_non_empty_technique_name(dict(payloads[0]))


@pytest.mark.parametrize("projection", [p for _, p in _PROJECTIONS], ids=_PROJECTION_IDS)
def test_edge_case_no_name_and_no_technique_id_still_yields_value(projection) -> None:
    """Edge case: an AI ability lacking BOTH ``name`` and ``technique_id`` and ``technique_name``.

    EXPECTED to FAIL on unfixed code: there is nothing to copy and no fallback, so the key is
    omitted. After the fix a deterministic sentinel must still be produced.
    """
    ability = _DuckAbility(
        ability_id="ab-3",
        tactic="command-and-control",
        description="AI ability lacking name, technique_id and technique_name.",
    )

    payloads = projection([ability])

    assert len(payloads) == 1
    _assert_non_empty_technique_name(dict(payloads[0]))


# -- Property-based bug condition check (Property 1) --------------------------------------

# A value that is "missing" from the invariant's point of view: None, "", or whitespace-only.
_missing_technique_name = st.one_of(
    st.none(),
    st.just(""),
    st.text(alphabet=" \t\n", min_size=1, max_size=5),
)

# Optional plain text for name / technique_id, which may themselves be absent/None so the
# edge case (no fallback source) is also generated.
_optional_text = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=20,
    ),
)


@st.composite
def _bug_condition_abilities(draw: st.DrawFn) -> _DuckAbility:
    """Generate an ability source where ``isBugCondition`` is true.

    ``technique_name`` is always missing (absent, None, empty or whitespace), so the current
    projection must drop or copy an unusable value. ``name`` and ``technique_id`` are free to be
    present or absent so the fallback edge case is also covered.
    """
    attrs: dict[str, Any] = {"ability_id": draw(st.text(min_size=1, max_size=8))}

    name = draw(_optional_text)
    if name is not None:
        attrs["name"] = name
    technique_id = draw(_optional_text)
    if technique_id is not None:
        attrs["technique_id"] = technique_id

    # Decide whether technique_name is entirely absent or present-but-empty.
    if draw(st.booleans()):
        attrs["technique_name"] = draw(_missing_technique_name)

    return _DuckAbility(**attrs)


@settings(max_examples=40)
@given(ability=_bug_condition_abilities())
def test_property_operation_runner_bug_condition_yields_non_empty_technique_name(
    ability: _DuckAbility,
) -> None:
    """Property 1 (OperationRunner copy): any bug-condition source yields a non-empty value.

    EXPECTED to FAIL on unfixed code. **Validates: Requirements 1.1, 1.2**
    """
    payloads = OperationRunner._ability_payloads([ability])
    assert len(payloads) == 1
    _assert_non_empty_technique_name(dict(payloads[0]))


@settings(max_examples=40)
@given(ability=_bug_condition_abilities())
def test_property_provisioner_bug_condition_yields_non_empty_technique_name(
    ability: _DuckAbility,
) -> None:
    """Property 1 (CurationEnvironmentProvisioner copy): same guarantee in both copies.

    EXPECTED to FAIL on unfixed code. **Validates: Requirements 1.1, 1.2**
    """
    payloads = CurationEnvironmentProvisioner._ability_payloads([ability])
    assert len(payloads) == 1
    _assert_non_empty_technique_name(dict(payloads[0]))


# =========================================================================================
# Task 2: Preservation property tests for already-complete ability sources
#
# **Property 2: Preservation** — Already-complete payloads unchanged.
#
# These tests follow the *observation-first* methodology (design Testing Strategy >
# Preservation Checking): they capture the payload produced by the CURRENT (unfixed)
# projection for every ``isBugCondition``-false source (one carrying a non-empty
# ``technique_name``) and assert that the projection reproduces it byte-for-byte, together
# with every other projected field (``ability_id``, ``name``, ``tactic``, ``technique_id``,
# ``description``, ``executors``).
#
# The fixed unified helper does not exist yet, so the baseline is captured against the
# current projection output directly: the "reference" payload is observed once and then the
# projection is asserted to reproduce it identically. This locks in the behavior the fix
# (Task 3) must preserve byte-for-byte for non-empty ``technique_name`` sources.
#
# EXPECTED OUTCOME on unfixed code: these tests PASS (they encode the baseline to protect).
# When the fix lands, the SAME tests must keep passing (the fallback branch is never taken
# for a non-empty ``technique_name`` source, so the payload is unchanged).
#
# **Validates: Requirements 3.1, 3.2, 3.3**
# =========================================================================================

import copy

from app.models.domain import Ability as OrmAbility

# The fields the preservation assertion must find projected identically (design Preservation
# Requirements). ``ability_id`` and ``executors`` are copied under their own conditions in the
# projection; the remaining attrs flow through the shared field loop.
_PRESERVED_FIELDS = (
    "ability_id",
    "name",
    "tactic",
    "technique_name",
    "technique_id",
    "description",
    "executors",
)


def _orm_ability(**attrs: Any) -> OrmAbility:
    """Build an ORM ``Ability`` row without a DB session.

    The projection only reads attributes via ``getattr``; it never touches the SQLAlchemy
    session, so a plain instance populated with keyword attributes is a faithful stand-in for
    a curated row loaded via ``CaseData``.
    """
    row = OrmAbility()
    for key, value in attrs.items():
        setattr(row, key, value)
    return row


def _assert_payload_preserved(projection, ability: Any) -> None:
    """Observe the unfixed projection output, then assert it is reproduced identically.

    Captures the reference payload (observation-first), re-runs the projection, and asserts
    byte-for-byte equality plus field-by-field equality for every preserved field. This is the
    baseline the fix must not disturb for ``isBugCondition``-false sources.
    """
    # Observe the current (unfixed) projection output and snapshot it.
    reference_payloads = projection([ability])
    assert len(reference_payloads) == 1
    reference = copy.deepcopy(dict(reference_payloads[0]))

    # Sanity: this source is isBugCondition-false (it already carries a non-empty value).
    assert "technique_name" in reference
    assert isinstance(reference["technique_name"], str)
    assert reference["technique_name"].strip() != ""

    # Re-run the projection; the output must be byte-for-byte identical to the snapshot.
    actual = dict(projection([ability])[0])
    assert actual == reference, (
        "preservation violated: projection output changed for a non-empty technique_name "
        f"source. reference={reference!r} actual={actual!r}"
    )

    # And every other projected field must match the reference exactly.
    for field in _PRESERVED_FIELDS:
        assert actual.get(field) == reference.get(field), (
            f"preservation violated: field {field!r} differs. "
            f"reference={reference.get(field)!r} actual={actual.get(field)!r}"
        )


# -- Explicit preservation test cases (from design Testing Strategy > Preservation) -------


@pytest.mark.parametrize("projection", [p for _, p in _PROJECTIONS], ids=_PROJECTION_IDS)
def test_curated_orm_ability_payload_preserved(projection) -> None:
    """Case 1: a curated ORM ``Ability`` with ``technique_name = "Web Service"`` is unchanged.

    The curated path (Req. 3.1) must produce byte-for-byte identical payloads; the fallback must
    never trigger for a row that already carries a real ``technique_name``.
    """
    ability = _orm_ability(
        ability_id="curated-1",
        name="T1102 - Web Service",
        tactic="command-and-control",
        technique_name="Web Service",
        technique_id="T1102",
        description="A curated ability loaded from the attack flow ability file.",
        executors=[{"name": "sh", "platform": "linux", "command": "curl https://example"}],
    )

    _assert_payload_preserved(projection, ability)

    # Explicitly confirm the real curated value is carried through verbatim.
    payload = dict(projection([ability])[0])
    assert payload["technique_name"] == "Web Service"


@pytest.mark.parametrize("projection", [p for _, p in _PROJECTIONS], ids=_PROJECTION_IDS)
def test_complete_duck_typed_source_payload_preserved(projection) -> None:
    """Case 2: a duck-typed source with a non-empty ``technique_name`` is unchanged.

    Any already-complete source (Req. 3.3) must be projected identically before and after the
    fix. Executors are omitted here so the ``executors`` branch is exercised in its absent form.
    """
    ability = _DuckAbility(
        ability_id="duck-1",
        name="Spearphishing Link",
        tactic="initial-access",
        technique_name="Phishing: Spearphishing Link",
        technique_id="T1566.002",
        description="Duck-typed source already carrying a non-empty technique_name.",
    )

    _assert_payload_preserved(projection, ability)


@pytest.mark.parametrize("projection", [p for _, p in _PROJECTIONS], ids=_PROJECTION_IDS)
def test_other_fields_projected_identically(projection) -> None:
    """Case 3: every other field is projected identically for a complete source.

    Confirms ``ability_id``, ``name``, ``tactic``, ``technique_id``, ``description`` are all
    carried through verbatim for an ``isBugCondition``-false ability (design Preservation).
    """
    ability = _orm_ability(
        ability_id="curated-2",
        name="T1059 - Command and Scripting Interpreter",
        tactic="execution",
        technique_name="Command and Scripting Interpreter",
        technique_id="T1059",
        description="Another curated ability with all fields populated.",
    )

    payload = dict(projection([ability])[0])

    assert payload["ability_id"] == "curated-2"
    assert payload["name"] == "T1059 - Command and Scripting Interpreter"
    assert payload["tactic"] == "execution"
    assert payload["technique_id"] == "T1059"
    assert payload["description"] == "Another curated ability with all fields populated."
    assert payload["technique_name"] == "Command and Scripting Interpreter"


# -- Property-based preservation check (Property 2) ---------------------------------------

# A non-empty, non-whitespace-only string: the mark of an isBugCondition-false source.
_non_empty_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")

# Optional text for the remaining projected fields (may be absent/None).
_optional_field_text = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=30,
    ),
)


@st.composite
def _complete_abilities(draw: st.DrawFn) -> _DuckAbility:
    """Generate an ability source where ``isBugCondition`` is FALSE.

    ``technique_name`` is always a non-empty, non-whitespace-only string, so the preservation
    branch applies. The other projected fields vary freely (present or absent) to exercise the
    projection across the already-complete input space.
    """
    attrs: dict[str, Any] = {
        "ability_id": draw(st.text(min_size=1, max_size=10)),
        "technique_name": draw(_non_empty_text),
    }
    for field in ("name", "tactic", "technique_id", "description"):
        value = draw(_optional_field_text)
        if value is not None:
            attrs[field] = value
    # Optionally attach JSON-serialisable executors (plain dicts) so that branch is covered.
    if draw(st.booleans()):
        attrs["executors"] = [
            {"name": "sh", "platform": "linux", "command": draw(st.text(max_size=20))}
        ]
    return _DuckAbility(**attrs)


@settings(max_examples=40)
@given(ability=_complete_abilities())
def test_property_operation_runner_preserves_complete_payload(
    ability: _DuckAbility,
) -> None:
    """Property 2 (OperationRunner copy): complete sources are projected identically.

    Observation-first: snapshot the current projection output, then assert the projection
    reproduces it byte-for-byte. EXPECTED to PASS on unfixed code (baseline).
    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    _assert_payload_preserved(OperationRunner._ability_payloads, ability)


@settings(max_examples=40)
@given(ability=_complete_abilities())
def test_property_provisioner_preserves_complete_payload(
    ability: _DuckAbility,
) -> None:
    """Property 2 (CurationEnvironmentProvisioner copy): same preservation guarantee.

    Observation-first: snapshot the current projection output, then assert the projection
    reproduces it byte-for-byte. EXPECTED to PASS on unfixed code (baseline).
    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    _assert_payload_preserved(CurationEnvironmentProvisioner._ability_payloads, ability)
