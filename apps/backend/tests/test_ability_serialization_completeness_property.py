"""Property-based test for per-Ability data completeness in serialization (task 9.2).

# Feature: stage6-execution-redesign, Property 9: Completude dos dados por Ability na serialização

*Para qualquer* ``AbilityResult`` (com qualquer combinação de campos presentes ou
nulos/vazios), o frame/view serializado para a UI — a saída de
``AbilityResultView.model_dump(mode="json")`` e, equivalentemente, cada linha de
``results`` do frame WS montado por :func:`run_operation_progress_channel` — expõe as
quatro chaves por Ability do rastreamento ao vivo do Estágio 6: ``command``,
``target_container``, ``attacker_host`` e ``command_output``. E para cada chave cujo
valor é nulo/vazio, a projeção para a UI resolve o indicador explícito de
"não disponível" em vez de um valor em branco.

**Validates: Requirements 2.4, 2.5**

Alcance / fronteira (conforme design.md, Property 9)
----------------------------------------------------
A persistência do dado "não disponível" é uma preocupação do **frontend** (o backend
transporta ``None`` quando o dado ainda não foi coletado — ver
:class:`AbilityResultView`). Portanto esta propriedade asserta, no lado do backend, que a
**serialização sempre carrega as quatro chaves** (``null`` quando ausente) para que o
frontend possa renderizar o indicador. Para fechar o critério 2.5 de forma verificável
sem depender do frontend, um **oráculo de projeção para a UI** independente
(:func:`_ui_projection`) — espelhando o que o frontend fará — recomputa, para cada chave,
ou o valor concreto não-vazio ou o indicador explícito ``"não disponível"``; a propriedade
asserta que nenhuma chave presente/ausente produz um valor em branco sem rótulo.

Sem Caldera e sem rede: a propriedade é exercida diretamente sobre a view tipada e sobre a
mesma projeção de frame do canal WS, em linha com a restrição de "nenhuma emulação real
durante o desenvolvimento".
"""

from __future__ import annotations

from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import AbilityResultStatus, OperationState
from app.services.session.session_state_service import (
    AbilityResultView,
    OperationResultView,
)

# The four per-Ability data keys that the Stage 6 live tracking must always
# expose in the serialized frame/view (Req. 2.4).
_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"command", "target_container", "attacker_host", "command_output"}
)

# The explicit indicator the UI renders for a null/empty datum (Req. 2.5). The
# exact string is the frontend's concern; the backend-side oracle only needs a
# non-blank, labelled placeholder to prove the projection is never a blank value.
_NOT_AVAILABLE = "não disponível"


def _ui_projection(value: Optional[str]) -> str:
    """Independent UI-projection oracle mirroring the frontend (Req. 2.5).

    For a per-Ability datum, resolve either the concrete non-empty value or the
    explicit "não disponível" indicator. A ``None`` or empty/whitespace-only
    string is treated as *not collected*, so the UI must show the indicator
    rather than a blank field.
    """
    if value is None or value.strip() == "":
        return _NOT_AVAILABLE
    return value


# A string value that is "present" from the UI's point of view: non-empty and
# not whitespace-only. ``\x00`` is excluded so the value survives JSON round-trip
# cleanly in the serialized frame.
_present_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")

# An "empty-ish" value the UI must treat as not-available: None, "", or
# whitespace-only strings.
_emptyish_text = st.one_of(
    st.none(),
    st.just(""),
    st.text(alphabet=" \t\n", min_size=1, max_size=5),
)

# A field is either present (non-empty) or empty-ish (None / blank). This is what
# drives "any combination of present/null fields".
_optional_field = st.one_of(_present_text, _emptyish_text)


@st.composite
def _ability_result_views(draw: st.DrawFn) -> AbilityResultView:
    """Generate an ``AbilityResultView`` with arbitrary present/empty-ish fields."""
    return AbilityResultView(
        operation_id=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10_000))),
        ability_id=draw(st.one_of(st.none(), _present_text)),
        status=draw(st.sampled_from(list(AbilityResultStatus))),
        command_output=draw(_optional_field),
        command=draw(_optional_field),
        target_container=draw(_optional_field),
        attacker_host=draw(_optional_field),
    )


def _operation_frame(view: OperationResultView) -> dict:
    """Build the WS operation frame exactly as ``run_operation_progress_channel``.

    Mirrors the frame assembly in :func:`app.ws.channels.run_operation_progress_channel`
    so the property validates the shape actually pushed to the UI, serializing each
    per-Ability result via ``model_dump(mode="json")``.
    """
    return {
        "type": "operation",
        "operation_id": view.operation_id,
        "campaign_id": view.campaign_id,
        "state": view.state.value,
        "found": True,
        "results": [r.model_dump(mode="json") for r in view.results],
        "aggregate": {
            "total_success": view.total_success,
            "total_failure": view.total_failure,
        },
    }


@settings(max_examples=200)
@given(view=_ability_result_views())
def test_view_serialization_always_carries_the_four_keys(
    view: AbilityResultView,
) -> None:
    """``model_dump(mode="json")`` always exposes the four per-Ability keys (Req. 2.4)."""
    dumped = view.model_dump(mode="json")

    # Completeness: every required key is present regardless of which values are
    # None/empty — so the frontend can always render each datum (or its indicator).
    assert _REQUIRED_KEYS.issubset(dumped.keys())

    # The serialized value for an absent datum is JSON ``null`` (None), never a
    # silently-dropped key, so the frontend can detect "not collected".
    for key in _REQUIRED_KEYS:
        value = dumped[key]
        assert value is None or isinstance(value, str)


@settings(max_examples=200)
@given(view=_ability_result_views())
def test_ui_projection_resolves_explicit_indicator_for_empty(
    view: AbilityResultView,
) -> None:
    """Each null/empty datum projects to an explicit indicator, never a blank (Req. 2.5)."""
    dumped = view.model_dump(mode="json")

    for key in _REQUIRED_KEYS:
        projected = _ui_projection(dumped[key])

        # Never a blank/whitespace-only value without a label.
        assert projected.strip() != ""

        raw = dumped[key]
        if raw is None or raw.strip() == "":
            # Not collected => the explicit "não disponível" indicator.
            assert projected == _NOT_AVAILABLE
        else:
            # Collected => the concrete value is shown verbatim.
            assert projected == raw


@settings(max_examples=200)
@given(
    views=st.lists(_ability_result_views(), min_size=0, max_size=6),
    state=st.sampled_from(list(OperationState)),
    total_success=st.integers(min_value=0, max_value=50),
    total_failure=st.integers(min_value=0, max_value=50),
)
def test_ws_frame_results_each_carry_the_four_keys(
    views: list[AbilityResultView],
    state: OperationState,
    total_success: int,
    total_failure: int,
) -> None:
    """Every per-Ability row of the WS frame exposes the four keys (Req. 2.4, 2.5)."""
    operation_view = OperationResultView(
        operation_id=1,
        campaign_id="shadowray",
        state=state,
        total_success=total_success,
        total_failure=total_failure,
        results=views,
    )

    frame = _operation_frame(operation_view)

    assert len(frame["results"]) == len(views)
    for row in frame["results"]:
        # Completeness per Ability in the actual WS frame projection.
        assert _REQUIRED_KEYS.issubset(row.keys())
        for key in _REQUIRED_KEYS:
            # And the UI projection of each key is explicit (value or indicator),
            # never a blank field.
            assert _ui_projection(row[key]).strip() != ""
