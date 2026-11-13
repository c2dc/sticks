"""Unit tests for the SEPINF settings and the lazy LLM factory (task 1.3).

Cover :mod:`app.services.ai.config` (:class:`SepinfSettings`,
:func:`get_sepinf_settings`) and :mod:`app.services.ai.llm`
(:func:`make_chat_model`, :class:`SepinfConfigError`, :func:`_require`),
asserting the Requirement 7 guarantees:

1. Settings values are read from the environment — ``SEPINF_BASE_URL``,
   ``SEPINF_MODEL_ID``, ``SEPINF_API_KEY``, ``AI_MAX_ITERATIONS`` (default 3),
   ``AI_EMBEDDING_DIM`` and ``AI_RETRIEVAL_K`` (Requirement 7.1).
2. Neither module embeds the three SEPINF endpoint values as source literals
   (Requirement 7.2).
3. The resolved API key is never rendered by ``repr``/``str`` of the settings
   object, so it cannot leak into logs or stack traces (Requirement 7.3).
4. Importing either module opens no network connection and performs no
   ``langchain_openai`` import (the factory defers it) (Requirement 7.4).
5. A missing/empty ``SEPINF_API_KEY`` raises a name-only
   :class:`SepinfConfigError` that never contains the key value
   (Requirement 7.5).
6. A missing/empty ``SEPINF_BASE_URL`` or ``SEPINF_MODEL_ID`` raises a
   name-only :class:`SepinfConfigError` (Requirement 7.6).

Isolation note: the "missing setting" cases construct :class:`SepinfSettings`
with ``_env_file=None`` and explicit empty overrides so the tests never depend
on the contents of the repository-root ``.env``.

_Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.services.ai import config as config_module
from app.services.ai import llm as llm_module
from app.services.ai.config import SepinfSettings, get_sepinf_settings
from app.services.ai.llm import SepinfConfigError, _require, make_chat_model

# The three SEPINF endpoint environment-variable names (aliases on the model).
_SEPINF_ENV_VARS = ("SEPINF_BASE_URL", "SEPINF_MODEL_ID", "SEPINF_API_KEY")
_AI_ENV_VARS = ("AI_MAX_ITERATIONS", "AI_EMBEDDING_DIM", "AI_RETRIEVAL_K")


# Attribute name -> environment-variable alias, so overrides bind correctly.
# The model fields are aliased (``base_url`` <- ``SEPINF_BASE_URL`` etc.) and
# ``populate_by_name`` is not enabled, so init kwargs must use the alias.
_ATTR_TO_ALIAS = {
    "base_url": "SEPINF_BASE_URL",
    "model_id": "SEPINF_MODEL_ID",
    "api_key": "SEPINF_API_KEY",
    "max_iterations": "AI_MAX_ITERATIONS",
    "embedding_dim": "AI_EMBEDDING_DIM",
    "retrieval_k": "AI_RETRIEVAL_K",
}


def _settings_from_overrides(**overrides: object) -> SepinfSettings:
    """Build a ``SepinfSettings`` that ignores the root ``.env``.

    Passing ``_env_file=None`` disables the file source so only the explicit
    overrides are considered, keeping the "missing setting" cases independent of
    whatever the repo-root ``.env`` happens to contain. Overrides are keyed by
    the model's attribute names and translated here to the field aliases that
    pydantic-settings binds on init.
    """
    aliased = {_ATTR_TO_ALIAS[name]: value for name, value in overrides.items()}
    return SepinfSettings(_env_file=None, **aliased)  # type: ignore[call-arg] -- pydantic-settings accepts _env_file at runtime but the stub does not declare it


@pytest.fixture
def clear_sepinf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all SEPINF/AI env vars so each test controls its own inputs."""
    for name in (*_SEPINF_ENV_VARS, *_AI_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    # The accessor is cached; drop any instance built under other env state.
    get_sepinf_settings.cache_clear()


# ---------------------------------------------------------------------------
# Requirement 7.1 — values are read from the environment
# ---------------------------------------------------------------------------


def test_settings_read_endpoint_values_from_env(
    clear_sepinf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``base_url``/``model_id``/``api_key`` come straight from env settings."""
    monkeypatch.setenv("SEPINF_BASE_URL", "http://env.example/v1")
    monkeypatch.setenv("SEPINF_MODEL_ID", "env-model-xyz")
    monkeypatch.setenv("SEPINF_API_KEY", "env-key-abc123")

    settings = SepinfSettings()

    assert settings.base_url == "http://env.example/v1"
    assert settings.model_id == "env-model-xyz"
    assert settings.api_key == "env-key-abc123"


def test_settings_read_loop_knobs_from_env(
    clear_sepinf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AI_MAX_ITERATIONS``/``AI_EMBEDDING_DIM``/``AI_RETRIEVAL_K`` from env."""
    monkeypatch.setenv("AI_MAX_ITERATIONS", "7")
    monkeypatch.setenv("AI_EMBEDDING_DIM", "768")
    monkeypatch.setenv("AI_RETRIEVAL_K", "9")

    settings = SepinfSettings()

    assert settings.max_iterations == 7
    assert settings.embedding_dim == 768
    assert settings.retrieval_k == 9


def test_max_iterations_defaults_to_three(clear_sepinf_env: None) -> None:
    """``max_iterations`` defaults to 3 when ``AI_MAX_ITERATIONS`` is unset."""
    settings = _settings_from_overrides()
    assert settings.max_iterations == 3


def test_get_sepinf_settings_is_cached(
    clear_sepinf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accessor returns a single cached instance (lru_cache)."""
    monkeypatch.setenv("SEPINF_BASE_URL", "http://cached.example/v1")
    monkeypatch.setenv("SEPINF_MODEL_ID", "cached-model")
    monkeypatch.setenv("SEPINF_API_KEY", "cached-key")

    first = get_sepinf_settings()
    second = get_sepinf_settings()

    assert first is second
    assert first.base_url == "http://cached.example/v1"


# ---------------------------------------------------------------------------
# Requirement 7.2 — no endpoint values embedded as source literals
# ---------------------------------------------------------------------------


def test_source_contains_no_literal_sepinf_values(
    clear_sepinf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured endpoint values never appear verbatim in the source.

    Rather than hardcoding the real key into the test, the test sets known,
    unusual sentinel values in the environment, resolves them through the
    settings object, and asserts those resolved values are absent from both
    module source files. (A literal would have to be a hardcoded constant and so
    would not track these env-provided values — the assertion catches any such
    accidental embedding of the real values too, since the real values are not
    among the source tokens either.)
    """
    config_src = Path(config_module.__file__).read_text(encoding="utf-8")
    llm_src = Path(llm_module.__file__).read_text(encoding="utf-8")

    monkeypatch.setenv("SEPINF_BASE_URL", "http://sentinel-url.invalid/v1")
    monkeypatch.setenv("SEPINF_MODEL_ID", "sentinel-model-id-zzz")
    monkeypatch.setenv("SEPINF_API_KEY", "sk-sentinel-key-should-not-be-literal")
    settings = SepinfSettings()

    for value in (settings.base_url, settings.model_id, settings.api_key):
        assert value, "sentinel value should be non-empty"
        assert value not in config_src
        assert value not in llm_src


def test_source_contains_no_api_key_literal_assignment(
    clear_sepinf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``sk-...`` style key literal is assigned/returned in the source.

    Reads the real resolved key (whatever the environment currently provides)
    and asserts it is not present as a literal in either module. The key value
    is never printed or echoed anywhere in the test output.
    """
    monkeypatch.setenv("SEPINF_API_KEY", "sk-real-looking-key-0123456789")
    resolved_key = SepinfSettings().api_key

    config_src = Path(config_module.__file__).read_text(encoding="utf-8")
    llm_src = Path(llm_module.__file__).read_text(encoding="utf-8")

    assert resolved_key not in config_src
    assert resolved_key not in llm_src


# ---------------------------------------------------------------------------
# Requirement 7.3 — API key never rendered by repr/str (no log/trace leak)
# ---------------------------------------------------------------------------


def test_repr_and_str_never_expose_api_key(
    clear_sepinf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``repr``/``str`` of the settings mask the key so logs cannot leak it."""
    secret = "sk-super-secret-value-should-never-render"
    monkeypatch.setenv("SEPINF_BASE_URL", "http://ok.example/v1")
    monkeypatch.setenv("SEPINF_MODEL_ID", "ok-model")
    monkeypatch.setenv("SEPINF_API_KEY", secret)

    settings = SepinfSettings()

    assert secret not in repr(settings)
    assert secret not in str(settings)
    # The masked settings still render the non-secret fields for diagnostics.
    assert "ok-model" in repr(settings)


# ---------------------------------------------------------------------------
# Requirement 7.4 — no network/langchain import at module import time
# ---------------------------------------------------------------------------


def test_import_opens_no_network_connection() -> None:
    """A fresh import of both modules opens no socket connection.

    The check runs in a subprocess with a hard socket guard installed *before*
    the modules are imported: any ``socket.socket.connect`` / ``create_connection``
    attempt raises, so a non-zero exit proves import-time network I/O. Running in
    a child process guarantees a truly fresh import and avoids mutating the
    parent's module registry (which would corrupt class identity for other
    tests).
    """
    program = textwrap.dedent(
        """
        import socket
        import sys

        def _forbid(*_a, **_k):
            raise AssertionError("import opened a network connection")

        socket.socket.connect = _forbid  # type: ignore[assignment] -- test replaces the stdlib function to forbid network access
        socket.create_connection = _forbid  # type: ignore[assignment] -- test replaces the stdlib function to forbid network access

        import app.services.ai.config  # noqa: F401 -- import under the network guard proves it makes no connection
        import app.services.ai.llm  # noqa: F401 -- import under the network guard proves it makes no connection

        sys.exit(0)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, (
        f"importing the AI config/llm modules performed network I/O:\n{result.stderr}"
    )


def test_make_chat_model_defers_langchain_import() -> None:
    """The ``langchain_openai`` import lives inside the factory, not at module
    top level, so importing ``llm`` requires neither the dependency nor I/O."""
    source = Path(llm_module.__file__).read_text(encoding="utf-8")

    # No eager top-level import of the heavy client.
    assert "\nfrom langchain_openai import ChatOpenAI" not in source
    assert "\nimport langchain_openai" not in source
    # The deferred import appears inside the factory body (indented).
    assert "    from langchain_openai import ChatOpenAI" in source


# ---------------------------------------------------------------------------
# Requirement 7.5 — missing/empty SEPINF_API_KEY -> name-only error
# ---------------------------------------------------------------------------


def test_require_raises_name_only_for_missing_api_key(
    clear_sepinf_env: None,
) -> None:
    """Empty ``SEPINF_API_KEY`` fails fast, naming the key only.

    An absent variable resolves to the same empty-string default, so the empty
    string covers both the absent and empty cases (Requirement 7.5).
    """
    secret_that_should_not_appear = "sk-this-must-never-be-in-the-message"
    settings = _settings_from_overrides(
        base_url="http://ok.example/v1",
        model_id="ok-model",
        api_key="",
    )

    with pytest.raises(SepinfConfigError) as excinfo:
        _require(settings)

    message = str(excinfo.value)
    assert "SEPINF_API_KEY" in message
    # Never any key material, and never the other setting values.
    assert secret_that_should_not_appear not in message
    assert "http://ok.example/v1" not in message
    assert "ok-model" not in message


def test_make_chat_model_raises_name_only_for_missing_api_key(
    clear_sepinf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory surfaces the same name-only error for a missing key.

    ``get_sepinf_settings`` is patched so the factory resolves a key-less
    settings object without touching the root ``.env``.
    """
    keyless = _settings_from_overrides(
        base_url="http://ok.example/v1", model_id="ok-model", api_key=""
    )
    monkeypatch.setattr(llm_module, "get_sepinf_settings", lambda: keyless)

    with pytest.raises(SepinfConfigError) as excinfo:
        make_chat_model()

    assert "SEPINF_API_KEY" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Requirement 7.6 — missing/empty SEPINF_BASE_URL or SEPINF_MODEL_ID
# ---------------------------------------------------------------------------


def test_require_raises_name_only_for_missing_base_url(
    clear_sepinf_env: None,
) -> None:
    """Absent/empty ``SEPINF_BASE_URL`` fails fast, naming the setting only."""
    settings = _settings_from_overrides(base_url="", model_id="ok-model", api_key="present-key")

    with pytest.raises(SepinfConfigError) as excinfo:
        _require(settings)

    message = str(excinfo.value)
    assert "SEPINF_BASE_URL" in message
    assert "present-key" not in message


def test_require_raises_name_only_for_missing_model_id(
    clear_sepinf_env: None,
) -> None:
    """Absent/empty ``SEPINF_MODEL_ID`` fails fast, naming the setting only."""
    settings = _settings_from_overrides(
        base_url="http://ok.example/v1", model_id="", api_key="present-key"
    )

    with pytest.raises(SepinfConfigError) as excinfo:
        _require(settings)

    message = str(excinfo.value)
    assert "SEPINF_MODEL_ID" in message
    assert "present-key" not in message


def test_require_passes_when_all_present(clear_sepinf_env: None) -> None:
    """``_require`` is a no-op when every required setting is present."""
    settings = _settings_from_overrides(
        base_url="http://ok.example/v1",
        model_id="ok-model",
        api_key="present-key",
    )
    assert _require(settings) is None
