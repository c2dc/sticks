"""Property-based test for StixService download failures (stix-bundle-cache-location).

Covers task 3.3 — Property 8 of the ``stix-bundle-cache-location`` spec. The
property-based test carries the exact
``# Feature: stix-bundle-cache-location, Property 8: <text>`` comment required by
the plan and runs with Hypothesis at ``max_examples=100`` (minimum 100
iterations).

Isolation (Req. 5.1, 5.4): every example injects a ``data_dir`` under
``tmp_path`` and a stub ``http_client`` — the real ``sticks`` cache and the
network are never touched. The property injects a failure at one of the three
points of ``download_base`` (fetch, non-STIX content, atomic replace) and
asserts that after ``StixDownloadError`` the target bundle is absent and no
temporary file is left behind in ``data_dir``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import app.services.stix.stix_service as stix_service_module
from app.services.stix.stix_service import KNOWN_VERSIONS, StixDownloadError, StixService

# Shared iteration budget so the property runs >= 100 examples. The
# ``function_scoped_fixture`` health check is suppressed because ``tmp_path`` and
# ``monkeypatch`` are per-test fixtures reused across Hypothesis examples (each
# example writes to a distinct child path, so there is no cross-example leakage).
PROP_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# A minimal but structurally valid STIX bundle (JSON object with an "objects"
# list) — used so the content validation passes and the failure can be exercised
# at the atomic-replace step.
_VALID_BUNDLE = json.dumps({"type": "bundle", "objects": []})

# The three failure points of download_base, per the design (Property 8):
# fetch error, non-STIX content, and write/rename (replace) failure.
_FAILURE_POINTS = ("fetch", "non_stix", "replace")


class _StubHttpClient:
    """A stub HTTP client whose GET returns a canned body (or raises).

    When ``raise_on_get`` is set, ``get`` raises it to simulate a transport
    failure (the "fetch" failure point). Otherwise it returns a tiny response
    object exposing ``text`` and a no-op ``raise_for_status`` so the download
    reaches the content-validation and atomic-write steps.
    """

    def __init__(self, body: str, raise_on_get: Exception | None = None) -> None:
        self._body = body
        self._raise_on_get = raise_on_get

    def get(self, url: str, timeout: float | None = None):  # noqa: ARG002
        if self._raise_on_get is not None:
            raise self._raise_on_get

        class _Resp:
            def __init__(self, text: str) -> None:
                self.text = text
                self.status_code = 200

            def raise_for_status(self) -> None:
                return None

        return _Resp(self._body)


def _leftover_files(data_dir: Path) -> list[str]:
    """Names of any files currently present in ``data_dir`` (empty if absent)."""
    if not data_dir.exists():
        return []
    return [p.name for p in data_dir.iterdir() if p.is_file()]


# Feature: stix-bundle-cache-location, Property 8: Nenhum arquivo parcial em caso
# de falha. Para qualquer falha injetada no caminho de download (erro de fetch,
# conteúdo não-STIX, ou falha de escrita/rename), após download_base lançar
# StixDownloadError o base_path(version) NÃO existe e nenhum arquivo temporário
# permanece no data_dir — a escrita atômica nunca deixa um bundle meio escrito.
# Validates: Requirements 3.3
@PROP_SETTINGS
@given(
    version=st.sampled_from(KNOWN_VERSIONS),
    failure_point=st.sampled_from(_FAILURE_POINTS),
)
def test_property8_no_partial_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    failure_point: str,
) -> None:
    data_dir = tmp_path / "stix-cache"

    if failure_point == "fetch":
        # Fetch fails: the GET raises a transport error.
        client = _StubHttpClient(_VALID_BUNDLE, raise_on_get=RuntimeError("boom"))
    elif failure_point == "non_stix":
        # Content is valid JSON but NOT a STIX bundle (no "objects" list).
        client = _StubHttpClient(json.dumps({"not": "a bundle"}))
    else:  # "replace"
        # Content is a valid STIX bundle, but the atomic rename fails.
        client = _StubHttpClient(_VALID_BUNDLE)
        monkeypatch.setattr(
            stix_service_module.os,
            "replace",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
        )

    service = StixService(data_dir=data_dir, http_client=client)
    target = service.base_path(version)

    # Every injected failure must surface as the typed download error.
    with pytest.raises(StixDownloadError):
        service.download_base(version)

    # No partial bundle: the target file must be absent.
    assert not target.exists(), (
        f"base_path({version}) não deve existir após falha '{failure_point}'"
    )

    # No leftover temporary file remains in data_dir.
    leftovers = _leftover_files(data_dir)
    assert leftovers == [], (
        f"nenhum arquivo deve permanecer em data_dir após falha '{failure_point}', "
        f"mas encontrou: {leftovers}"
    )
