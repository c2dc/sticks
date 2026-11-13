"""Test helper: turn an on-disk ``atomics/`` fixture tree into fetcher documents."""

from __future__ import annotations

import re
from pathlib import Path

from app.services.atomic_red_team_ingest_service import AtomicDocument

_TECHNIQUE_DIR_RE = re.compile(r"^T\d{4}(?:\.\d{3})?$")


def documents_from_dir(atomics_dir: Path) -> list[AtomicDocument]:
    """Read every ``T####/T####.yaml`` under ``atomics_dir`` as an ``AtomicDocument``."""
    documents: list[AtomicDocument] = []
    for technique_dir in sorted(atomics_dir.iterdir()):
        if not technique_dir.is_dir() or not _TECHNIQUE_DIR_RE.match(technique_dir.name):
            continue
        atomic = technique_dir / f"{technique_dir.name}.yaml"
        if atomic.is_file():
            documents.append(AtomicDocument(technique_dir.name, atomic.read_bytes()))
    return documents


# ---------------------------------------------------------------------------
# Documents with ``input_arguments`` (Req. 11)
# ---------------------------------------------------------------------------

ARGUMENTS_TECHNIQUE = "T1560.001"
ARGUMENTS_COMMANDS = {
    "with_defaults": (
        "7z a -p#{zip_password} #{output_file} #{input_file} && echo #{zip_password}"
    ),
    "path_to_atomics": ("cp $PathToAtomicsFolder/T1560.001/src/payload.txt #{output_file}"),
    "unknown_placeholder": "run #{vbscript} with #{output_file}",
    "no_arguments_stored": "echo #{orphan}",
}


def arguments_document() -> AtomicDocument:
    """One technique with four tests covering every substitution case.

    * ``with_defaults``: repeated placeholder, defaults with and without
      ``PathToAtomicsFolder``.
    * ``path_to_atomics``: command-level ``$PathToAtomicsFolder``.
    * ``unknown_placeholder``: ``vbscript`` is declared without ``default``
      (and ``output_file`` has one), so it must stay unresolved.
    * ``no_arguments_stored``: placeholder with no ``input_arguments`` at all.
    """
    tests = [
        {
            "name": "with defaults",
            "supported_platforms": ["windows"],
            "input_arguments": {
                "zip_password": {
                    "description": "password",
                    "type": "string",
                    "default": "infected",
                },
                "output_file": {
                    "description": "out",
                    "type": "path",
                    "default": "$PathToAtomicsFolder/out.zip",
                },
                "input_file": {
                    "description": "in",
                    "type": "path",
                    "default": "C:\\Temp\\in.txt",
                },
            },
            "executor": {
                "name": "command_prompt",
                "command": ARGUMENTS_COMMANDS["with_defaults"],
            },
        },
        {
            "name": "path to atomics",
            "supported_platforms": ["linux"],
            "input_arguments": {
                "output_file": {"description": "out", "type": "path", "default": "/tmp/o"}
            },
            "executor": {
                "name": "sh",
                "command": ARGUMENTS_COMMANDS["path_to_atomics"],
            },
        },
        {
            "name": "unknown placeholder",
            "supported_platforms": ["macos"],
            "input_arguments": {
                "vbscript": {"description": "no default", "type": "string"},
                "output_file": {"description": "out", "type": "path", "default": "/tmp/o"},
            },
            "executor": {
                "name": "bash",
                "command": ARGUMENTS_COMMANDS["unknown_placeholder"],
            },
        },
        {
            "name": "no arguments",
            "supported_platforms": ["linux"],
            "executor": {
                "name": "zsh",
                "command": ARGUMENTS_COMMANDS["no_arguments_stored"],
            },
        },
    ]
    import yaml

    payload = yaml.safe_dump(
        {"attack_technique": ARGUMENTS_TECHNIQUE, "atomic_tests": tests}, sort_keys=False
    ).encode("utf-8")
    return AtomicDocument(ARGUMENTS_TECHNIQUE, payload)
