"""Property-based tests for the pure STIX path resolver (stix-bundle-cache-location).

Each property-based test is tagged with the exact
``# Feature: stix-bundle-cache-location, Property N: <text>`` comment required by
the plan and runs with Hypothesis at ``max_examples=100`` (minimum 100
iterations).

Isolation (Req. 5.1, 5.2, 5.3): the resolver is a **pure** function. Every test
injects ``injected`` / ``override`` / ``env`` / ``platform`` / ``os_name`` /
``home`` — the real OS, the real filesystem and the network are never touched.
No directories are created (resolution only computes a ``Path``).
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.stix.stix_paths import (
    default_stix_data_dir,
    resolve_stix_data_dir,
)

# Shared iteration budget so every property runs >= 100 examples.
PROP_SETTINGS = settings(max_examples=100, deadline=None)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Path-like strings used as injected / override values. Kept simple and portable
# (no OS-specific separators required) so the resolver's pathlib composition is
# what is exercised, not the generator.
_path_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="/._-",
    ),
    min_size=1,
    max_size=24,
)

# Optional injected Path (``None`` means "not injected").
_injected = st.one_of(st.none(), _path_text.map(Path))

# Optional override string. Includes ``None``, empty/whitespace strings (which
# must be ignored, Req. 2.3) and genuine non-empty path strings.
_override = st.one_of(
    st.none(),
    st.sampled_from(["", "   ", "\t", "\n"]),
    _path_text,
)

# An environment map that may or may not carry the base cache keys, so the
# default branch is exercised across the OS matrix.
_env = st.fixed_dictionaries(
    {},
    optional={
        "LOCALAPPDATA": _path_text,
        "XDG_CACHE_HOME": _path_text,
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
# Property 1: strict precedence of the resolution
# ---------------------------------------------------------------------------


# Feature: stix-bundle-cache-location, Property 1: Precedência estrita da
# resolução. Para qualquer injected (opcional), override (opcional) e mapa de
# ambiente env, resolve_stix_data_dir retorna Path(injected) quando injected não
# é None (precedência máxima); caso contrário Path(override) quando override é
# não-vazio após strip(); caso contrário o default do SO calculado a partir de
# env (igual a default_stix_data_dir(env=...)). Um override ausente/vazio nunca
# altera o resultado do default.
# Validates: Requirements 2.1, 2.2, 2.3
@PROP_SETTINGS
@given(
    injected=_injected,
    override=_override,
    env=_env,
    os_pair=_os_matrix,
    home=_home,
)
def test_resolve_strict_precedence(injected, override, env, os_pair, home):
    platform, os_name = os_pair

    resolved = resolve_stix_data_dir(
        injected=injected,
        override=override,
        env=env,
        platform=platform,
        os_name=os_name,
        home=home,
    )

    if injected is not None:
        # Highest precedence: injected wins regardless of override/env.
        assert resolved == Path(injected)
    elif override is not None and override.strip():
        # Non-empty override wins over the OS default.
        assert resolved == Path(override)
    else:
        # Absent/empty override is ignored → falls back to the OS default,
        # identical to calling default_stix_data_dir with the same inputs.
        expected_default = default_stix_data_dir(
            env=env,
            platform=platform,
            os_name=os_name,
            home=home,
        )
        assert resolved == expected_default

    # An absent/empty override must never alter the default result: resolving
    # with override=None yields the same path as any empty/whitespace override
    # when nothing is injected.
    if injected is None and not (override is not None and override.strip()):
        with_none = resolve_stix_data_dir(
            injected=None,
            override=None,
            env=env,
            platform=platform,
            os_name=os_name,
            home=home,
        )
        assert resolved == with_none
