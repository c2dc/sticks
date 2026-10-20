"""Example test for the online/offline split (task 1.2).

Validates: Requirements 2.2 — ``classify_commands`` splits the **real** curated
``shadowray_suta.sh`` setup deterministically: the internet-bound steps
(``apt-get install``, ``pip3 install``) are classified ``ONLINE`` (sub-stage
5.1, egress window), while the host-local steps that need no internet
(``useradd``, writing ``backdoor.py`` via ``cat > ... << EOF``, starting
``sshd &``) are classified ``OFFLINE`` (sub-stage 5.2, isolated).

The commands below are sourced from the real
``infra/docker/.docker/nginx/shadowray_suta.sh`` so the example exercises the
heuristic against production-shaped input. The safe default (no online pattern ⇒
OFFLINE) is also checked, since a misclassification toward OFFLINE never opens
undue egress.
"""

from __future__ import annotations

from app.services.environment.target_setup import (
    ClassifiedCommand,
    SetupPhase,
    classify_commands,
)


def _phase_of(result: list[ClassifiedCommand], command: str) -> SetupPhase:
    """Return the classified phase of ``command`` in ``result`` (one-to-one order)."""
    for item in result:
        if item.command == command:
            return item.phase
    raise AssertionError(f"command not found in result: {command!r}")


def test_apt_and_pip_installs_are_online() -> None:
    """apt-get install / pip3 install ⇒ ONLINE (need the egress window, 5.1)."""
    commands = [
        "apt-get install -y python3 python3-pip sshpass curl socat wget 2>/dev/null || true",
        "apt-get install -y openssh-server 2>/dev/null || true",
        "pip3 install --break-system-packages flask flask-cors requests 2>/dev/null || true",
    ]

    result = classify_commands(commands)

    assert all(item.phase == SetupPhase.ONLINE for item in result)


def test_user_payload_and_service_steps_are_offline() -> None:
    """useradd / cat > ...backdoor.py / sshd & ⇒ OFFLINE (isolated, 5.2)."""
    useradd = "useradd -m attacker -s /bin/bash"
    backdoor = "cat > /home/attacker/malware/backdoor.py << 'EOF'"
    sshd = 'pgrep -x "sshd" > /dev/null || /usr/sbin/sshd &'

    result = classify_commands([useradd, backdoor, sshd])

    assert _phase_of(result, useradd) == SetupPhase.OFFLINE
    assert _phase_of(result, backdoor) == SetupPhase.OFFLINE
    assert _phase_of(result, sshd) == SetupPhase.OFFLINE


def test_real_shadowray_split_mixes_online_and_offline() -> None:
    """The real monolithic script splits into both phases, order preserved."""
    shadowray = [
        "apt-get install -y python3 python3-pip sshpass curl socat wget 2>/dev/null || true",
        "mkdir -p /var/run/sshd",
        "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config",
        'pgrep -x "sshd" > /dev/null || /usr/sbin/sshd &',
        "echo 'root:RootPass123' | chpasswd",
        "useradd -m attacker -s /bin/bash",
        "usermod -aG sudo attacker",
        "pip3 install --break-system-packages flask flask-cors requests 2>/dev/null || true",
        "cat > /home/attacker/malware/backdoor.py << 'EOF'",
        "chown attacker:attacker /home/attacker/malware/backdoor.py",
        "chmod +x /home/attacker/malware/backdoor.py",
        "echo 'print(\"hello\")' | base64 > /tmp/encoded_payload.b64",
    ]

    result = classify_commands(shadowray)

    # Order preserved one-to-one with the input.
    assert [item.command for item in result] == shadowray

    online = {item.command for item in result if item.phase == SetupPhase.ONLINE}
    offline = {item.command for item in result if item.phase == SetupPhase.OFFLINE}

    # Only the apt/pip installs reach for the internet.
    assert online == {
        "apt-get install -y python3 python3-pip sshpass curl socat wget 2>/dev/null || true",
        "pip3 install --break-system-packages flask flask-cors requests 2>/dev/null || true",
    }
    # Everything host-local (users, payload, service start, file ops) is offline.
    assert "useradd -m attacker -s /bin/bash" in offline
    assert "cat > /home/attacker/malware/backdoor.py << 'EOF'" in offline
    assert 'pgrep -x "sshd" > /dev/null || /usr/sbin/sshd &' in offline


def test_unmatched_command_defaults_to_offline() -> None:
    """A command matching no ONLINE pattern defaults to OFFLINE (safe default)."""
    result = classify_commands(["echo 'nothing online here'"])

    assert result[0].phase == SetupPhase.OFFLINE
