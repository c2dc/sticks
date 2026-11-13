"""Pytest configuration for the backend test suite.

Its mere presence makes pytest insert this ``tests`` directory onto
``sys.path`` at the start of collection, so sibling test-support modules (such
as ``_stix_seed``) are importable from the test modules regardless of the
invocation's working directory or import mode.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# No test may trigger network access or background ingestion through the app
# ``lifespan``; tests that need the orchestrator build it explicitly.
os.environ["STICKS_INGEST_ON_START"] = "0"

import pytest

# Variables inherited from the developer's shell that must never leak into a
# test. ``PIPELINE_UI_LAB*`` are the explicit opt-in switches of the real-lab
# harness and are preserved (that harness is deselected by default anyway).
_ISOLATED_PREFIXES = ("PIPELINE_UI_", "OLLAMA_")
_PRESERVED_PREFIXES = ("PIPELINE_UI_LAB",)


@pytest.fixture(autouse=True)
def _offline_stix_version_catalog():
    """No test queries the upstream version index: the catalog starts offline
    (served from ``KNOWN_VERSIONS``). Tests may inject their own fetcher."""
    from app.services.stix.stix_service import StixVersionCatalog, set_version_catalog

    def _offline(timeout: float):
        raise OSError("network disabled in tests")

    previous = set_version_catalog(StixVersionCatalog(_offline))
    yield
    set_version_catalog(previous)


@pytest.fixture(autouse=True)
def _isolate_settings_from_developer_environment(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
):
    """Hide inherited ``PIPELINE_UI_*`` / ``OLLAMA_*`` variables and ``.env``.

    Each test starts from the code defaults: inherited variables are removed,
    the ``Settings`` ``.env`` file is not read, and the ``get_settings`` cache is
    cleared before and after. Values a test sets itself (``monkeypatch.setenv``
    or ``env=`` of a subprocess) are applied afterwards and are preserved.
    Real-lab tests (marker ``lab``) read the environment on purpose and keep it.
    """
    from app.core.config import Settings, get_settings

    if request.node.get_closest_marker("lab") is None:
        for name in list(os.environ):
            if name.startswith(_ISOLATED_PREFIXES) and not name.startswith(_PRESERVED_PREFIXES):
                monkeypatch.delenv(name, raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", None)
        # Small fake catalogs must count as complete (production floor is 150).
        monkeypatch.setenv("DOCKER_CATALOG_MIN_IMAGES", "0")
        # Small fake Atomic fixtures must pass the plausibility floor (default
        # 100), and the developer's INGEST_WORK_DIR (env or root .env) must not
        # leak into tests: an empty value means "memory only".
        monkeypatch.setenv("ATOMIC_MIN_TECHNIQUES", "0")
        monkeypatch.setenv("INGEST_WORK_DIR", "")

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
