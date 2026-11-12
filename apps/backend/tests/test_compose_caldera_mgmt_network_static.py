"""Static checks of the compose Caldera management-network wiring (task 25.4).

Reads ``ops/docker/docker-compose.yml`` as YAML (no Docker daemon, no network)
and asserts that the backend reaches Caldera through the dedicated
``caldera-c2`` alias on the non-internal ``caldera-mgmt-network`` without ever
joining the ``internal: true`` attack networks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "ops" / "docker" / "docker-compose.yml"

MGMT = "caldera-mgmt-network"
APP = "app-network"
INTERNAL_LAB_NETWORKS = ("caldera-kali-network", "kali-nginx-network", "nginx-db-network")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _service_networks(compose: dict, service: str) -> dict:
    nets = compose["services"][service].get("networks") or {}
    if isinstance(nets, list):
        return {name: {} for name in nets}
    return {name: (cfg or {}) for name, cfg in nets.items()}


def test_caldera_has_c2_alias_on_mgmt_network(compose: dict) -> None:
    nets = _service_networks(compose, "caldera-attacker")
    assert MGMT in nets
    assert "caldera-c2" in (nets[MGMT].get("aliases") or [])


def test_backend_joins_mgmt_and_app_networks(compose: dict) -> None:
    nets = _service_networks(compose, "sticks-backend")
    assert MGMT in nets
    assert APP in nets


def test_backend_not_on_any_internal_lab_network(compose: dict) -> None:
    nets = _service_networks(compose, "sticks-backend")
    for name in INTERNAL_LAB_NETWORKS:
        assert compose["networks"][name].get("internal") is True
        assert name not in nets
    # Defence in depth: the backend is on no internal network at all.
    for name in nets:
        assert not compose["networks"][name].get("internal", False)


def test_mgmt_network_is_not_internal(compose: dict) -> None:
    assert not compose["networks"][MGMT].get("internal", False)


def test_backend_caldera_url_default_uses_c2_alias(compose: dict) -> None:
    value = compose["services"]["sticks-backend"]["environment"]["PIPELINE_UI_CALDERA_URL"]
    match = re.fullmatch(r"\$\{PIPELINE_UI_CALDERA_URL:-(?P<default>[^}]*)\}", value)
    assert match is not None
    assert match.group("default") == "http://caldera-c2:8888"


def test_backend_no_longer_uses_host_gateway(compose: dict) -> None:
    backend = compose["services"]["sticks-backend"]
    assert "extra_hosts" not in backend
    assert "host.docker.internal" not in str(backend.get("environment"))
