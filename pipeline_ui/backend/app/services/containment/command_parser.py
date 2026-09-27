"""Command-destination parser (task 4.1).

This module is part of the ``ContainmentValidator`` security core. Its single
responsibility is to read a concrete shell command (as found in the curated
cases' ``executors[].command`` fields) and extract every **destination address**
the command would reach out to: IP addresses, URLs and hostnames appearing in
``curl``/``wget`` targets, ``ssh``/``sshpass ... user@host`` hops, ``git clone``
remotes, ``apt-get``/``pip install`` sources, and generic ``user@host`` /
``http(s)://host`` / bare-IP patterns.

Scope boundary (important):

- This module **only extracts** destinations. It deliberately does **not**
  classify a destination as internal (∈ 172.20/21/22.0.0/24) or external — that
  is task 4.2, which consumes ``CommandDestinations`` produced here.
- No network access is performed. Parsing is pure Python: ``shlex`` for
  tokenization (with a regex fallback for inputs ``shlex`` cannot split) plus a
  small set of well-scoped regexes. This keeps the parser deterministic and safe
  to run in the Windows dev environment.

Nested / compound commands:

Real ShadowRay commands nest a second ``ssh`` inside the remote payload of the
first, targeting a *different* host, e.g.::

    sshpass ... ssh attacker@172.21.0.20 '... sshpass ... ssh attacker@172.22.0.20 "..."'

The parser recursively descends into quoted argument strings so **both** the
outer host (172.21.0.20) and the inner host (172.22.0.20) are detected. It also
splits on shell operators (``&&``, ``||``, ``;``, ``|``) so each sub-command of a
compound line contributes its own destinations.

_Requisitos: 6.1, 6.2_
"""

from __future__ import annotations

import enum
import re
import shlex
from dataclasses import dataclass, field


class DestinationKind(str, enum.Enum):
    """The syntactic form a destination was extracted from.

    Kept as metadata for task 4.2 and for the preview (task 4.6). Subclasses
    ``str`` so it serializes to its value over JSON, consistent with the other
    domain enums in :mod:`app.models.enums`.
    """

    IP = "ip"          # bare IPv4 literal, e.g. 172.21.0.20
    HOST = "host"      # hostname, e.g. nmap.org or the host part of user@host
    URL = "url"        # full URL, e.g. http://172.21.0.20:5055/exec


@dataclass(frozen=True)
class Destination:
    """A single destination extracted from a command.

    Attributes:
        value: The canonical address to be classified by task 4.2. For URLs and
            ``user@host`` forms this is the **host/IP only** (scheme, port, path,
            user and password are stripped), so subnet classification can match a
            bare IP or hostname directly. ``raw`` preserves the original token.
        kind: How the destination was written in the command
            (:class:`DestinationKind`).
        raw: The original substring the destination came from (URL, ``user@host``
            token, etc.), useful for the confirmation preview and error messages.
    """

    value: str
    kind: DestinationKind
    raw: str


@dataclass(frozen=True)
class CommandDestinations:
    """Result of parsing one command.

    Attributes:
        command: The original command string, unchanged.
        destinations: Ordered, de-duplicated list of destinations detected in the
            command. Empty when the command is purely local (e.g. ``whoami``,
            ``cat /etc/passwd``, ``echo '...'``).
    """

    command: str
    destinations: list[Destination] = field(default_factory=list)

    @property
    def is_local(self) -> bool:
        """True when no destination was detected (purely local command)."""
        return not self.destinations


# --- Regex building blocks ---------------------------------------------------

# IPv4 dotted-quad. Not a strict 0-255 validator on purpose: the curated data
# uses well-formed lab IPs, and task 4.2 does the authoritative subnet check.
_IPV4 = r"(?:\d{1,3}\.){3}\d{1,3}"

# Hostname label(s) with at least one dot and a TLD-like final label, e.g.
# ``nmap.org`` or ``downloads.example.co``. Avoids matching bare filenames.
_HOSTNAME = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}"

# A URL with an explicit scheme we care about (http/https/ftp/git/ssh/scp).
_URL_RE = re.compile(
    r"\b(?P<scheme>https?|ftp|ftps|git|ssh|scp)://"
    r"(?:(?P<userinfo>[^/@\s]+)@)?"
    r"(?P<hostport>(?:\[[^\]\s]+\]|[^/:\s]+))"
    r"(?::(?P<port>\d+))?",
    re.IGNORECASE,
)

# ``user@host`` (host being an IP or hostname). Used for ssh/scp/rsync style
# targets. Anchored so it is not confused with the userinfo of a matched URL.
_USER_AT_HOST_RE = re.compile(
    rf"(?P<user>[A-Za-z0-9._-]+)@(?P<host>{_IPV4}|{_HOSTNAME})"
)

# Bare IPv4 anywhere in a token (e.g. ``http://172.21.0.20:5055`` already
# handled by the URL rule; this catches ``ping 172.21.0.20`` style bare IPs).
_BARE_IP_RE = re.compile(rf"(?<![\w.]){_IPV4}(?![\w.])")

# Shell operators that separate sub-commands on a single line.
_COMPOUND_SPLIT_RE = re.compile(r"&&|\|\||;|\|")


