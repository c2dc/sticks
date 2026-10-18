"""Property-based test for Property 3 of the pure STIX path resolver
(stix-bundle-cache-location).

This file is intentionally separate from ``test_stix_paths_property.py`` to
avoid write conflicts while sibling properties are added concurrently. It
mirrors the imports and style of that file.

Isolation (Req. 5.1, 5.2, 5.3): the resolver is a **pure** function. Every test
injects ``env`` / ``platform`` / ``os_name`` / ``home`` — the real OS, the real
filesystem and the network are never touched. No directories are created
(resolution only computes a ``Path``). The repo-root is itself a controlled,
injected base so the assertion never depends on where the test actually runs.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.stix.stix_paths import default_stix_data_dir

# Shared iteration budget so the property runs >= 100 examples.
PROP_SETTINGS = settings(max_examples=100, deadline=None)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Path-like strings used to build base/home values. Kept simple and portable
# (no OS-specific separators) so the resolver's pathlib composition is what is
# exercised, not the generator.
_path_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="._-",
    ),
    min_size=1,
    max_size=16,
)

# Platform / os_name pairs covering both Windows and POSIX deterministically.
# The POSIX pair uses a POSIX-style controlled repo root / home; the Windows
# pair uses a Windows-style drive-rooted repo root / home. This keeps the
# ``is_relative_to`` comparison on a single pure-path flavour per example so the
# result is deterministic regardless of the host OS running the suite.
_os_matrix = st.sampled_from(
    [
        ("win32", "nt"),  # Windows
        ("linux", "posix"),  # Linux
        ("darwin", "posix"),  # macOS
    ]
)


# ---------------------------------------------------------------------------
# Property 3: default resolved outside the repository root
# ---------------------------------------------------------------------------


# Feature: stix-bundle-cache-location, Property 3: Default resolvido fora da
# raiz do repositório. Para qualquer env, plataforma e home que não esteja
# contido na raiz do repositório (_repo_root()), o caminho retornado por
# default_stix_data_dir SHALL NOT ser relativo a _repo_root() — ou seja,
# resolved.is_relative_to(repo_root) é False.
# Validates: Requirements 1.1, 1.4, 4.1
@PROP_SETTINGS
@given(
    os_pair=_os_matrix,
    base_seg=_path_text,
    home_seg=_path_text,
    local_app_data=st.one_of(st.none(), _path_text),
    xdg_cache=st.one_of(st.none(), _path_text),
)
def test_default_outside_repo_root(os_pair, base_seg, home_seg, local_app_data, xdg_cache):
    platform, os_name = os_pair
    is_windows = os_name == "nt"

    # A controlled repo-root and a home/env base that live on sibling branches
    # of the tree, so the resolved default can never be inside the repo-root.
    # Using a distinct top-level segment for each guarantees non-containment
    # regardless of the random tail segments.
    if is_windows:
        repo_root = PureWindowsPath("C:/__repo_root__/project")
        home = PureWindowsPath(f"C:/__user_home__/{home_seg}")
        base = f"C:/__cache_base__/{base_seg}"
    else:
        repo_root = PurePosixPath("/__repo_root__/project")
        home = PurePosixPath(f"/__user_home__/{home_seg}")
        base = f"/__cache_base__/{base_seg}"

    env: dict[str, str] = {}
    # Populate the base cache var for the active OS branch so every branch of
    # the resolver (explicit base var and home fallback) is exercised.
    if is_windows and local_app_data is not None:
        env["LOCALAPPDATA"] = f"C:/__cache_base__/{local_app_data}"
    if not is_windows and xdg_cache is not None:
        env["XDG_CACHE_HOME"] = f"/__cache_base__/{xdg_cache}"

    resolved = default_stix_data_dir(
        env=env,
        platform=platform,
        os_name=os_name,
        home=home,
    )

    # Compare on the same pure-path flavour as the controlled repo-root so the
    # containment check is deterministic on any host OS.
    pure = PureWindowsPath(str(resolved)) if is_windows else PurePosixPath(str(resolved))

    assert not pure.is_relative_to(repo_root)
