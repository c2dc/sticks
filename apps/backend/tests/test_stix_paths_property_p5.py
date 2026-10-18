"""Property-based test for pathlib portability of the STIX path resolver
(stix-bundle-cache-location, Property 5).

Isolated in a dedicated file to avoid write conflicts with concurrently
authored properties in ``test_stix_paths_property.py``. It follows the same
imports/style as that file: Hypothesis at ``max_examples=100`` (minimum 100
iterations), the resolver exercised as a **pure** function with every
environment input injected (``env`` / ``platform`` / ``os_name`` / ``home``),
never touching the real OS, the real filesystem or the network. Resolution only
computes a ``Path`` — no directories are created.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.stix.stix_paths import default_stix_data_dir

# Shared iteration budget so the property runs >= 100 examples.
PROP_SETTINGS = settings(max_examples=100, deadline=None)

# The trailing components every resolved cache base must end with.
_SUFFIX_PARTS = ("sticks", "stix")

# Path-like strings used as the injected base directory. Kept simple and
# portable (no OS-specific separators required) so the resolver's pathlib
# composition is what is exercised, not the generator.
_path_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="/._-",
    ),
    min_size=1,
    max_size=24,
)

# Platform / os_name pairs covering both Windows and POSIX deterministically,
# paired with the base cache env key that maps directly to ``base / sticks / stix``.
#   Windows → LOCALAPPDATA ; POSIX → XDG_CACHE_HOME
_os_base_key = st.sampled_from(
    [
        ("win32", "nt", "LOCALAPPDATA"),  # Windows
        ("linux", "posix", "XDG_CACHE_HOME"),  # Linux
        ("darwin", "posix", "XDG_CACHE_HOME"),  # macOS
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


# Feature: stix-bundle-cache-location, Property 5: Portabilidade por pathlib.
# Para qualquer diretório-base e plataforma (Windows/POSIX) injetados, o caminho
# resolvido é exatamente a composição pathlib da base com o sufixo esperado —
# isto é, suas duas últimas componentes (resolved.parts[-2:]) são
# ("sticks", "stix") e resolved == Path(base).joinpath(*suffix_parts) —
# propriedade que só se sustenta se a construção usar exclusivamente pathlib,
# sem separadores hard-coded.
# Validates: Requirements 5.3
@PROP_SETTINGS
@given(base=_path_text, os_base_key=_os_base_key, home=_home)
def test_default_is_pure_pathlib_composition(base, os_base_key, home):
    platform, os_name, base_key = os_base_key

    resolved = default_stix_data_dir(
        env={base_key: base},
        platform=platform,
        os_name=os_name,
        home=home,
    )

    # The resolved path is exactly the pathlib composition of the base with the
    # expected suffix — holds only if composition uses pathlib exclusively.
    assert resolved == Path(base).joinpath(*_SUFFIX_PARTS)
    # Its last two components are ("sticks", "stix").
    assert resolved.parts[-2:] == _SUFFIX_PARTS
