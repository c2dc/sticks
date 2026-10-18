"""Example tests for the stix-bundle-cache-location Settings wiring (task 2.1).

Validates: Requirements 2.4 — ``Settings`` exposes an optional ``stix_dir``
override following the existing naming/precedence convention (env prefix
``PIPELINE_UI_``, read from ``.env``). When ``PIPELINE_UI_STIX_DIR`` is set it is
reflected on ``Settings().stix_dir``; when absent it is ``None``.

Isolation: each case instantiates ``Settings`` directly (bypassing the
``lru_cache`` on ``get_settings``) and manipulates the environment via
``monkeypatch`` so no case leaks state into the next. The ``get_settings``
cache is also cleared to keep the FastAPI-dependency path consistent.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings


def test_stix_dir_reads_env_override(monkeypatch) -> None:
    """PIPELINE_UI_STIX_DIR set ⇒ Settings().stix_dir mirrors the raw value."""
    monkeypatch.setenv("PIPELINE_UI_STIX_DIR", "/x")

    assert Settings().stix_dir == "/x"


def test_stix_dir_absent_is_none(monkeypatch) -> None:
    """PIPELINE_UI_STIX_DIR absent ⇒ Settings().stix_dir is None (default)."""
    monkeypatch.delenv("PIPELINE_UI_STIX_DIR", raising=False)

    assert Settings().stix_dir is None


def test_get_settings_reflects_override(monkeypatch) -> None:
    """The cached FastAPI-dependency path also reflects the override once the
    lru_cache is cleared between environments."""
    get_settings.cache_clear()
    monkeypatch.setenv("PIPELINE_UI_STIX_DIR", "/custom/stix")

    try:
        assert get_settings().stix_dir == "/custom/stix"
    finally:
        get_settings.cache_clear()
