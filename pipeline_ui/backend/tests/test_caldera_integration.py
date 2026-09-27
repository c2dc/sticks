"""Integration/contract tests for the CalderaClient v2 methods (task 6.3).

These tests exercise the **task 6.2** surface of :class:`CalderaClient`
(abilities / adversaries / operations) against a **mocked** Caldera using
:class:`httpx.MockTransport` — *no real Caldera and no real network* are used, in
line with the spec's "no real emulation during development" constraint. They
complement (do not duplicate) the focused availability tests in
``test_caldera_availability.py`` (task 6.1).

What is verified here:

* **Contract** — the exact endpoint path + HTTP method, the ``KEY: ADMIN123``
  header, and the request payload shapes for:
    - load abilities   -> ``POST /api/v2/abilities``
    - list adversaries -> ``GET  /api/v2/adversaries``
    - create operation -> ``POST /api/v2/operations`` (atomic planner, red
      group, jitter)
    - poll operation   -> ``GET  /api/v2/operations/{id}``
* **Per-Ability status derivation** from a link ``status``:
  ``None`` -> pendente, ``-3`` -> em_execucao, ``0`` -> sucesso, other -> falha,
  plus base64 command/output decoding.
* **Timeout (Req. 4.6)** — a non-responsive Caldera (raising
  :class:`httpx.TimeoutException`) makes ``ensure_available`` raise
  :class:`CalderaUnavailable` and ``check_availability`` report ``timed_out``.
* **Non-2xx** — an error status raises :class:`CalderaApiError`.

A single :class:`httpx.MockTransport` handler routes by
``request.url.path`` + ``request.method``; each test asserts on the captured
request(s) to prove the contract.

_Requisitos: 4.2, 4.6_
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.models.enums import AbilityResultStatus
from app.services.caldera import (
    ABILITIES_PATH,
    ADVERSARIES_PATH,
    DEFAULT_GROUP,
    DEFAULT_JITTER,
    DEFAULT_PLANNER,
    OPERATIONS_PATH,
    CalderaApiError,
    CalderaClient,
    CalderaUnavailable,
)


def _b64(text: str) -> str:
    """Encode text as Caldera does for link commands / output."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class RouterHandler:
    """A MockTransport handler that routes by ``(method, path)``.

    Records every request it sees (so tests can assert on headers / body) and
    dispatches to a registered route, returning its :class:`httpx.Response`.
    Unregistered routes raise, surfacing an unexpected call clearly.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], object] = {}

    def route(self, method: str, path: str, responder) -> "RouterHandler":
        """Register a responder (an ``httpx.Response`` or a callable)."""
        self._routes[(method.upper(), path)] = responder
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method.upper(), request.url.path)
        responder = self._routes.get(key)
        if responder is None:
            raise AssertionError(f"unexpected request: {request.method} {request.url.path}")
        if callable(responder):
            return responder(request)
        return responder


def _client(handler) -> CalderaClient:
    """Build a CalderaClient backed by a MockTransport around ``handler``."""
    return CalderaClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Abilities — POST /api/v2/abilities
# ---------------------------------------------------------------------------


def test_load_ability_contract() -> None:
    ability_payload = {
        "ability_id": "abcd-1234",
        "name": "T1102 - Web Service",
        "tactic": "command-and-control",
        "technique_id": "T1102",
    }
    handler = RouterHandler().route(
        "POST", ABILITIES_PATH, httpx.Response(200, json=ability_payload)
    )

    with _client(handler) as client:
        ref = client.load_ability(ability_payload)

    # Contract: single POST to the abilities collection with the KEY header.
    assert len(handler.requests) == 1
    req = handler.requests[0]
    assert req.method == "POST"
    assert req.url.path == ABILITIES_PATH
    assert req.headers.get("KEY") == "ADMIN123"
    # Payload shape: sent as-is (matches the v2 Ability schema).
    assert json.loads(req.content.decode("utf-8")) == ability_payload

    # Typed result carries the identifying fields.
    assert ref.ability_id == "abcd-1234"
    assert ref.name == "T1102 - Web Service"
    assert ref.tactic == "command-and-control"
    assert ref.technique_id == "T1102"


def test_load_abilities_posts_each_ability() -> None:
    abilities = [
        {"ability_id": "a-1", "name": "one"},
        {"ability_id": "a-2", "name": "two"},
    ]

    def responder(request: httpx.Request) -> httpx.Response:
        # Echo back the posted ability so the ids round-trip.
        return httpx.Response(200, json=json.loads(request.content.decode("utf-8")))

    handler = RouterHandler().route("POST", ABILITIES_PATH, responder)

    with _client(handler) as client:
        refs = client.load_abilities(abilities)

    # One POST per ability (so a single failure surfaces precisely).
    assert len(handler.requests) == 2
    assert [r.ability_id for r in refs] == ["a-1", "a-2"]
    for req in handler.requests:
        assert req.method == "POST"
        assert req.url.path == ABILITIES_PATH
        assert req.headers.get("KEY") == "ADMIN123"


# ---------------------------------------------------------------------------
# Adversaries — GET /api/v2/adversaries
# ---------------------------------------------------------------------------


def test_list_adversaries_contract() -> None:
    adversaries = [
        {
            "adversary_id": "adv-1",
            "name": "APT41-DUST",
            "description": "curated",
            "atomic_ordering": ["a-1", "a-2"],
        },
        {"adversary_id": "adv-2", "name": "C0010"},
    ]
    handler = RouterHandler().route(
        "GET", ADVERSARIES_PATH, httpx.Response(200, json=adversaries)
    )

    with _client(handler) as client:
        refs = client.list_adversaries()

    assert len(handler.requests) == 1
    req = handler.requests[0]
    assert req.method == "GET"
    assert req.url.path == ADVERSARIES_PATH
    assert req.headers.get("KEY") == "ADMIN123"

    assert [r.adversary_id for r in refs] == ["adv-1", "adv-2"]
    assert refs[0].name == "APT41-DUST"
    assert refs[0].atomic_ordering == ("a-1", "a-2")


def test_adversary_exists_true_and_false() -> None:
    adversaries = [{"adversary_id": "adv-1"}, {"adversary_id": "adv-2"}]
    handler = RouterHandler().route(
        "GET", ADVERSARIES_PATH, httpx.Response(200, json=adversaries)
    )

    with _client(handler) as client:
        assert client.adversary_exists("adv-2") is True
        assert client.adversary_exists("nope") is False


# ---------------------------------------------------------------------------
# Operations — POST /api/v2/operations (create) & GET .../{id} (poll)
# ---------------------------------------------------------------------------


def test_create_operation_contract_atomic_red_jitter() -> None:
    created = {"id": "op-99", "name": "APT41-DUST run", "state": "running"}
    handler = RouterHandler().route(
        "POST", OPERATIONS_PATH, httpx.Response(200, json=created)
    )

    with _client(handler) as client:
        op = client.create_operation(name="APT41-DUST run", adversary_id="adv-1")

    assert len(handler.requests) == 1
    req = handler.requests[0]
    assert req.method == "POST"
    assert req.url.path == OPERATIONS_PATH
    assert req.headers.get("KEY") == "ADMIN123"

    # Payload shape: atomic planner, red group, "2/8" jitter, adversary by id.
    body = json.loads(req.content.decode("utf-8"))
    assert body["name"] == "APT41-DUST run"
    assert body["group"] == DEFAULT_GROUP == "red"
    assert body["jitter"] == DEFAULT_JITTER == "2/8"
    assert body["planner"] == {"id": DEFAULT_PLANNER}
    assert DEFAULT_PLANNER == "atomic"
    assert body["adversary"] == {"adversary_id": "adv-1"}

    assert op.operation_id == "op-99"
    assert op.state == "running"


def test_create_operation_merges_extra_fields() -> None:
    handler = RouterHandler().route(
        "POST", OPERATIONS_PATH, httpx.Response(200, json={"id": "op-1"})
    )

    with _client(handler) as client:
        client.create_operation(
            name="n",
            adversary_id="adv-1",
            group="red",
            jitter="4/9",
            source={"id": "src-1"},
            auto_close=True,
        )

    body = json.loads(handler.requests[0].content.decode("utf-8"))
    assert body["jitter"] == "4/9"
    assert body["source"] == {"id": "src-1"}
    assert body["auto_close"] is True


def test_poll_operation_contract_and_status_derivation() -> None:
    # A chain covering every status-derivation branch plus base64 decoding.
    op_payload = {
        "id": "op-7",
        "state": "running",
        "chain": [
            {  # None status -> pendente
                "id": "l-pending",
                "ability": {"ability_id": "ab-p"},
                "command": _b64("whoami"),
            },
            {  # -3 -> em_execucao
                "id": "l-running",
                "ability": {"ability_id": "ab-r"},
                "command": _b64("id"),
                "status": -3,
            },
            {  # 0 -> sucesso, with decoded output
                "id": "l-success",
                "ability": {"ability_id": "ab-s"},
                "command": _b64("hostname"),
                "status": 0,
                "result": _b64("victim-01\n"),
            },
            {  # other (1) -> falha
                "id": "l-failure",
                "ability": {"ability_id": "ab-f"},
                "command": _b64("cat /nope"),
                "status": 1,
            },
        ],
    }
    path = f"{OPERATIONS_PATH}/op-7"
    handler = RouterHandler().route("GET", path, httpx.Response(200, json=op_payload))

    with _client(handler) as client:
        state = client.poll_operation("op-7")

    # Contract: GET the specific operation with the KEY header.
    assert len(handler.requests) == 1
    req = handler.requests[0]
    assert req.method == "GET"
    assert req.url.path == path
    assert req.headers.get("KEY") == "ADMIN123"

    assert state.operation_id == "op-7"
    assert state.state == "running"

    by_id = {link.link_id: link for link in state.links}
    # Status derivation per link status.
    assert by_id["l-pending"].status is AbilityResultStatus.PENDING
    assert by_id["l-running"].status is AbilityResultStatus.RUNNING
    assert by_id["l-success"].status is AbilityResultStatus.SUCCESS
    assert by_id["l-failure"].status is AbilityResultStatus.FAILURE

    # base64 command / output decoding.
    assert by_id["l-pending"].command == "whoami"
    assert by_id["l-success"].command == "hostname"
    assert by_id["l-success"].output == "victim-01\n"
    # ability_id carried through from the nested ability object.
    assert by_id["l-success"].ability_id == "ab-s"


def test_get_operation_is_alias_of_poll() -> None:
    path = f"{OPERATIONS_PATH}/op-1"
    handler = RouterHandler().route(
        "GET", path, httpx.Response(200, json={"id": "op-1", "state": "finished"})
    )

    with _client(handler) as client:
        result = client.get_operation("op-1")

    assert result.operation_id == "op-1"
    assert result.state == "finished"


# ---------------------------------------------------------------------------
# Timeout (Req. 4.6) — non-responsive Caldera
# ---------------------------------------------------------------------------


def test_timeout_ensure_available_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("no response", request=request)

    with _client(handler) as client:
        with pytest.raises(CalderaUnavailable) as excinfo:
            client.ensure_available()

    # The 10s window is reported on the typed error (mapped to 503 upstream).
    assert excinfo.value.timeout == 10.0
    assert excinfo.value.url == "http://localhost:8888"


def test_timeout_check_availability_reports_timed_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("no response", request=request)

    with _client(handler) as client:
        result = client.check_availability()

    assert result.available is False
    assert result.timed_out is True
    assert result.status_code is None


def test_timeout_on_v2_method_raises_unavailable() -> None:
    # A non-responsive Caldera during a v2 call also maps to CalderaUnavailable
    # (so the pre-flight can surface 503) rather than leaking a raw httpx error.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("no response", request=request)

    with _client(handler) as client:
        with pytest.raises(CalderaUnavailable):
            client.list_adversaries()


# ---------------------------------------------------------------------------
# Non-2xx status -> CalderaApiError
# ---------------------------------------------------------------------------


def test_non_2xx_on_create_operation_raises_api_error() -> None:
    handler = RouterHandler().route(
        "POST", OPERATIONS_PATH, httpx.Response(500, text="planner not found")
    )

    with _client(handler) as client:
        with pytest.raises(CalderaApiError) as excinfo:
            client.create_operation(name="n", adversary_id="adv-1")

    assert excinfo.value.status_code == 500
    assert excinfo.value.method == "POST"
    assert excinfo.value.path == OPERATIONS_PATH
    assert "planner not found" in excinfo.value.body


def test_non_2xx_on_list_adversaries_raises_api_error() -> None:
    handler = RouterHandler().route(
        "GET", ADVERSARIES_PATH, httpx.Response(403, text="forbidden")
    )

    with _client(handler) as client:
        with pytest.raises(CalderaApiError) as excinfo:
            client.list_adversaries()

    assert excinfo.value.status_code == 403
