"""Pure path resolver for the STIX data directory (stix-bundle-cache-location).

This module decides *where* the STIX bundle lives, with **no side effects**: it
only computes a :class:`pathlib.Path`. It never creates directories and never
touches the network, so it is safe to call during import/instantiation of
``StixService`` (Req. 5.4). All path composition uses ``pathlib`` exclusively
(no hard-coded OS separators), which keeps the resolution portable across
Windows/Linux/macOS (Req. 5.3).

Two entry points:

* :func:`default_stix_data_dir` — the OS-specific default in the user's
  persistent cache (Req. 1.2, 1.3, 1.5).
* :func:`resolve_stix_data_dir` — the strict precedence chain
  ``injected > override (PIPELINE_UI_STIX_DIR) > default`` (Req. 2.1, 2.2, 2.3).

Every environment input (``env``, ``platform``, ``os_name``, ``home``) is an
injectable parameter so tests cover Windows and POSIX deterministically without
touching the real OS or filesystem (Req. 5.2). When a parameter is ``None`` the
function reads the real OS (``os.environ``, ``sys.platform``, ``os.name``,
``Path.home()``).

_Requirements: 1.1, 1.2, 1.3, 1.5, 2.1, 2.2, 2.3, 5.3, 5.4_
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

# Trailing path components appended to every resolved cache base. Kept as a
# tuple composed via ``pathlib`` so there is never a hard-coded separator
# (Req. 5.3).
_SUFFIX: tuple[str, ...] = ("sticks", "stix")


def _is_windows(*, platform: Optional[str], os_name: Optional[str]) -> bool:
    """True iff the (injected or real) OS indicators denote Windows.

    Detection matches either signal so both ``os.name`` and ``sys.platform``
    injected in tests are honoured: ``os_name == "nt"`` **or** ``platform``
    starting with ``"win"`` (Req. 1.2). ``None`` indicators fall back to the
    real OS.
    """
    name = os.name if os_name is None else os_name
    plat = sys.platform if platform is None else platform
    if name == "nt":
        return True
    return bool(plat) and plat.startswith("win")


def _resolve_home(home: Optional[Path]) -> Optional[Path]:
    """Return the home directory, or ``None`` when it cannot be resolved.

    When ``home`` is injected it is used verbatim. Otherwise ``Path.home()`` is
    attempted; any failure (environment without a home/profile) is swallowed and
    ``None`` is returned so the caller can fall back deterministically without
    propagating an error (Req. 1.5, 5.4).
    """
    if home is not None:
        return Path(home)
    try:
        return Path.home()
    except Exception:  # noqa: BLE001 — no home/profile; fall back deterministically.
        return None


def default_stix_data_dir(
    *,
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
    os_name: Optional[str] = None,
    home: Optional[Path] = None,
) -> Path:
    """Resolve the default STIX directory in the user's persistent cache.

    Pure: only computes a ``Path`` — no directory creation, no network
    (Req. 5.4). Uses ``pathlib`` exclusively (no hard-coded separators,
    Req. 5.3). Injectable parameters allow deterministic Windows/POSIX tests
    (Req. 5.2); when ``None`` they read the real OS (``os.environ``,
    ``sys.platform``, ``os.name``, ``Path.home()``).

    Default precedence (Req. 1.2, 1.3, 1.5):

    * Windows:  ``%LOCALAPPDATA%\\sticks\\stix``   (when ``LOCALAPPDATA`` set)
    * POSIX:    ``$XDG_CACHE_HOME/sticks/stix``    (when ``XDG_CACHE_HOME`` set)
                ``~/.cache/sticks/stix``           (when a home is available)
    * Fallback: ``~/.sticks/stix``                 (deterministic, never raises)
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    resolved_home = _resolve_home(home)

    if _is_windows(platform=platform, os_name=os_name):
        local_app_data = environ.get("LOCALAPPDATA")
        if local_app_data and local_app_data.strip():
            return Path(local_app_data).joinpath(*_SUFFIX)
        return _home_fallback(resolved_home)

    # POSIX branch.
    xdg_cache = environ.get("XDG_CACHE_HOME")
    if xdg_cache and xdg_cache.strip():
        return Path(xdg_cache).joinpath(*_SUFFIX)
    if resolved_home is not None:
        return resolved_home.joinpath(".cache", *_SUFFIX)
    return _home_fallback(resolved_home)


def _home_fallback(resolved_home: Optional[Path]) -> Path:
    """Deterministic fallback under the home directory (Req. 1.5).

    When a home directory is available it returns ``home / ".sticks" / "stix"``.
    When no home can be resolved it returns a deterministic relative path
    (``.sticks / stix``) so resolution is total and never raises during
    import/instantiation (Req. 1.5, 5.4).
    """
    base = resolved_home if resolved_home is not None else Path(".")
    return base.joinpath(".sticks", "stix")


def resolve_stix_data_dir(
    *,
    injected: Optional[Path] = None,
    override: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
    os_name: Optional[str] = None,
    home: Optional[Path] = None,
) -> Path:
    """Resolve the STIX data directory with strict precedence.

    Precedence (Req. 2.1, 2.2, 2.3)::

        injected  >  override (PIPELINE_UI_STIX_DIR)  >  default do SO

    ``injected`` has the highest precedence (preserves the current tests).
    ``override`` is used when non-empty after ``strip()`` (an absent/empty value
    is ignored, Req. 2.3). Otherwise it falls back to
    :func:`default_stix_data_dir`. Pure: no directory creation, no network.
    """
    if injected is not None:
        return Path(injected)
    if override is not None and override.strip():
        return Path(override)
    return default_stix_data_dir(
        env=env,
        platform=platform,
        os_name=os_name,
        home=home,
    )
