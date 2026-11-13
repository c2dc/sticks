"""Fetch strategy for the MITRE Attack Flow corpus (``.afb`` files).

One GitHub ``git/trees`` call lists the corpus at a ref; each file is then
downloaded from ``raw.githubusercontent.com``. Importing this module performs
no I/O. The ingest service accepts any object with the same two methods, so
tests can double the fetcher with no network.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_REF = "v4.1.0"
_REPO = "center-for-threat-informed-defense/attack-flow"
_API_ROOT = "https://api.github.com"
_RAW_ROOT = "https://raw.githubusercontent.com"
_CORPUS_PREFIX = "corpus/"
_CORPUS_SUFFIX = ".afb"


class CorpusFetchError(RuntimeError):
    """The corpus listing or a file download failed (network, 403/429, bad JSON)."""


@dataclass(frozen=True)
class CorpusFile:
    """One corpus entry from the tag listing; ``name`` is the ``source_key``."""

    name: str  # file name without the "corpus/" prefix
    sha: str  # git blob SHA


class AttackFlowCorpusFetcher:
    """Lists and downloads the ``.afb`` corpus of the Attack Flow repository."""

    def __init__(
        self,
        *,
        ref: str | None = None,
        token: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._ref = ref or os.environ.get("ATTACK_FLOW_REF") or DEFAULT_REF
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._timeout = timeout

    @property
    def ref(self) -> str:
        return self._ref

    def list_files(self, ref: str | None = None) -> list[CorpusFile]:
        """Single ``git/trees/{ref}?recursive=1`` call, filtered to corpus ``.afb``."""
        resolved = ref or self._ref
        url = (
            f"{_API_ROOT}/repos/{_REPO}/git/trees/"
            f"{urllib.parse.quote(resolved, safe='')}?recursive=1"
        )
        document = json.loads(self._get(url, accept="application/vnd.github+json"))
        tree = document.get("tree") if isinstance(document, dict) else None
        if not isinstance(tree, list):
            raise CorpusFetchError("github tree listing has no 'tree' list")
        files: list[CorpusFile] = []
        for entry in tree:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            sha = entry.get("sha")
            if (
                entry.get("type") == "blob"
                and isinstance(path, str)
                and isinstance(sha, str)
                and path.startswith(_CORPUS_PREFIX)
                and path.endswith(_CORPUS_SUFFIX)
                and "/" not in path[len(_CORPUS_PREFIX) :]
            ):
                files.append(CorpusFile(name=path[len(_CORPUS_PREFIX) :], sha=sha))
        return sorted(files, key=lambda f: f.name)

    def fetch(self, corpus_file: CorpusFile, ref: str | None = None) -> bytes:
        """Download one file; the name is percent-encoded (spaces, ``&``)."""
        resolved = ref or self._ref
        url = (
            f"{_RAW_ROOT}/{_REPO}/{urllib.parse.quote(resolved, safe='')}/"
            f"corpus/{urllib.parse.quote(corpus_file.name, safe='')}"
        )
        return self._get(url)

    def _get(self, url: str, *, accept: str | None = None) -> bytes:
        headers: dict[str, Any] = {"User-Agent": "sticks-attack-flow-ingest"}
        if accept:
            headers["Accept"] = accept
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, headers=headers)  # noqa: S310 -- URL comes from the pinned corpus release, not user input
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 -- URL comes from the pinned corpus release, not user input
                return response.read()
        except Exception as exc:
            raise CorpusFetchError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc


__all__ = [
    "DEFAULT_REF",
    "AttackFlowCorpusFetcher",
    "CorpusFetchError",
    "CorpusFile",
]
