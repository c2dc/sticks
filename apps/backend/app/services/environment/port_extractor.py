"""Deterministic port extraction from final operationalization commands.

Design D13 / task 16.2: the ports a technique needs are read from the final command
with a small set of regular expressions. The extractor is pure (no I/O, no AI) and
independent from :func:`app.services.containment.parse_command`, so the security
parser and its ``Destination`` type stay unchanged.

Recognized forms:

- ``scheme://host:port``
- ``IPv4:port``
- ``-p N`` / ``--port N`` / ``--port=N``
- ``port=N`` / ``port="N"``
- ``nc|netcat [options] host port``
- ``/dev/tcp/host/port``

Limitation: ports defined only inside scripts or downloaded files are not seen.
"""

from __future__ import annotations

import re

_MIN_PORT = 1
_MAX_PORT = 65535

_HOST = r"[A-Za-z0-9_.\-\[\]:]+?"

_PORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # scheme://host:port
    re.compile(
        r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]*(?:@[^\s/:]*)?[^\s/:]*:(\d{1,5})\b", re.IGNORECASE
    ),
    # IPv4:port
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:(\d{1,5})\b"),
    # -p N, --port N, --port=N (a flag must start a token)
    re.compile(r"(?:(?<=\s)|^)(?:-p|--port)(?:\s+|=)(\d{1,5})\b"),
    # port=N, port="N", port='N' (case-insensitive, e.g. Flask run(port=5000))
    re.compile(r"\bport\s*=\s*[\"']?(\d{1,5})\b", re.IGNORECASE),
    # nc|netcat [options] host port
    re.compile(
        r"\b(?:nc|ncat|netcat)\b(?:\s+-[A-Za-z0-9]+)*\s+" + _HOST + r"\s+(\d{1,5})\b",
        re.IGNORECASE,
    ),
    # /dev/tcp/host/port
    re.compile(r"/dev/(?:tcp|udp)/[^\s/]+/(\d{1,5})\b"),
)


def extract_ports(command: str | None) -> list[int]:
    """Return the sorted, de-duplicated ports referenced by ``command``.

    Values outside ``1..65535`` are discarded. An empty or ``None`` command yields
    an empty list.
    """
    if not command:
        return []
    found: set[int] = set()
    for pattern in _PORT_PATTERNS:
        for match in pattern.finditer(command):
            value = int(match.group(1))
            if _MIN_PORT <= value <= _MAX_PORT:
                found.add(value)
    return sorted(found)


__all__ = ["extract_ports"]
