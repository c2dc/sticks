"""Bridge to the existing ``sticks/config/config.py`` module.

The backend is *additive*: it must reuse the values already defined by the
existing pipeline (e.g. ``CALDERA_URL`` and the Caldera API key used as the
``KEY`` header) instead of duplicating them here.

``sticks/config/config.py`` is a plain module (its parent directory is not a
formal Python package with ``__init__.py``), so we load it directly by file
path. This keeps the integration robust on Windows and does not require the
``sticks`` package to be importable / installed.

This module never modifies any file under ``sticks/`` — it only reads values.
"""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType


class SticksConfigError(RuntimeError):
    """Raised when the existing sticks config cannot be located or loaded."""


def _repo_root() -> Path:
    """Return the repository root (the folder that contains ``sticks/``).

    Layout:  <repo>/pipeline_ui/backend/app/core/sticks_config.py
             <repo>/sticks/config/config.py
    """
    # sticks_config.py -> core -> app -> backend -> pipeline_ui -> <repo>
    return Path(__file__).resolve().parents[4]


def _sticks_config_path() -> Path:
    return _repo_root() / "sticks" / "config" / "config.py"


@lru_cache(maxsize=1)
def load_sticks_config() -> ModuleType:
    """Load and cache the existing ``sticks/config/config.py`` as a module.

    Raises:
        SticksConfigError: if the file does not exist or cannot be imported.
    """
    config_path = _sticks_config_path()
    if not config_path.is_file():
        raise SticksConfigError(
            f"Existing sticks config not found at {config_path}. "
            "The backend must run inside the sticks repository."
        )

    spec = importlib.util.spec_from_file_location("sticks_pipeline_config", config_path)
    if spec is None or spec.loader is None:
        raise SticksConfigError(f"Could not build an import spec for {config_path}.")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - defensive
        raise SticksConfigError(f"Failed to load sticks config from {config_path}: {exc}") from exc
    return module