def _looks_quoted(token: str) -> bool:
    """Heuristic: a token that itself contains shell syntax worth recursing into.

    ``shlex`` strips the outer quotes of an argument, so a remote payload passed
    as ``ssh host '...'`` arrives here already unquoted. We recurse into any
    token that still contains command separators, another ``ssh``/``sshpass``,
    a URL scheme, or a ``user@host`` — i.e. anything that may hide a nested
    destination.
    """
    if _COMPOUND_SPLIT_RE.search(token):
        return True
    if _URL_RE.search(token) or _USER_AT_HOST_RE.search(token):
        return True
    return bool(re.search(r"\b(?:ssh|sshpass|scp|rsync)\b", token))


def _tokenize(command: str) -> list[str]:
    """Split a command into tokens, tolerating imperfect quoting.

    ``shlex.split`` with ``posix=True`` handles the mixed single/double quoting
    seen in the curated data (e.g. ``echo '$(whoami) on $(uname -n)'``). If the
    input has unbalanced quotes (which happens with deeply nested payloads),
    fall back to a whitespace-aware split so extraction still proceeds.
    """
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        # Unbalanced quotes: degrade gracefully. Strip stray quote chars and
        # split on whitespace; the regexes below still find hosts/URLs.
        return [t for t in re.split(r"\s+", command.replace("'", " ").replace('"', " ")) if t]


def _collect_from_text(text: str, sink: list[Destination]) -> None:
    """Extract URL, user@host and bare-IP destinations from a flat text span.

    Order matters: URLs first (so their host is captured with scheme context),
    then ``user@host`` (so the user prefix is stripped), then any remaining bare
    IPs not already accounted for.
    """
    consumed_spans: list[tuple[int, int]] = []

    for m in _URL_RE.finditer(text):
        hostport = m.group("hostport")
        host = hostport.strip("[]")  # drop IPv6 brackets if present
        sink.append(Destination(value=host, kind=_kind_for_host(host), raw=m.group(0)))
        consumed_spans.append(m.span())

    for m in _USER_AT_HOST_RE.finditer(text):
        # Skip if this match sits inside an already-consumed URL userinfo span.
        if any(start <= m.start() < end for start, end in consumed_spans):
            continue
        host = m.group("host")
        sink.append(Destination(value=host, kind=_kind_for_host(host), raw=m.group(0)))
        consumed_spans.append(m.span())

    for m in _BARE_IP_RE.finditer(text):
        if any(start <= m.start() < end for start, end in consumed_spans):
            continue
        ip = m.group(0)
        sink.append(Destination(value=ip, kind=DestinationKind.IP, raw=ip))


def _kind_for_host(host: str) -> DestinationKind:
    """Classify a host string as an IP literal or a hostname."""
    return DestinationKind.IP if re.fullmatch(_IPV4, host) else DestinationKind.HOST


def _walk_tokens(tokens: list[str], sink: list[Destination]) -> None:
    """Recursively collect destinations from a token list.

    For each token, extract flat destinations, then—if the token looks like it
    embeds another command (a quoted remote payload)—recurse into it. This is
    what makes the nested ``ssh ... 'ssh ... "..."'`` case yield both hosts.
    """
    for token in tokens:
        _collect_from_text(token, sink)
        if _looks_quoted(token):
            for part in _COMPOUND_SPLIT_RE.split(token):
                nested = _tokenize(part)
                # Guard against infinite recursion: only recurse when the nested
                # tokenization actually broke the token into smaller pieces.
                if nested and nested != [part]:
                    _walk_tokens(nested, sink)


def _dedupe(destinations: list[Destination]) -> list[Destination]:
    """Remove duplicate destinations, preserving first-seen order.

    De-duplication keys on ``(value, kind)`` so the same host reached via both a
    URL and a ``user@host`` is reported once per kind.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[Destination] = []
    for dest in destinations:
        key = (dest.value, dest.kind.value)
        if key not in seen:
            seen.add(key)
            unique.append(dest)
    return unique


def parse_command(command: str) -> CommandDestinations:
    """Parse a single command and return its detected destinations.

    Handles ``curl``/``wget``, ``ssh``/``sshpass ... user@host``, ``git clone``,
    ``apt-get``, ``pip install`` and generic ``user@host`` / ``http(s)://host`` /
    bare-IP patterns, including nested and compound commands. Purely local
    commands (``whoami``, ``cat``, ``echo``, ...) yield an empty destination list.

    Args:
        command: The concrete shell command string.

    Returns:
        A :class:`CommandDestinations` with the original command and the ordered,
        de-duplicated list of :class:`Destination` values.
    """
    sink: list[Destination] = []
    for part in _COMPOUND_SPLIT_RE.split(command):
        tokens = _tokenize(part)
        _walk_tokens(tokens, sink)
    return CommandDestinations(command=command, destinations=_dedupe(sink))


def extract_destinations(commands: list[str]) -> list[CommandDestinations]:
    """Parse a batch of commands.

    Convenience wrapper so task 4.2 (subnet validation) and task 4.6 (preview)
    can feed an Ability's executor commands straight through.

    Args:
        commands: Concrete command strings.

    Returns:
        One :class:`CommandDestinations` per input command, in the same order.
    """
    return [parse_command(command) for command in commands]
