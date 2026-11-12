"""Deterministic doubles for the in-memory STIX download (no network, no disk).

``FakeStixHttp`` stands in for ``requests``: it serves STIX bundles from a dict
keyed by version, so ``StixService.fetch_bundle`` runs end to end without any
network access. It records every requested URL in ``calls``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_VERSION_IN_URL = re.compile(r"enterprise-attack-(\d+(?:\.\d+)+)\.json$")


class FakeStixResponse:
    """Minimal ``requests.Response`` look-alike."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeStixHttp:
    """HTTP client double serving ``{version: bundle}`` from memory."""

    def __init__(
        self,
        bundles: Optional[dict[str, Any]] = None,
        *,
        fail_versions: Optional[set[str]] = None,
    ) -> None:
        self.bundles: dict[str, Any] = dict(bundles or {})
        self.fail_versions: set[str] = set(fail_versions or ())
        self.calls: list[str] = []

    def get(self, url: str, timeout: Optional[float] = None) -> FakeStixResponse:
        self.calls.append(url)
        match = _VERSION_IN_URL.search(url)
        version = match.group(1) if match else None
        if version is None or version in self.fail_versions:
            raise ConnectionError("simulated network failure")
        if version not in self.bundles:
            return FakeStixResponse("not found", status_code=404)
        body = self.bundles[version]
        return FakeStixResponse(body if isinstance(body, str) else json.dumps(body))


def small_bundle(campaign_name: str = "Alpha Campaign") -> dict:
    """A tiny valid bundle: one campaign, one technique, one actor, three edges."""
    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-0000-0000-000000000000",
        "objects": [
            {
                "type": "campaign",
                "id": "campaign--00000000-0000-0000-0000-000000000001",
                "name": campaign_name,
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "C9001"}
                ],
            },
            {
                "type": "attack-pattern",
                "id": "attack-pattern--00000000-0000-0000-0000-00000000000a",
                "name": "Command and Scripting Interpreter",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "T1059"}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
                ],
            },
            {
                "type": "intrusion-set",
                "id": "intrusion-set--00000000-0000-0000-0000-0000000000f1",
                "name": "APT-Example",
            },
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000011",
                "relationship_type": "uses",
                "source_ref": "campaign--00000000-0000-0000-0000-000000000001",
                "target_ref": "attack-pattern--00000000-0000-0000-0000-00000000000a",
            },
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000012",
                "relationship_type": "attributed-to",
                "source_ref": "campaign--00000000-0000-0000-0000-000000000001",
                "target_ref": "intrusion-set--00000000-0000-0000-0000-0000000000f1",
            },
        ],
    }
