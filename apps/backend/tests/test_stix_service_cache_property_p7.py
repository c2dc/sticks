"""Property-based test for StixService.download_base — Property 7
(stix-bundle-cache-location).

Covers task 3.2 — Property 7 of the ``stix-bundle-cache-location`` spec: the STIX
data directory is created **on demand** (parents included) during the download,
idempotently. The property-based test is tagged with the exact
``# Feature: stix-bundle-cache-location, Property 7: <text>`` comment required by
the plan and runs with Hypothesis at ``max_examples=100`` (minimum 100
iterations).

Isolation (Req. 5.1, 5.4): every example injects a ``data_dir`` that points at a
not-yet-existing directory under ``tmp_path`` and a **stub** ``http_client`` that
returns a valid STIX bundle (a JSON object carrying an ``"objects"`` list). The
real ``sticks`` cache and the network are never touched: the single network
egress is fully mocked.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from uuid import UUID

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.stix.stix_service import KNOWN_VERSIONS, StixService

# Monotonic, process-wide counter used to guarantee a brand-new directory for
# every Hypothesis example. Relying on the drawn ``unique`` UUID alone is not
# enough on case-insensitive / Unicode-normalizing filesystems (e.g. Windows):
# ``st.uuids()`` deterministically replays the same first UUID across examples,
# and distinct ``subdir`` values can case-fold/normalize onto the same on-disk
# name, so a directory created by an earlier example can collide with a later
# one. The counter sidesteps that entirely.
_example_counter = itertools.count()

# Shared iteration budget so the property runs >= 100 examples. The
# ``function_scoped_fixture`` health check is suppressed because ``tmp_path`` is
# a per-test fixture reused across Hypothesis examples (each example writes to a
# distinct child path, so there is no cross-example leakage).
PROP_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


class _StubResponse:
    """A minimal HTTP response exposing the shape ``_download_content`` reads.

    Provides ``text`` and a no-op ``raise_for_status`` so the download path
    treats it as a successful 2xx response carrying the given body.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:  # noqa: D401 — mimics requests.Response.
        return None


class _StubHttpClient:
    """An HTTP client stub returning a fixed valid STIX bundle for every GET.

    ``get`` records each call and always returns a :class:`_StubResponse` whose
    body is a valid STIX bundle (JSON object with an ``"objects"`` list), so
    ``download_base`` passes content validation and proceeds to the atomic write.
    """

    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[tuple[tuple, dict]] = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _StubResponse(self._body)


# A valid STIX bundle body: a JSON object carrying an "objects" list.
_VALID_BUNDLE = json.dumps(
    {
        "type": "bundle",
        "id": "bundle--00000000-0000-0000-0000-000000000000",
        "objects": [
            {"type": "campaign", "id": "campaign--1", "name": "Teste"},
        ],
    }
)

# A relative, portable sub-path (no OS-specific separators) appended to
# ``tmp_path`` to form a data_dir with missing parents that does not yet exist.
# ``tmp_path`` is a single per-test fixture shared across all Hypothesis
# examples, so a per-example unique component (a UUID) is prepended to guarantee
# a fresh, not-yet-existing directory for every example (no cross-example reuse).
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


# Feature: stix-bundle-cache-location, Property 7: Criação de diretório
# idempotente sob demanda. Para qualquer data_dir resolvido inicialmente
# inexistente, executar download_base (com fetch mockado para um bundle válido)
# cria o diretório e seus pais sem lançar; uma segunda execução sobre o diretório
# já existente também conclui sem erro (idempotência de
# mkdir(parents=True, exist_ok=True)).
# Validates: Requirements 3.3
@PROP_SETTINGS
@given(subdir=_subdir, version=st.sampled_from(KNOWN_VERSIONS), unique=st.uuids())
def test_property7_on_demand_dir_creation_is_idempotent(
    tmp_path: Path, subdir: list[str], version: str, unique: UUID
) -> None:
    # A data_dir under tmp_path, with missing parents, guaranteed not to exist.
    # A monotonic per-example counter (plus the drawn UUID) prepends a component
    # that cannot collide across examples that share the single ``tmp_path``
    # fixture, even on case-insensitive / Unicode-normalizing filesystems where
    # distinct ``subdir`` values could otherwise map to the same directory.
    fresh = f"{next(_example_counter)}-{unique}"
    data_dir = tmp_path.joinpath(fresh, *subdir)
    assert not data_dir.exists(), "pré-condição: o diretório não deve existir"

    stub = _StubHttpClient(_VALID_BUNDLE)
    service = StixService(data_dir=data_dir, http_client=stub)

    # __init__ must not have created the directory (creation only on download).
    assert not data_dir.exists(), (
        "a instanciação não deve criar o diretório (criação só no download, Req. 3.3)"
    )

    # First download: must create the directory (and parents) and succeed.
    info = service.download_base(version)
    assert data_dir.exists(), (
        "download_base deve criar o diretório sob demanda (Req. 3.3)"
    )
    assert info.downloaded is True
    assert info.selectable is True
    target = service.base_path(version)
    assert target.exists(), "o bundle deve estar materializado após o download"

    # Second download over the now-existing directory: idempotent, no error.
    info2 = service.download_base(version)
    assert info2.downloaded is True
    assert info2.selectable is True
    assert target.exists(), (
        "a segunda execução também conclui sem erro e mantém o bundle (Req. 3.3)"
    )
