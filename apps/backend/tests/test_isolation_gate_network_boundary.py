"""Deterministic network-boundary tests for the Isolation_Gate (task 9.2).

# Feature: agentic-phase-model-refactor, Property 4: Fronteira de rede.
# ``execution`` never starts without a valid ``IsolationProof``; under isolation
# every external destination is rejected and every internal destination accepted
# (the single source of truth being ``is_internal_ip``, re-exported by the gate as
# ``validate_destination``).

Validates: Requirements 4.3, 4.4

This is a *feature* property (``agentic-phase-model-refactor`` is a feature, not a
bugfix): the property must **hold** over the Isolation_Gate as it exists today.

Two deterministic tests (no Hypothesis / no property generation), per the design's
"Property 4: Fronteira de rede → 2 testes determinísticos":

(a) ``require_isolation`` blocks the start of ``execution`` when there is no valid
    proof — a missing proof, a proof for another run, and a proof that attests
    isolation was NOT achieved all raise ``IsolationRequired`` (Req. 4.4); a valid
    proof passes and is threaded forward.
(b) Under isolation, ``validate_destination`` (reusing ``is_internal_ip``) rejects
    every external destination and accepts every internal one — internal traffic
    (Caldera ↔ targets on the Internal_Network) must stay allowed because isolation
    cuts external egress but keeps the internal network (Req. 4.3, design DD3).

No Docker daemon, network, Caldera, LLM or database is ever touched: the gate is a
pure function over already-computed inputs and the subnet constants.

_Requirements: 4.3, 4.4_
"""

from __future__ import annotations

import pytest

from app.services.containment.isolation_gate import (
    IsolationProof,
    IsolationRequired,
    require_isolation,
    validate_destination,
)
from app.services.containment.subnets import (
    INTERNAL_SUBNET_CIDRS,
    is_internal_ip,
)

_RUN_ID = "run-network-boundary"


# ---------------------------------------------------------------------------
# (a) execution is blocked without a valid IsolationProof (Req. 4.4)
# ---------------------------------------------------------------------------


def test_execution_blocked_without_valid_isolation_proof() -> None:
    """``require_isolation`` blocks ``execution`` start unless a valid proof exists.

    Covers the three ways a run can lack a valid proof — ``None`` (the gate was
    never crossed), a proof belonging to a different run, and a proof that attests
    isolation was NOT achieved (``isolated=False``) — each raising
    ``IsolationRequired`` so no offensive command can run. A valid proof
    (``isolated=True``, same run) is returned unchanged.
    _Requirements: 4.4_
    """
    # 1) No proof at all — the gate was never crossed -> blocked.
    with pytest.raises(IsolationRequired) as missing:
        require_isolation(_RUN_ID, None)
    assert missing.value.run_id == _RUN_ID

    # 2) A proof that attests isolation FAILED (external egress not cut) -> blocked.
    not_isolated = IsolationProof(
        run_id=_RUN_ID,
        isolated=False,
        reason="egresso externo não confirmadamente cortado",
    )
    assert not_isolated.is_valid is False
    with pytest.raises(IsolationRequired) as invalid:
        require_isolation(_RUN_ID, not_isolated)
    assert invalid.value.run_id == _RUN_ID

    # 3) A valid proof for a DIFFERENT run must not approve this run -> blocked.
    other_run_proof = IsolationProof(run_id="some-other-run", isolated=True)
    with pytest.raises(IsolationRequired):
        require_isolation(_RUN_ID, other_run_proof)

    # 4) A valid proof for THIS run lets ``execution`` start and is threaded back.
    valid = IsolationProof(run_id=_RUN_ID, isolated=True, targets=("caldera", "kali"))
    assert valid.is_valid is True
    approved = require_isolation(_RUN_ID, valid)
    assert approved is valid


# ---------------------------------------------------------------------------
# (b) under isolation, external destinations are rejected and internal accepted
#     (Req. 4.3) — reusing is_internal_ip via validate_destination
# ---------------------------------------------------------------------------


def test_destination_validation_rejects_external_accepts_internal() -> None:
    """Under isolation every external destination is rejected, every internal one accepted.

    ``validate_destination`` is a thin re-export of ``is_internal_ip`` (the single
    source of truth), so the gate validates command destinations without
    re-implementing the internal/external verdict. Isolation cuts *external* egress
    but preserves the Internal_Network, so internal traffic (Caldera ↔ targets)
    stays allowed while any external destination is rejected.
    _Requirements: 4.3_
    """
    # One concrete address inside each of the three internal lab subnets — all
    # accepted under isolation (the internal network stays active).
    internal_destinations = ("192.168.10.10", "192.168.20.30", "192.168.30.40")
    for destination in internal_destinations:
        assert validate_destination(destination) is True
        # The gate's verdict is exactly is_internal_ip's (same source of truth).
        assert validate_destination(destination) is is_internal_ip(destination)

    # External / non-internal destinations — all rejected under isolation: a public
    # IP, a hostname (never internal, resolves anywhere), and a private IP outside
    # the lab's internal ranges.
    external_destinations = ("8.8.8.8", "nmap.org", "10.0.0.5", "203.0.113.7")
    for destination in external_destinations:
        assert validate_destination(destination) is False
        assert validate_destination(destination) is is_internal_ip(destination)

    # Sanity: every declared internal subnet's network address is classified
    # internal, proving the gate's verdict tracks the subnet source of truth and
    # not a hard-coded list local to this test.
    for cidr in INTERNAL_SUBNET_CIDRS:
        network_address = cidr.split("/", 1)[0]
        base = network_address.rsplit(".", 1)[0]
        assert validate_destination(f"{base}.10") is True
