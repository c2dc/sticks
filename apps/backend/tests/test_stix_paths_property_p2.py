"""Property-based test for the default STIX dir OS mapping (stix-bundle-cache-location).

This file holds **Property 2** only (kept separate from
``test_stix_paths_property.py`` to avoid concurrent write conflicts while other
properties are added). It is tagged with the exact
``# Feature: stix-bundle-cache-location, Property N: <text>`` comment required by
the plan and runs with Hypothesis at ``max_examples=100`` (minimum 100
iterations).

Isolation (Req. 5.1, 5.2, 5.3): the resolver is a **pure** function. Every test
injects ``env`` / ``platform`` / ``os_name`` / ``home`` — the real OS, the real
filesystem and the network are never touched. No directories are created
(resolution only computes a ``Path``).
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.stix.stix_paths import default_stix_data_dir

# Shared iteration budget so the property runs >= 100 examples.
PROP_SETTINGS = settings(max_examples=100, deadline=None)

# Expected trailing components appended to every resolved cache base.
_SUFFIX = ("sticks", "stix")

# Path-like strings used as directory bases. Kept simple and portable (no
# OS-specific separators required) so the resolver's pathlib composition is what
# is exercised, not the generator.
_base_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="/._-",
    ),
    min_size=1,
    max_size=24,
)

# POSIX platform/os_name pairs covering both Linux and macOS.
_posix_os = st.sampled_from(
    [
        ("linux", "posix"),
        ("darwin", "posix"),
    ]
)

# Windows platform/os_name pairs covering both detection signals.
_win_os = st.sampled_from(
    [
        ("win32", "nt"),
        ("win32", "posix"),  # platform signal alone
        ("linux", "nt"),  # os_name signal alone
    ]
)


# ---------------------------------------------------------------------------
# Property 2: default-per-OS mapping
# ---------------------------------------------------------------------------


# Feature: stix-bundle-cache-location, Property 2: Mapeamento do default por SO.
# Para qualquer diretório-base b: com plataforma Windows e env["LOCALAPPDATA"] == b,
# default_stix_data_dir retorna Path(b) / "sticks" / "stix"; com plataforma POSIX
# e env["XDG_CACHE_HOME"] == b, retorna Path(b) / "sticks" / "stix"; com
# plataforma POSIX, sem XDG_CACHE_HOME e home == b, retorna
# Path(b) / ".cache" / "sticks" / "stix".
# Validates: Requirements 1.2, 1.3
@PROP_SETTINGS
@given(
    base=_base_text,
    win_os=_win_os,
    posix_os=_posix_os,
)
def test_default_os_mapping(base, win_os, posix_os):
    # Windows: LOCALAPPDATA base -> base / sticks / stix.
    win_platform, win_name = win_os
    win_resolved = default_stix_data_dir(
        env={"LOCALAPPDATA": base},
        platform=win_platform,
        os_name=win_name,
        home=Path("/home/unused"),
    )
    assert win_resolved == Path(base).joinpath(*_SUFFIX)
    assert win_resolved.parts[-2:] == _SUFFIX

    posix_platform, posix_name = posix_os

    # POSIX with XDG_CACHE_HOME set -> base / sticks / stix.
    xdg_resolved = default_stix_data_dir(
        env={"XDG_CACHE_HOME": base},
        platform=posix_platform,
        os_name=posix_name,
        home=Path("/home/unused"),
    )
    assert xdg_resolved == Path(base).joinpath(*_SUFFIX)
    assert xdg_resolved.parts[-2:] == _SUFFIX

    # POSIX without XDG_CACHE_HOME, home == base -> base / .cache / sticks / stix.
    cache_resolved = default_stix_data_dir(
        env={},
        platform=posix_platform,
        os_name=posix_name,
        home=Path(base),
    )
    assert cache_resolved == Path(base).joinpath(".cache", *_SUFFIX)
    assert cache_resolved.parts[-3:] == (".cache",) + _SUFFIX
