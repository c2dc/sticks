"""Lazy loader of the curated seed JSON files (``app/seeds/data/<slug>.json``).

Nothing is read at import time. Files are located through
:mod:`importlib.resources` (package ``app.seeds``, sub-folder ``data``), so they
work from a source tree, an installed wheel and the Docker image alike, and are
decoded as UTF-8. Each file holds one curated case with the keys ``campaign``,
``abilities``, ``adversary`` and ``attack_flow``. The files carry no ``curated``
flag and no flow ``origin``: the applier always stores campaigns with
``curated = true`` and flows with ``origin = human_curation``.

Each ``campaign`` declares ``stix_version`` (dotted numbers such as ``18.0``, the
ATT&CK version the case was curated against) and ``stix_campaign_key`` (``C``
plus four digits, the STIX campaign external id); both are validated here. They are not stored in
the database (``campaigns`` has no column for them): the binding is derived from
the seed and :mod:`app.seeds.manifest`.

Only a minimal structural validation is done here (required keys and types); a
violation raises :class:`SeedDataError` naming the file and the offending key.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

_PACKAGE = "app.seeds"
_DATA_DIR = "data"

_REQUIRED_TYPES: dict[str, type] = {
    "campaign": dict,
    "abilities": list,
    "adversary": dict,
    "attack_flow": dict,
}
_CAMPAIGN_KEYS = ("id", "name", "translation_source", "stix_version", "stix_campaign_key")
# STIX binding of each case: ATT&CK version curated against and the STIX
# campaign external id (see ``app.seeds.manifest``).
_STIX_CAMPAIGN_PATTERNS: dict[str, re.Pattern[str]] = {
    "stix_version": re.compile(r"\d+(\.\d+)+"),
    "stix_campaign_key": re.compile(r"C\d{4}"),
}
_ABILITY_KEYS = ("ability_id", "campaign_id", "translation_source")
_ADVERSARY_KEYS = ("id", "campaign_id", "atomic_ordering", "translation_source")
_FLOW_KEYS = ("campaign_id", "identifier", "content")


class SeedDataError(ValueError):
    """A curated seed file is missing, unreadable or structurally invalid."""


def _data_root() -> Traversable:
    return resources.files(_PACKAGE).joinpath(_DATA_DIR)


def list_slugs() -> tuple[str, ...]:
    """Sorted slugs derived from the ``*.json`` file names (no file is opened)."""
    root = _data_root()
    if not root.is_dir():
        raise SeedDataError(f"seed data directory not found: {_PACKAGE}/{_DATA_DIR}")
    return tuple(
        sorted(
            entry.name[: -len(".json")]
            for entry in root.iterdir()
            if entry.is_file() and entry.name.endswith(".json")
        )
    )


def _require_keys(where: str, item: Any, keys: tuple[str, ...]) -> None:
    if not isinstance(item, dict):
        raise SeedDataError(f"{where}: expected an object, got {type(item).__name__}")
    for key in keys:
        if key not in item:
            raise SeedDataError(f"{where}: missing key {key!r}")


def _validate(slug: str, data: Any) -> dict[str, Any]:
    name = f"{slug}.json"
    if not isinstance(data, dict):
        raise SeedDataError(f"{name}: top level must be an object")
    for key, expected in _REQUIRED_TYPES.items():
        if key not in data:
            raise SeedDataError(f"{name}: missing key {key!r}")
        if not isinstance(data[key], expected):
            raise SeedDataError(
                f"{name}: key {key!r} must be {expected.__name__}, got {type(data[key]).__name__}"
            )
    _require_keys(f"{name}: campaign", data["campaign"], _CAMPAIGN_KEYS)
    if data["campaign"]["id"] != slug:
        raise SeedDataError(f"{name}: campaign.id {data['campaign']['id']!r} != file slug")
    for key, pattern in _STIX_CAMPAIGN_PATTERNS.items():
        value = data["campaign"].get(key)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise SeedDataError(
                f"{name}: campaign.{key} {value!r} does not match {pattern.pattern!r}"
            )
    for index, ability in enumerate(data["abilities"]):
        _require_keys(f"{name}: abilities[{index}]", ability, _ABILITY_KEYS)
    _require_keys(f"{name}: adversary", data["adversary"], _ADVERSARY_KEYS)
    if not isinstance(data["adversary"]["atomic_ordering"], list):
        raise SeedDataError(f"{name}: adversary.atomic_ordering must be list")
    _require_keys(f"{name}: attack_flow", data["attack_flow"], _FLOW_KEYS)
    if not isinstance(data["attack_flow"]["content"], dict):
        raise SeedDataError(f"{name}: attack_flow.content must be dict")
    return data


def load_case(slug: str) -> dict[str, Any]:
    """Read and validate one curated case file."""
    entry = _data_root().joinpath(f"{slug}.json")
    try:
        text = entry.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise SeedDataError(f"{slug}.json: cannot read seed file ({type(exc).__name__})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SeedDataError(f"{slug}.json: invalid JSON ({exc.msg} at line {exc.lineno})") from exc
    return _validate(slug, data)


def load_all() -> list[dict[str, Any]]:
    """Read every curated case, ordered by slug."""
    return [load_case(slug) for slug in list_slugs()]


__all__ = ["SeedDataError", "list_slugs", "load_all", "load_case"]
