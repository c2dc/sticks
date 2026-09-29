"""Unit tests for the destination subnet validator (task 4.2).

These example-based tests cover the internal/external classification and the
Ability/Operation containment verdict that enforces Property 1 (no emulation
starts with an external destination). The property-based test with generated
mixed commands is task 4.3 and lives elsewhere.

The suite exercises:

1. IP membership across the three internal /24 subnets (boundaries included).
2. Hostname destinations classified EXTERNAL without any DNS resolution.
3. URL destinations classified by their reduced host/IP.
4. Ability/Operation verdicts, including the offending-command details used for
   error messages (Req. 6.2).
5. The real ShadowRay curated commands: internal-only abilities are allowed and
   the ``wget https://nmap.org/...`` ability is refused, identifying it.

_Requisitos: 6.1, 6.2_
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.containment.command_parser import (
    Destination,
    DestinationKind,
    parse_command,
)
from app.services.containment.subnet_validator import (
    INTERNAL_SUBNETS,
    DestinationClass,
    classify_destination,
    is_internal_ip,
    validate_abilities,
    validate_ability,
    validate_commands,
)


# ---------------------------------------------------------------------------
# 1. is_internal_ip — membership in the three internal /24 subnets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "172.20.0.10",  # caldera
        "172.20.0.20",  # kali
        "172.20.0.0",   # network address (boundary)
        "172.20.0.255",  # broadcast (boundary)
        "172.21.0.20",  # nginx (most common ShadowRay hop)
        "172.22.0.20",  # db
        "172.22.0.1",
    ],
)
def test_internal_ips_are_internal(ip: str) -> None:
    """IPs inside 172.20/21/22.0.0/24 are internal. _Requisitos: 6.1_"""
    assert is_internal_ip(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "172.19.0.20",   # just below the first subnet
        "172.23.0.20",   # just above the last subnet
        "172.20.1.10",   # right /24 base but wrong third octet
        "8.8.8.8",       # public DNS
        "10.0.0.5",      # different private range
        "192.168.1.1",   # different private range
        "127.0.0.1",     # loopback
        "not-an-ip",     # malformed literal
        "",              # empty
        "999.999.999.999",  # invalid octets
    ],
)
def test_external_or_invalid_ips_are_not_internal(ip: str) -> None:
    """IPs outside the internal subnets (or invalid) are not internal.
    _Requisitos: 6.1, 6.2_"""
    assert is_internal_ip(ip) is False


def test_internal_subnets_constant_is_the_three_lab_networks() -> None:
    """The named constant exposes exactly the three internal /24s (task 4.4)."""
    assert [str(net) for net in INTERNAL_SUBNETS] == [
        "172.20.0.0/24",
        "172.21.0.0/24",
        "172.22.0.0/24",
    ]


# ---------------------------------------------------------------------------
# 2/3. classify_destination — IP / URL internal, HOST external (no DNS)
# ---------------------------------------------------------------------------


def test_internal_ip_destination_is_internal() -> None:
    dest = Destination(value="172.21.0.20", kind=DestinationKind.IP, raw="172.21.0.20")
    assert classify_destination(dest) is DestinationClass.INTERNAL


def test_external_ip_destination_is_external() -> None:
    dest = Destination(value="8.8.8.8", kind=DestinationKind.IP, raw="8.8.8.8")
    assert classify_destination(dest) is DestinationClass.EXTERNAL


def test_hostname_destination_is_external_without_dns() -> None:
    """A hostname is external by definition — no DNS resolution. _Requisitos: 6.2_"""
    dest = Destination(value="nmap.org", kind=DestinationKind.HOST, raw="https://nmap.org")
    assert classify_destination(dest) is DestinationClass.EXTERNAL


def test_url_reduced_to_internal_ip_is_internal() -> None:
    """The parser reduces a URL to its host/IP; internal IP host => internal."""
    dest = Destination(
        value="172.21.0.20",
        kind=DestinationKind.URL,
        raw="http://172.21.0.20:5055/exec",
    )
    assert classify_destination(dest) is DestinationClass.INTERNAL


# ---------------------------------------------------------------------------
# 4. Ability / Operation verdicts and offending details
# ---------------------------------------------------------------------------


def test_ability_with_only_internal_commands_is_allowed() -> None:
    report = validate_commands(
        [
            "curl -X POST -F 'cmd=whoami' http://172.21.0.20:5055/exec",
            "sshpass -p Passw0rd ssh attacker@172.21.0.20 'whoami'",
            "echo 'mining crypto'",  # purely local
        ],
        ability_id="ability-internal",
        ability_name="Internal Only",
    )
    assert report.allowed is True
    assert report.refused is False
    assert report.offending_commands == []
    assert report.describe_violations() == []


def test_ability_with_external_host_is_refused_and_identified() -> None:
    """An external host refuses the Ability and names command + destination.
    _Requisitos: 6.1, 6.2_"""
    report = validate_commands(
        ["sshpass -p 'Passw0rd' ssh attacker@172.21.0.20 wget https://nmap.org/dist/nmap-7.98.tgz"],
        ability_id="8130dba3-f51c-57d2-9d49-78e9b0bb3c4b",
        ability_name="T1068 - Exploitation for Privilege Escalation",
    )
    assert report.allowed is False
    assert report.refused is True

    offending = report.offending_commands
    assert len(offending) == 1
    external_hosts = [d.value for d in offending[0].external_destinations]
    assert "nmap.org" in external_hosts

    violations = report.describe_violations()
    assert len(violations) == 1
    assert "T1068" in violations[0]
    assert "nmap.org" in violations[0]


def test_ability_with_external_ip_is_refused() -> None:
    report = validate_commands(
        ["sshpass -p x ssh attacker@203.0.113.5 'id'"],
        ability_id="ext-ip",
        ability_name="External IP",
    )
    assert report.refused is True
    assert "203.0.113.5" in report.describe_violations()[0]


def test_operation_refused_when_any_ability_has_external_destination() -> None:
    """If ANY ability has an external destination, the whole Operation is refused."""

    class _Ability:
        def __init__(self, ability_id: str, name: str, command: str) -> None:
            self.ability_id = ability_id
            self.name = name
            self.executors = [{"name": "sh", "platform": "linux", "command": command}]

    abilities = [
        _Ability("a1", "Internal A", "curl http://172.20.0.10/x"),
        _Ability("a2", "Internal B", "ssh attacker@172.21.0.20 'ls'"),
        _Ability("a3", "External", "wget https://nmap.org/dist/nmap.tgz"),
    ]

    op_report = validate_abilities(abilities)
    assert op_report.allowed is False
    assert op_report.refused is True
    refused_ids = [r.ability_id for r in op_report.refused_abilities]
    assert refused_ids == ["a3"]
    assert any("nmap.org" in line for line in op_report.describe_violations())


def test_operation_allowed_when_all_abilities_internal() -> None:
    class _Ability:
        def __init__(self, ability_id: str, command: str) -> None:
            self.ability_id = ability_id
            self.name = ability_id
            self.executors = [{"name": "sh", "platform": "linux", "command": command}]

    abilities = [
        _Ability("a1", "curl http://172.20.0.10/x"),
        _Ability("a2", "ssh attacker@172.22.0.20 'ls'"),
    ]
    op_report = validate_abilities(abilities)
    assert op_report.allowed is True
    assert op_report.refused_abilities == []


def test_ability_with_no_executors_is_trivially_allowed() -> None:
    class _Ability:
        ability_id = "empty"
        name = "Empty"
        executors: list = []

    report = validate_ability(_Ability())
    assert report.allowed is True
    assert report.commands == []


# ---------------------------------------------------------------------------
# 5. Real ShadowRay curated commands
# ---------------------------------------------------------------------------


def _load_shadowray_abilities() -> list[dict]:
    """Load the real curated ShadowRay abilities from sticks/data (read-only)."""
    here = Path(__file__).resolve()
    # tests/ -> backend/ -> pipeline_ui/ -> repo root -> sticks/data/api/...
    repo_root = here.parents[3]
    path = repo_root / "sticks" / "data" / "api" / "shadowray_dag-ability.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_shadowray_internal_ability_is_allowed() -> None:
    """The T1190 curl-to-172.21.0.20 ability is internal-only => allowed."""
    abilities = _load_shadowray_abilities()
    t1190 = next(a for a in abilities if a["technique_id"] == "T1190")
    report = validate_commands(
        [ex["command"] for ex in t1190["executors"]],
        ability_id=t1190["ability_id"],
        ability_name=t1190["name"],
    )
    assert report.allowed is True


def test_shadowray_t1068_internal_ability_is_allowed() -> None:
    """The current T1068 command targets only the contained nginx host."""
    abilities = _load_shadowray_abilities()
    t1068 = next(a for a in abilities if a["technique_id"] == "T1068")
    report = validate_commands(
        [ex["command"] for ex in t1068["executors"]],
        ability_id=t1068["ability_id"],
        ability_name=t1068["name"],
    )
    assert report.allowed is True
    assert report.describe_violations() == []


def test_shadowray_full_operation_is_contained() -> None:
    """All 11 current ShadowRay abilities stay inside the contained lab."""
    abilities = _load_shadowray_abilities()
    op_report = validate_abilities(
        [type("A", (), {"ability_id": a["ability_id"], "name": a["name"], "executors": a["executors"]})() for a in abilities]
    )
    assert op_report.allowed is True
    assert op_report.refused_abilities == []


def test_shadowray_nested_double_ssh_stays_internal() -> None:
    """The T1016 nested ssh (172.21.0.20 -> 172.22.0.20) is fully internal."""
    abilities = _load_shadowray_abilities()
    t1016 = next(a for a in abilities if a["technique_id"] == "T1016")
    parsed = parse_command(t1016["executors"][0]["command"])
    values = {d.value for d in parsed.destinations}
    assert {"172.21.0.20", "172.22.0.20"} <= values
    report = validate_commands([t1016["executors"][0]["command"]])
    assert report.allowed is True
