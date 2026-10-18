"""Property-based tests for StixService instantiation (stix-bundle-cache-location).

Covers task 3.1 — Property 6 of the ``stix-bundle-cache-location`` spec. The
property-based test is tagged with the exact
``# Feature: stix-bundle-cache-location, Property 6: <text>`` comment required by
the plan and runs with Hypothesis at ``max_examples=100`` (minimum 100
iterations).

Isolation (Req. 5.1, 5.4): every example injects a ``data_dir`` that points at a
not-yet-existing directory under ``tmp_path`` and a **spy** ``http_client`` that
records any method invocation. The real ``sticks`` cache and the network are
never touched: construction must be a pure ``Path`` computation.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.stix.stix_service import StixService

# Shared iteration budget so the property runs >= 100 examples. The
# ``function_scoped_fixture`` health check is suppressed because ``tmp_path`` is
# a per-test fixture reused across Hypothesis examples (each example writes to a
# distinct child path, so there is no cross-example leakage).
PROP_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


class _SpyHttpClient:
    """An HTTP client spy that records every attribute access as a call.

    ``__getattr__`` returns a callable for any attribute (``get``, ``post`` …)
    that appends the invocation to :attr:`calls`. Instantiating ``StixService``
    must never touch this client, so after ``__init__`` :attr:`calls` stays
    empty.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return _record


# Relative, portable sub-paths (no OS-specific separators) appended to
# ``tmp_path`` to form a directory that does not yet exist.
_subdir = st.lists(
    st.text(
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd"),
            whitelist_characters="_-",
        ),
        min_size=1,
        max_size=12,
    ),
    min_size=1,
    max_size=4,
)


# Feature: stix-bundle-cache-location, Property 6: Instanciação sem efeitos
# colaterais. Para qualquer data_dir apontando para um diretório inexistente,
# instanciar StixService com um http_client espião NÃO cria o diretório nem
# realiza qualquer chamada de rede — após o __init__ o diretório ainda não
# existe e nenhum método do cliente HTTP foi invocado.
# Validates: Requirements 5.4
@PROP_SETTINGS
@given(subdir=_subdir)
def test_property6_instantiation_has_no_side_effects(
    tmp_path: Path, subdir: list[str]
) -> None:
    # A directory under tmp_path that is guaranteed not to exist yet.
    data_dir = tmp_path.joinpath(*subdir)
    assert not data_dir.exists(), "pré-condição: o diretório não deve existir"

    spy = _SpyHttpClient()

    service = StixService(data_dir=data_dir, http_client=spy)

    # The resolved directory must be exactly the injected path (injected has the
    # highest precedence) and __init__ must NOT have created it on disk.
    assert service.data_dir == data_dir
    assert not data_dir.exists(), (
        "a instanciação não deve criar o diretório de dados STIX (Req. 5.4)"
    )

    # No network egress: the spy client must not have been invoked at all.
    assert spy.calls == [], (
        "a instanciação não deve chamar nenhum método do http_client (Req. 5.4)"
    )
