"""Ingestion settings read like the other project variables, and the work directory.

``read_env`` resolves a variable from the process environment first and then from
the repository-root ``.env`` (same lookup order as ``AI_EMBEDDING_DIM``). A
variable that is present in the process environment, even empty, wins.

``INGEST_WORK_DIR`` names an absolute directory where the ingestion may put
temporary files (for example one that has an antivirus exception). By default
(unset or empty) the ingestion does not touch the disk: sources are parsed in
memory. When a source needs disk anyway it uses the operating system temp
directory. When the variable is set and valid, every temporary directory is a
unique ``<prefix>_<uuid>`` subfolder of it, removed when the work ends (success or
failure). An invalid value (relative path, not creatable, not writable) logs a
warning and behaves as unset; it never raises.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

INGEST_WORK_DIR_ENV = "INGEST_WORK_DIR"

# parents: [ingestion] [services] [app] [backend] [apps] [repo-root]
_ROOT_ENV_PATH = Path(__file__).resolve().parents[5] / ".env"


def _read_root_env(key: str) -> str | None:
    """Value of ``key`` in the repository-root ``.env`` (simple ``KEY=value`` lines)."""
    try:
        text = _ROOT_ENV_PATH.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    found: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            found = value
    return found


def read_env(name: str) -> str | None:
    """Process environment first, then the root ``.env``; ``None`` when absent."""
    if name in os.environ:
        return os.environ[name]
    return _read_root_env(name)


def read_env_int(name: str, default: int) -> int:
    """Non-negative integer setting; ``default`` when unset or invalid."""
    raw = (read_env(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def configured_work_dir() -> Path | None:
    """The valid ``INGEST_WORK_DIR`` or ``None`` (unset, empty or invalid)."""
    raw = (read_env(INGEST_WORK_DIR_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        logger.warning("%s must be an absolute path; ignoring %r.", INGEST_WORK_DIR_ENV, raw)
        return None
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as exc:
        logger.warning(
            "%s=%r is not usable (%s: %s); using the default.",
            INGEST_WORK_DIR_ENV,
            raw,
            type(exc).__name__,
            exc,
        )
        return None
    return path


@contextmanager
def scratch_dir(prefix: str, *, only_configured: bool = False) -> Iterator[Path | None]:
    """A unique temporary directory, removed on exit (success or failure).

    Inside ``INGEST_WORK_DIR`` when it is valid, else in the OS temp directory.
    With ``only_configured=True`` and no valid work directory it yields ``None``
    so the caller can stay in memory instead of touching the disk.
    """
    base = configured_work_dir()
    if base is None and only_configured:
        yield None
        return
    path: Path | None = None
    if base is not None:
        candidate = base / f"{prefix}_{uuid.uuid4().hex}"
        try:
            candidate.mkdir()
            path = candidate
        except OSError as exc:
            logger.warning("cannot create %s (%s); using the OS temp directory.", candidate, exc)
    if path is None:
        path = Path(tempfile.mkdtemp(prefix=f"{prefix}_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "INGEST_WORK_DIR_ENV",
    "configured_work_dir",
    "read_env",
    "read_env_int",
    "scratch_dir",
]
