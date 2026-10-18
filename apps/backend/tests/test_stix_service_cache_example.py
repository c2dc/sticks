"""Example tests for StixService cache behaviour (stix-bundle-cache-location).

Covers task 3.5 — concrete example/edge-case tests complementing the
property-based suite for Stage-1 behaviour over the relocated cache:

* **Disk reuse (Req. 3.2)** — a bundle pre-created in the resolved ``data_dir``
  is reported as ``downloaded=true`` by ``list_bases`` and read by
  ``read_bundle`` **without** touching the injected ``http_client`` (no network
  egress on a cache hit).
* **Clean initial state (Req. 3.1, 3.5)** — an empty ``data_dir`` yields every
  known version flagged ``downloaded=false`` / ``selectable=false`` (nothing is
  downloaded by default).
* **Override wins over the OS default** — a concrete example confirming that the
  ``PIPELINE_UI_STIX_DIR`` override (and an injected ``data_dir``) take
  precedence over the default resolved for the current OS.

Isolation (Req. 5.1): every case injects a ``data_dir`` under ``tmp_path`` and a
**spy** ``http_client`` that records any method invocation, so the real user
cache and the network are never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.stix.stix_paths import resolve_stix_data_dir
from app.services.stix.stix_service import KNOWN_VERSIONS, StixService

# A KNOWN version from KNOWN_VERSIONS used for the concrete disk-reuse example.
_VERSION = "18.0"
assert _VERSION in KNOWN_VERSIONS, "a versão de exemplo deve ser conhecida"

# A valid STIX bundle body: a JSON object carrying an "objects" list.
_VALID_BUNDLE = {
    "type": "bundle",
    "id": "bundle--00000000-0000-0000-0000-000000000000",
    "objects": [
        {"type": "campaign", "id": "campaign--1", "name": "Teste"},
        {"type": "attack-pattern", "id": "attack-pattern--1", "name": "Tec"},
    ],
}


class _SpyHttpClient:
    """An HTTP client spy that records every attribute access as a call.

    ``__getattr__`` returns a callable for any attribute (``get``, ``post`` …)
    that appends the invocation to :attr:`calls`. A cache hit (listing/reading an
    already-present bundle) must never touch this client, so :attr:`calls` stays
    empty.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return _record


def _write_bundle(data_dir: Path, version: str) -> Path:
    """Pre-create ``enterprise-attack-{version}.json`` with a valid STIX body."""
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / f"enterprise-attack-{version}.json"
    target.write_text(json.dumps(_VALID_BUNDLE), encoding="utf-8")
    return target


# --- Disk reuse: a pre-created bundle is served from disk, no network (Req. 3.2)


def test_precreated_bundle_is_reported_downloaded(tmp_path: Path) -> None:
    """A bundle present on disk ⇒ list_bases flags it downloaded/selectable."""
    data_dir = tmp_path / "stix"
    target = _write_bundle(data_dir, _VERSION)

    spy = _SpyHttpClient()
    service = StixService(data_dir=data_dir, http_client=spy)

    bases = {b.version: b for b in service.list_bases()}
    info = bases[_VERSION]

    assert info.downloaded is True
    assert info.selectable is True
    assert info.file == str(target)
    # Listing a cache hit performs no network egress.
    assert spy.calls == [], "list_bases não deve chamar o http_client (Req. 3.2)"


def test_read_bundle_uses_disk_without_http_client(tmp_path: Path) -> None:
    """read_bundle reads the pre-created copy from disk, never calling http."""
    data_dir = tmp_path / "stix"
    _write_bundle(data_dir, _VERSION)

    spy = _SpyHttpClient()
    service = StixService(data_dir=data_dir, http_client=spy)

    bundle = service.read_bundle(_VERSION)

    assert bundle == _VALID_BUNDLE
    assert isinstance(bundle["objects"], list)
    # Reusing the on-disk copy must not trigger any network egress (Req. 3.2).
    assert spy.calls == [], "read_bundle não deve chamar o http_client (Req. 3.2)"


# --- Clean initial state: empty data_dir ⇒ nothing downloaded (Req. 3.1, 3.5)


def test_empty_data_dir_reports_all_versions_not_downloaded(tmp_path: Path) -> None:
    """An empty data_dir ⇒ every known version is downloaded=false (Req. 3.1, 3.5)."""
    data_dir = tmp_path / "empty-stix"
    data_dir.mkdir()

    spy = _SpyHttpClient()
    service = StixService(data_dir=data_dir, http_client=spy)

    bases = service.list_bases()

    # Every known version is present in the listing and none is downloaded.
    assert {b.version for b in bases} == set(KNOWN_VERSIONS)
    assert all(b.downloaded is False for b in bases), (
        "nenhuma versão deve estar baixada num data_dir vazio (Req. 3.1, 3.5)"
    )
    assert all(b.selectable is False for b in bases)
    assert all(b.file is None for b in bases)
    assert spy.calls == [], "a listagem não deve acessar a rede (Req. 3.1)"


# --- Override precedence: PIPELINE_UI_STIX_DIR / injected win over OS default


def test_override_wins_over_current_os_default(tmp_path: Path) -> None:
    """PIPELINE_UI_STIX_DIR override takes precedence over the OS default.

    Resolving with a non-empty override on the current OS (real ``env``/platform)
    must return exactly the override path, never the OS-default cache dir.
    """
    override_dir = tmp_path / "override-cache" / "stix"

    # Default (no override) resolves to the user's OS cache dir on this machine.
    default_dir = resolve_stix_data_dir()
    # With a non-empty override it must win regardless of the current OS default.
    resolved = resolve_stix_data_dir(override=str(override_dir))

    assert resolved == override_dir
    assert resolved != default_dir, (
        "o override deve vencer o default do SO corrente (Req. 2.1)"
    )


def test_injected_data_dir_wins_over_os_default(tmp_path: Path) -> None:
    """A data_dir injected into StixService wins over the OS default (Req. 2.2)."""
    injected = tmp_path / "injected" / "stix"

    service = StixService(data_dir=injected, http_client=_SpyHttpClient())

    assert service.data_dir == injected
    assert service.data_dir != resolve_stix_data_dir(), (
        "o data_dir injetado deve vencer o default do SO corrente (Req. 2.2)"
    )
