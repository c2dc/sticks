"""Static guard: backend sources must not reference the legacy ``infra/docker`` path.

The lab moved to ``ops/docker``. This test scans the backend application and
test sources and fails if the old path reappears.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("app", "tests")
_LEGACY = re.compile(r"infra[/\\]+docker", re.IGNORECASE)
_LEGACY_JOINED = re.compile(r"""["']infra["']\s*/\s*["']docker["']""")


def test_backend_sources_do_not_reference_legacy_infra_docker_path() -> None:
    offenders: list[str] = []
    for name in _SCAN_DIRS:
        for path in (_BACKEND_ROOT / name).rglob("*.py"):
            if path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if _LEGACY.search(text) or _LEGACY_JOINED.search(text):
                offenders.append(str(path.relative_to(_BACKEND_ROOT)))
    assert not offenders, f"legacy infra/docker path referenced in: {offenders}"
