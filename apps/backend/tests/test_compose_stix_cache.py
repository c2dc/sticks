"""Snapshot/parse test for the STIX cache mapping in the compose file (task 5.1).

Verifies the infra wiring that lets the containerized `backend` service write
the STIX bundle to a persistent named volume outside the repo tree:

* the `backend` service sets ``PIPELINE_UI_STIX_DIR=/var/cache/sticks/stix``
  (the override that wins over the OS default — Req. 2.1), and
* the `backend` service mounts the named volume ``sticks-stix-cache`` at
  ``/var/cache/sticks/stix`` (Req. 4.4).

This is infra configuration (not a function with input/output), so it is a
parse/snapshot assertion rather than a property test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/ -> backend -> apps -> <repo>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPO_ROOT / "infra" / "docker" / "docker-compose.yml"

_EXPECTED_STIX_DIR = "/var/cache/sticks/stix"
_EXPECTED_VOLUME = "sticks-stix-cache"
_EXPECTED_MOUNT = "/var/cache/sticks/stix"


@pytest.fixture(scope="module")
def compose() -> dict:
    """Parse the compose file once for the whole module."""
    assert _COMPOSE_PATH.is_file(), f"compose file not found at {_COMPOSE_PATH}"
    with _COMPOSE_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def backend_service(compose: dict) -> dict:
    services = compose.get("services", {})
    assert "backend" in services, "`backend` service missing from compose"
    return services["backend"]


def _normalize_environment(env) -> dict[str, str]:
    """Return the service environment as a dict regardless of list/dict form."""
    if env is None:
        return {}
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    # list form: ["KEY=value", "OTHER=value"]
    result: dict[str, str] = {}
    for item in env:
        key, sep, value = str(item).partition("=")
        result[key] = value if sep else ""
    return result


def test_backend_sets_pipeline_ui_stix_dir(backend_service: dict) -> None:
    """`backend` defines PIPELINE_UI_STIX_DIR pointing at the cache mount (Req. 4.4)."""
    env = _normalize_environment(backend_service.get("environment"))
    assert env.get("PIPELINE_UI_STIX_DIR") == _EXPECTED_STIX_DIR


def test_backend_mounts_named_cache_volume(backend_service: dict) -> None:
    """`backend` mounts the named volume `sticks-stix-cache` at the cache dir (Req. 4.4)."""
    volumes = backend_service.get("volumes", [])
    mounts = {str(v) for v in volumes}
    expected_short = f"{_EXPECTED_VOLUME}:{_EXPECTED_MOUNT}"
    assert any(
        m.startswith(f"{_EXPECTED_VOLUME}:{_EXPECTED_MOUNT}") for m in mounts
    ), f"expected mount {expected_short!r} in {sorted(mounts)}"


def test_named_cache_volume_declared_top_level(compose: dict) -> None:
    """The `sticks-stix-cache` named volume is declared at the top level (Req. 4.4)."""
    volumes = compose.get("volumes", {})
    assert _EXPECTED_VOLUME in volumes, "`sticks-stix-cache` volume not declared"


def test_backend_no_longer_binds_repo_stix_dir(backend_service: dict) -> None:
    """The old writable bind under the repo tree is gone (decoupled from repo)."""
    volumes = {str(v) for v in backend_service.get("volumes", [])}
    assert not any(
        "packages/cli/data/stix" in m for m in volumes
    ), "backend still binds the in-repo STIX dir"
