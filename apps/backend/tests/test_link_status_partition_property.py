"""Property-based test for per-link status partitioning (task 3.3).

# Feature: stage6-execution-redesign, Property 8: Estado por Ability é exatamente um dos quatro

*Para qualquer* ``status_code`` cru de um link do Caldera (incluindo ``None``, o
sentinela de execução ``-3``, o código de sucesso ``0`` e quaisquer outros inteiros),
:func:`_derive_link_status` produz **exatamente um** dentre os quatro estados mutuamente
exclusivos de :class:`AbilityResultStatus` (``PENDING``, ``RUNNING``, ``SUCCESS``,
``FAILURE``). O mapeamento é **total** (todo ``status_code`` recebe um estado) e
**disjunto** (nunca produz dois estados simultâneos), espelhando o Requisito 2.1: cada
Ability exibe exatamente um estado visível.

**Validates: Requirements 2.1**

A propriedade é exercida diretamente sobre :func:`_derive_link_status` — a derivação pura
que o ``OperationRunner`` usa ao reconciliar os links de uma Operação — sem Caldera real
e sem rede, em linha com a restrição de "nenhuma emulação real durante o desenvolvimento".

Um **oráculo independente** (:func:`_expected_status`) recomputa o estado esperado a
partir das sentinelas documentadas (``None``→pendente, ``-3``→em_execucao, ``0``→sucesso,
outro→falha), de modo que as asserções nunca confiam na implementação para decidir o que
cada estado "significa".
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import AbilityResultStatus
from app.services.caldera.client import _LINK_STATUS_RUNNING, _derive_link_status

# The complete, closed set of the four mutually exclusive per-Ability states.
_ALL_STATUSES: frozenset[AbilityResultStatus] = frozenset(AbilityResultStatus)


def _expected_status(status_code: int | None) -> AbilityResultStatus:
    """Independent oracle: recompute the expected status from the sentinels.

    * ``None`` (link not yet run / not collected) => ``PENDING``
    * ``-3`` (in-flight running sentinel)         => ``RUNNING``
    * ``0``  (command finished successfully)      => ``SUCCESS``
    * any other finished (non-zero) status        => ``FAILURE``
    """
    if status_code is None:
        return AbilityResultStatus.PENDING
    if status_code == _LINK_STATUS_RUNNING:
        return AbilityResultStatus.RUNNING
    if status_code == 0:
        return AbilityResultStatus.SUCCESS
    return AbilityResultStatus.FAILURE


# Generate arbitrary status codes: None, the two known sentinels (-3, 0), and a
# wide band of other integers so the "any other => FAILURE" branch is exercised
# with both the documented sentinels and arbitrary noise around them.
_status_codes = st.one_of(
    st.none(),
    st.just(_LINK_STATUS_RUNNING),
    st.just(0),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
)


@settings(max_examples=300)
@given(status_code=_status_codes)
def test_derive_link_status_is_exactly_one_of_four(status_code: int | None) -> None:
    """The derived status is a member of the four-state set (total mapping)."""
    derived = _derive_link_status(status_code)

    # Membership: every input maps to one of exactly the four known states.
    assert derived in _ALL_STATUSES
    assert isinstance(derived, AbilityResultStatus)


@settings(max_examples=300)
@given(status_code=_status_codes)
def test_derive_link_status_matches_independent_oracle(
    status_code: int | None,
) -> None:
    """The derivation agrees with the independent sentinel oracle (disjoint).

    Agreeing with a single-valued oracle for every input proves the mapping is
    both total (defined everywhere) and disjoint (never two states at once):
    exactly one of the four states is produced.
    """
    derived = _derive_link_status(status_code)
    expected = _expected_status(status_code)

    assert derived is expected

    # Disjointness, made explicit: the derived value equals one state and differs
    # from each of the other three.
    others = _ALL_STATUSES - {derived}
    assert all(derived is not other for other in others)


@settings(max_examples=300)
@given(
    status_code=st.integers(min_value=-1_000_000, max_value=1_000_000).filter(
        lambda n: n not in (0, _LINK_STATUS_RUNNING)
    )
)
def test_non_sentinel_integers_map_to_failure(status_code: int) -> None:
    """Any finished status that is neither 0 nor -3 is a FAILURE."""
    assert _derive_link_status(status_code) is AbilityResultStatus.FAILURE
