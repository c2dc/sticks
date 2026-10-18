"""Property-based test for the deterministic, error-free fallback of the pure
STIX path resolver (stix-bundle-cache-location, Property 4).

Kept in a dedicated file to avoid write conflicts with the other property tests
being added concurrently to ``test_stix_paths_property.py``; it mirrors that
module's imports and style.

Isolation (Req. 5.1, 5.2, 5.3): the resolver is a **pure** function. Every test
injects ``env`` / ``platform`` / ``os_name`` / ``home`` — the real OS, the real
filesystem and the network are never touched, and no directory is created
(resolution only computes a ``Path``).
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.stix.stix_paths import default_stix_data_dir

# Shared iteration budget so the property runs >= 100 examples.
PROP_SETTINGS = settings(max_examples=100, deadline=None)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Path-like strings used for home values. Simple and portable (no OS-specific
# separators required) so the resolver's pathlib composition is what is
# exercised, not the generator.
_path_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="/._-",
    ),
    min_size=1,
    max_size=24,
)

# Environments WITHOUT the base cache keys (no ``LOCALAPPDATA`` / ``XDG_CACHE_HOME``)
# so the fallback branch is always exercised. Only unrelated keys may be present.
_env_without_base_keys = st.fixed_dictionaries(
    {},
    optional={
        "PATH": _path_text,
        "SOME_OTHER_KEY": _path_text,
    },
)

# Platform / os_name pairs covering both Windows and POSIX deterministically.
_os_matrix = st.sampled_from(
    [
        ("win32", "nt"),  # Windows
        ("linux", "posix"),  # Linux
        ("darwin", "posix"),  # macOS
    ]
)

# A home directory outside any repo, injected so ``Path.home()`` is never read.
_home = st.sampled_from(
    [
        Path("/home/tester"),
        Path("/Users/tester"),
        Path("C:/Users/tester"),
    ]
)


# ---------------------------------------------------------------------------
# Property 4: deterministic, error-free fallback
# ---------------------------------------------------------------------------


# Feature: stix-bundle-cache-location, Property 4: Fallback determinístico sem
# erro. Para qualquer env sem as chaves de base (LOCALAPPDATA, XDG_CACHE_HOME),
# a resolução do default NÃO lança exceção e é determinística (duas chamadas com
# o mesmo input retornam o mesmo Path); quando apenas o home está disponível
# (Windows sem LOCALAPPDATA), o resultado termina em (".sticks", "stix").
# Validates: Requirements 1.5
@PROP_SETTINGS
@given(
    env=_env_without_base_keys,
    os_pair=_os_matrix,
    home=_home,
)
def test_default_fallback_is_deterministic_and_never_raises(env, os_pair, home):
    platform, os_name = os_pair

    # Must not raise even without any base cache key in the environment.
    first = default_stix_data_dir(
        env=env,
        platform=platform,
        os_name=os_name,
        home=home,
    )
    # Deterministic: the same input yields an equal Path on a second call.
    second = default_stix_data_dir(
        env=env,
        platform=platform,
        os_name=os_name,
        home=home,
    )
    assert first == second

    # The last component is always "stix".
    assert first.parts[-1] == "stix"

    is_windows = os_name == "nt" or platform.startswith("win")
    if is_windows:
        # Windows without LOCALAPPDATA → only the home is available → the
        # deterministic home fallback "~/.sticks/stix", ending in
        # (".sticks", "stix").
        assert first == Path(home).joinpath(".sticks", "stix")
        assert first.parts[-2:] == (".sticks", "stix")
    else:
        # POSIX without XDG_CACHE_HOME but with a home → "~/.cache/sticks/stix",
        # ending in the shared ("sticks", "stix") suffix.
        assert first == Path(home).joinpath(".cache", "sticks", "stix")
        assert first.parts[-2:] == ("sticks", "stix")
