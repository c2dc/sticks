"""CalderaClient — wrapper around the MITRE Caldera v2 API (task 6.1).

This module implements the **availability check** portion of the ``CalderaClient``
component described in the design (see "Components and Interfaces" ->
``CalderaClient``). Before the ``OperationRunner`` (task 7.3) may start any
Operation, it must confirm the Caldera platform is reachable at the configured
URL **within 10 seconds**; otherwise no Operation is started and the caller maps
the failure to HTTP 503 (Requisito 4.6).

Scope of this task (6.1)
------------------------
Only the client scaffolding and the availability probe live here:

* base URL (reused from :mod:`app.core.config`, single source of truth),
* the ``KEY`` header (reused from config),
* an **injectable** ``httpx`` client / transport so tests (task 6.3) can simulate
  both a healthy response and a 10s timeout **without a real Caldera** (using
  :class:`httpx.MockTransport` or an injected :class:`httpx.Client`),
* :meth:`CalderaClient.check_availability` returning a typed result, and
* :meth:`CalderaClient.ensure_available` raising the typed
  :class:`CalderaUnavailable` error the API layer maps to 503.

The abilities / adversaries / operations methods are **task 6.2**. This module
is structured so 6.2 extends :class:`CalderaClient` cleanly: the shared
``httpx.Client`` (with its base URL, headers and 10s timeout) and the
``_request`` helper are reused by those future methods.

Design constraints honoured
----------------------------
* **No real network calls** happen on import or in tests — the client only calls
  out when a method is invoked, and tests inject an :class:`httpx.MockTransport`.
* **10s timeout is explicit** — :data:`DEFAULT_TIMEOUT_SECONDS` is applied as an
  ``httpx.Timeout`` on the client, and a timeout signals *unavailable* rather
  than raising an uncaught error.
* Reuses ``sticks`` config values via :mod:`app.core.config` — nothing under
  ``sticks/`` is modified.

_Requisitos: 4.6_
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from app.core.config import Settings, get_settings
from app.models.enums import AbilityResultStatus

# Availability timeout mandated by Requisito 4.6: if Caldera does not respond
# within this window, the environment is treated as unavailable and no Operation
# is started.
DEFAULT_TIMEOUT_SECONDS: float = 10.0

# Lightweight, read-only probe endpoint on the Caldera v2 API. It is authenticated
# with the same ``KEY`` header the rest of the pipeline uses. Kept as a module
# constant (overridable per call) so task 6.2 can reuse the same client for the
# richer endpoints without changing the availability contract.
HEALTH_PATH: str = "/api/v2/health"

# v2 API resource paths (task 6.2). Faithful to the design's ``CalderaClient``
# section: abilities via ``POST /api/v2/abilities``, adversaries via
# ``GET /api/v2/adversaries`` and operations via ``POST/GET /api/v2/operations``.
ABILITIES_PATH: str = "/api/v2/abilities"
ADVERSARIES_PATH: str = "/api/v2/adversaries"
OPERATIONS_PATH: str = "/api/v2/operations"

# Operation conventions matching the existing pipeline (``sticks/lib/operation.py``):
# the atomic planner, the "red" group and a "2/8" jitter. Kept as module
# constants (overridable per call) so callers stay compatible with the curated
# runs while the OperationRunner (task 7) can tweak them if needed.
DEFAULT_PLANNER: str = "atomic"
DEFAULT_GROUP: str = "red"
DEFAULT_JITTER: str = "2/8"


class CalderaUnavailable(RuntimeError):
    """Typed error signalling Caldera did not respond in time / is unreachable.

    The API layer (task 11.2) maps this to **HTTP 503** and the UI shows an
    "Ambiente_Docker não está disponível" message (Req. 4.6). Carries the base
    URL and the timeout that was applied so the message can be precise.
    """

    def __init__(self, url: str, timeout: float, reason: str) -> None:
        self.url = url
        self.timeout = timeout
        self.reason = reason
        super().__init__(
            f"Caldera indisponível em {url} (timeout {timeout:g}s): {reason}"
        )


class CalderaApiError(RuntimeError):
    """Typed error for a non-success HTTP status from a Caldera v2 endpoint.

    Distinct from :class:`CalderaUnavailable` (which is timeout / unreachable):
    here Caldera *did* respond, but with an error status (e.g. 4xx/5xx). Carries
    the request that failed and the response body for precise diagnostics.
    """

    def __init__(self, method: str, path: str, status_code: int, body: str) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Caldera respondeu HTTP {status_code} para {method} {path}: {body[:200]}"
        )


@dataclass(frozen=True)
class AvailabilityResult:
    """Typed result of an availability probe (Req. 4.6).

    Attributes:
        available: True iff Caldera answered in time with a success status.
        url: The base URL that was probed (for messages / logging).
        status_code: The HTTP status returned, or ``None`` when no response was
            received (timeout / connection error).
        reason: Human-readable explanation. Empty string when ``available``.
        timed_out: True when the failure was specifically a 10s timeout, as
            opposed to another connection error.
    """

    available: bool
    url: str
    status_code: int | None = None
    reason: str = ""
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Typed results for task 6.2 (abilities / adversaries / operations).
#
# These mirror the shapes the Caldera v2 API returns while exposing only the
# fields the pipeline needs. Keeping them as frozen dataclasses (rather than raw
# dicts) gives the OperationRunner (task 7) and the API layer (task 11) a stable,
# typed contract independent of Caldera's full payloads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbilityRef:
    """A loaded/created Ability as returned by ``POST /api/v2/abilities``.

    Attributes:
        ability_id: The Ability's UUID (``ability_id`` in the v2 payload).
        name: Human-readable name (e.g. ``"T1102 - Web Service"``), if present.
        tactic: MITRE ATT&CK tactic, if present.
        technique_id: ATT&CK technique id (``technique_id``), if present.
        raw: The full decoded payload, for callers needing more fields.
    """

    ability_id: str
    name: str | None = None
    tactic: str | None = None
    technique_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AbilityRef":
        return cls(
            ability_id=str(payload.get("ability_id", "")),
            name=payload.get("name"),
            tactic=payload.get("tactic"),
            technique_id=payload.get("technique_id"),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class AdversaryRef:
    """An Adversary profile as returned by ``GET /api/v2/adversaries``.

    Attributes:
        adversary_id: The Adversary's UUID (``adversary_id`` in the v2 payload).
        name: Human-readable name, if present.
        description: Free-text description, if present.
        atomic_ordering: Ordered list of ``ability_id``s, if present.
        raw: The full decoded payload.
    """

    adversary_id: str
    name: str | None = None
    description: str | None = None
    atomic_ordering: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AdversaryRef":
        ordering = payload.get("atomic_ordering") or []
        return cls(
            adversary_id=str(payload.get("adversary_id", "")),
            name=payload.get("name"),
            description=payload.get("description"),
            atomic_ordering=tuple(str(a) for a in ordering),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class CreatedOperation:
    """The Operation created by ``POST /api/v2/operations``.

    Attributes:
        operation_id: The Caldera operation id (``id`` in the v2 payload) used to
            poll state via ``GET /api/v2/operations/{id}``.
        name: The operation name.
        state: The operation's Caldera state string (e.g. ``"running"``), if any.
        raw: The full decoded payload.
    """

    operation_id: str
    name: str | None = None
    state: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CreatedOperation":
        return cls(
            operation_id=str(payload.get("id", "")),
            name=payload.get("name"),
            state=payload.get("state"),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class OperationLink:
    """A single link (per-Ability execution) inside an Operation's ``chain``.

    A Caldera *link* is one command run for one Ability on one agent. The
    OperationRunner (task 7) derives the per-Ability status and command output
    from these. The mapping to :class:`AbilityResultStatus`:

    * no ``status`` yet / not collected  => ``PENDING`` ("pendente")
    * ``status`` is the "in-flight" sentinel (-3) => ``RUNNING`` ("em_execucao")
    * ``status == 0`` => ``SUCCESS`` ("sucesso")
    * any other finished ``status`` (non-zero) => ``FAILURE`` ("falha")

    Attributes:
        link_id: The link's id/UUID.
        ability_id: The Ability this link executed.
        command: The concrete command (decoded from base64 when needed).
        paw: The agent identifier the command ran on, if present.
        status_code: The raw Caldera link status (``None`` when not yet run).
        status: The derived :class:`AbilityResultStatus`.
        output: The command's decoded stdout/stderr, if collected.
        raw: The full decoded link payload.
    """

    link_id: str
    ability_id: str | None
    command: str | None
    paw: str | None
    status_code: int | None
    status: AbilityResultStatus
    output: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "OperationLink":
        ability = payload.get("ability") or {}
        ability_id = ability.get("ability_id") if isinstance(ability, Mapping) else None
        status_code = payload.get("status")
        return cls(
            link_id=str(payload.get("id", "")),
            ability_id=ability_id,
            command=_maybe_b64_decode(payload.get("command")),
            paw=payload.get("paw"),
            status_code=status_code,
            status=_derive_link_status(status_code),
            output=_extract_link_output(payload),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class OperationStateResult:
    """A poll of an Operation's state plus its per-Ability links (Req. 4.3).

    Attributes:
        operation_id: The polled operation id.
        state: The Caldera operation state string (e.g. ``"running"``,
            ``"finished"``), if present.
        links: The links (per-Ability executions) discovered so far.
        raw: The full decoded operation payload.
    """

    operation_id: str
    state: str | None
    links: tuple[OperationLink, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "OperationStateResult":
        chain = payload.get("chain") or payload.get("links") or []
        links = tuple(
            OperationLink.from_payload(link)
            for link in chain
            if isinstance(link, Mapping)
        )
        return cls(
            operation_id=str(payload.get("id", "")),
            state=payload.get("state"),
            links=links,
            raw=dict(payload),
        )


# Caldera link status sentinels. A running (uncollected) link uses -3; a
# successfully finished command uses 0; any other finished status is a failure.
_LINK_STATUS_RUNNING = -3


def _derive_link_status(status_code: int | None) -> AbilityResultStatus:
    """Map a raw Caldera link status to a per-Ability :class:`AbilityResultStatus`."""
    if status_code is None:
        return AbilityResultStatus.PENDING
    if status_code == _LINK_STATUS_RUNNING:
        return AbilityResultStatus.RUNNING
    if status_code == 0:
        return AbilityResultStatus.SUCCESS
    return AbilityResultStatus.FAILURE


def _maybe_b64_decode(value: Any) -> str | None:
    """Decode a base64 string when possible, else return the value as text.

    Caldera encodes link commands (and result stdout) as base64. This tolerates
    already-plain values so tests can pass readable strings.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return value
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return value


def _extract_link_output(payload: Mapping[str, Any]) -> str | None:
    """Extract decoded command output from a link payload, if collected.

    The v2 API nests result data under ``result`` (base64 stdout/stderr). This is
    resilient to the several shapes Caldera has used (a bare string, or a dict
    with ``stdout``/``stderr``/``output``).
    """
    result = payload.get("result")
    if result is None:
        return None
    if isinstance(result, str):
        return _maybe_b64_decode(result)
    if isinstance(result, Mapping):
        for key in ("output", "stdout", "stderr"):
            if result.get(key):
                return _maybe_b64_decode(result[key])
    return None


class CalderaClient:
    """Wrapper around the Caldera v2 API with an injectable ``httpx`` client.

    Task 6.1 implements availability checking; task 6.2 adds ability/adversary
    loading and operation create/poll on top of the same client.

    The client owns (or borrows) a single :class:`httpx.Client` configured with:

    * ``base_url`` = the Caldera URL from config (single source of truth),
    * default headers containing the ``KEY`` header, and
    * an explicit **10s** :class:`httpx.Timeout`.

    Injection for tests (task 6.3)
    ------------------------------
    Pass either a fully built ``client=`` (e.g. one using
    :class:`httpx.MockTransport`) or a ``transport=`` and the client is built for
    you with the correct base URL, headers and timeout. When neither is given, a
    real client is created lazily on first use — so importing this module never
    opens a socket.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Build a CalderaClient.

        Args:
            settings: Backend settings; defaults to the cached
                :func:`app.core.config.get_settings`. Provides ``caldera_url``
                and ``caldera_headers`` (reused from sticks config).
            client: An optional pre-built :class:`httpx.Client`. When given it is
                used as-is (tests pass one backed by :class:`httpx.MockTransport`).
                The caller owns its lifecycle in that case.
            transport: An optional :class:`httpx.BaseTransport` (e.g.
                :class:`httpx.MockTransport`) used to build an internally-owned
                client with the correct base URL, headers and timeout.
            timeout: Timeout in seconds applied to requests. Defaults to the
                Req. 4.6 mandated **10s**; overridable for tests only.

        Raises:
            ValueError: if both ``client`` and ``transport`` are supplied.
        """
        if client is not None and transport is not None:
            raise ValueError(
                "forneça 'client' OU 'transport', não ambos"
            )

        self._settings = settings or get_settings()
        self._timeout_seconds = float(timeout)
        self._transport = transport
        # Whether we own the client's lifecycle (built internally) or it was
        # injected by the caller (who owns it).
        self._owns_client = client is None
        self._client = client

    # -- configuration surface (reused by task 6.2) --------------------------

    @property
    def base_url(self) -> str:
        """Caldera base URL (reused from config), without trailing slash."""
        return self._settings.caldera_url.rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        """Default request headers, including the ``KEY`` header (from config)."""
        return dict(self._settings.caldera_headers)

    @property
    def timeout(self) -> httpx.Timeout:
        """The explicit :class:`httpx.Timeout` applied to every request (10s)."""
        return httpx.Timeout(self._timeout_seconds)

    @property
    def timeout_seconds(self) -> float:
        """The configured timeout in seconds (10.0 per Req. 4.6 by default)."""
        return self._timeout_seconds

    # -- client lifecycle ----------------------------------------------------

    def _get_client(self) -> httpx.Client:
        """Return the underlying client, building one lazily when needed.

        A real (or transport-backed) client is only created on first use, so
        importing this module never opens a socket. The built client carries the
        base URL, the ``KEY`` header and the explicit 10s timeout so task 6.2's
        methods inherit the same contract.
        """
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=self.headers,
                timeout=self.timeout,
                transport=self._transport,
            )
        return self._client

    def close(self) -> None:
        """Close the underlying client if this instance owns it.

        Injected clients are left untouched — their owner is responsible for
        closing them.
        """
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "CalderaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- shared request helper (reused by task 6.2) --------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue a request through the shared client with the 10s timeout.

        Centralises HTTP access so the ability/adversary/operation methods reuse
        the same base URL, ``KEY`` header and explicit 10s timeout. Optionally
        sends a JSON body and/or query params (task 6.2); the timeout behaviour
        from task 6.1 is preserved on every call.

        Args:
            method: HTTP verb (``GET``, ``POST``, ...).
            path: Path on the Caldera v2 API (base URL comes from the client).
            json: Optional JSON-serialisable body (``httpx`` sets the
                ``Content-Type: application/json`` header when present).
            params: Optional query-string parameters.

        Returns:
            The raw :class:`httpx.Response` (callers decode / raise as needed).
        """
        client = self._get_client()
        return client.request(
            method, path, json=json, params=params, timeout=self.timeout
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Issue a request and return the decoded JSON body, raising on errors.

        Wraps :meth:`_request` with error handling suited to the ability /
        adversary / operation methods: a timeout / connection error becomes a
        typed :class:`CalderaUnavailable` (so the pre-flight can map it to 503),
        and a non-success HTTP status becomes a :class:`CalderaApiError`.

        Args:
            method: HTTP verb.
            path: Path on the Caldera v2 API.
            json: Optional JSON body.
            params: Optional query params.

        Returns:
            The decoded JSON (``list``/``dict``), or ``None`` for an empty body.

        Raises:
            CalderaUnavailable: on timeout / connection failure.
            CalderaApiError: on a non-2xx HTTP status.
        """
        try:
            response = self._request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise CalderaUnavailable(
                url=self.base_url,
                timeout=self._timeout_seconds,
                reason=f"sem resposta em {self._timeout_seconds:g}s ({exc!s})",
            ) from exc
        except httpx.HTTPError as exc:
            raise CalderaUnavailable(
                url=self.base_url,
                timeout=self._timeout_seconds,
                reason=f"falha de conexão: {exc!s}",
            ) from exc

        if not response.is_success:
            body = response.text
            raise CalderaApiError(
                method=method,
                path=path,
                status_code=response.status_code,
                body=body,
            )

        if not response.content:
            return None
        return response.json()

    # -- availability check (task 6.1 core) ----------------------------------

    def check_availability(self, path: str = HEALTH_PATH) -> AvailabilityResult:
        """Probe Caldera and return a typed :class:`AvailabilityResult`.

        A timely success response (2xx) => available. A timeout within the 10s
        window, a connection error, or a non-success status => unavailable. This
        method never raises for the unavailable case — it reports it — so callers
        that prefer a result over an exception can branch on it. Use
        :meth:`ensure_available` when a raised :class:`CalderaUnavailable` (and a
        503 mapping) is preferred.

        Args:
            path: The probe path on the Caldera v2 API. Defaults to
                :data:`HEALTH_PATH`.

        Returns:
            An :class:`AvailabilityResult` describing the outcome.
        """
        url = self.base_url
        try:
            response = self._request("GET", path)
        except httpx.TimeoutException as exc:
            return AvailabilityResult(
                available=False,
                url=url,
                reason=f"sem resposta em {self._timeout_seconds:g}s ({exc!s})",
                timed_out=True,
            )
        except httpx.HTTPError as exc:
            # Connection refused, DNS failure, etc. — Caldera is not reachable.
            return AvailabilityResult(
                available=False,
                url=url,
                reason=f"falha de conexão: {exc!s}",
            )

        if response.is_success:
            return AvailabilityResult(
                available=True,
                url=url,
                status_code=response.status_code,
            )
        return AvailabilityResult(
            available=False,
            url=url,
            status_code=response.status_code,
            reason=f"resposta HTTP {response.status_code}",
        )

    def ensure_available(self, path: str = HEALTH_PATH) -> AvailabilityResult:
        """Assert Caldera is available, raising :class:`CalderaUnavailable` if not.

        Convenience wrapper used by the pre-flight (task 7.3) and the emulation
        endpoint (task 11.2), which map the exception to **HTTP 503** (Req. 4.6).

        Args:
            path: The probe path; defaults to :data:`HEALTH_PATH`.

        Returns:
            The successful :class:`AvailabilityResult` when Caldera is available.

        Raises:
            CalderaUnavailable: when Caldera does not respond in time / is
                unreachable / returns a non-success status.
        """
        result = self.check_availability(path)
        if not result.available:
            raise CalderaUnavailable(
                url=result.url,
                timeout=self._timeout_seconds,
                reason=result.reason,
            )
        return result
    # -- abilities (task 6.2) ------------------------------------------------

    def load_ability(self, ability: Mapping[str, Any]) -> AbilityRef:
        """Create/load a single curated Ability via ``POST /api/v2/abilities``.

        The curated Abilities come from ``data/api/{caso}_dag-ability.json`` and
        are pushed into Caldera before an Operation runs (Req. 4.2). The payload
        is sent as-is (its shape already matches the v2 Ability schema) and the
        created Ability is returned as a typed :class:`AbilityRef`.

        Args:
            ability: The Ability payload (v2 schema) to create/load.

        Returns:
            The created Ability as an :class:`AbilityRef`.

        Raises:
            CalderaUnavailable: on timeout / connection failure.
            CalderaApiError: on a non-2xx HTTP status.
        """
        payload = self._request_json("POST", ABILITIES_PATH, json=dict(ability))
        return AbilityRef.from_payload(payload or {})

    def load_abilities(
        self, abilities: list[Mapping[str, Any]]
    ) -> list[AbilityRef]:
        """Create/load a list of curated Abilities (convenience over :meth:`load_ability`).

        Each Ability is loaded with its own ``POST /api/v2/abilities`` request so
        one failure surfaces precisely (via :class:`CalderaApiError`) rather than
        being hidden inside a batch.
        """
        return [self.load_ability(ability) for ability in abilities]

    # -- adversaries (task 6.2) ----------------------------------------------

    def list_adversaries(self) -> list[AdversaryRef]:
        """List Adversary profiles via ``GET /api/v2/adversaries`` (Req. 4.1/4.2).

        Returns:
            The Adversaries as typed :class:`AdversaryRef` values.

        Raises:
            CalderaUnavailable: on timeout / connection failure.
            CalderaApiError: on a non-2xx HTTP status.
        """
        payload = self._request_json("GET", ADVERSARIES_PATH)
        items = payload or []
        return [
            AdversaryRef.from_payload(item)
            for item in items
            if isinstance(item, Mapping)
        ]

    def adversary_exists(self, adversary_id: str) -> bool:
        """Return True iff an Adversary with ``adversary_id`` exists.

        Mirrors the ``adversary_exists`` check in ``sticks/lib/operation.py`` but
        over the v2 API and returning a bool rather than exiting the process.
        """
        return any(
            adv.adversary_id == adversary_id for adv in self.list_adversaries()
        )

    # -- operations (task 6.2) -----------------------------------------------

    def create_operation(
        self,
        name: str,
        adversary_id: str,
        *,
        group: str = DEFAULT_GROUP,
        planner: str = DEFAULT_PLANNER,
        jitter: str = DEFAULT_JITTER,
        **extra: Any,
    ) -> CreatedOperation:
        """Create an Operation via ``POST /api/v2/operations`` (Req. 4.2).

        Compatible with the existing pipeline's conventions
        (``sticks/lib/operation.py``): the **atomic** planner, the **red** group
        and a ``"2/8"`` jitter by default. Unlike the old code — which used the
        deprecated v1 ``/api/rest`` endpoint — this posts to the v2 operations
        collection per the design.

        The v2 payload references the planner/adversary/source by object id under
        the keys Caldera expects (``adversary``/``planner``/``source`` carry
        ``{"...": id}``), matching what the platform accepts on create.

        Args:
            name: The Operation name.
            adversary_id: The Adversary profile to run.
            group: Agent group to target (default ``"red"``).
            planner: Planner id (default ``"atomic"``).
            jitter: Command jitter window (default ``"2/8"``).
            **extra: Extra top-level fields merged into the payload (e.g.
                ``source``, ``auto_close``), for callers that need them.

        Returns:
            The created Operation as a :class:`CreatedOperation` (carries the id
            used to poll state).

        Raises:
            CalderaUnavailable: on timeout / connection failure.
            CalderaApiError: on a non-2xx HTTP status.
        """
        payload: dict[str, Any] = {
            "name": name,
            "group": group,
            "jitter": jitter,
            "adversary": {"adversary_id": adversary_id},
            "planner": {"id": planner},
        }
        payload.update(extra)
        result = self._request_json("POST", OPERATIONS_PATH, json=payload)
        return CreatedOperation.from_payload(result or {})

    def get_operation(self, operation_id: str) -> OperationStateResult:
        """Fetch a single Operation via ``GET /api/v2/operations/{id}``.

        Returns the state and per-Ability links so the OperationRunner (task 7)
        can derive the per-Ability status ("pendente"/"em execução"/"sucesso"/
        "falha") and the command output (Req. 4.3, 4.4).

        Args:
            operation_id: The Caldera operation id.

        Returns:
            The Operation state as an :class:`OperationStateResult`.

        Raises:
            CalderaUnavailable: on timeout / connection failure.
            CalderaApiError: on a non-2xx HTTP status.
        """
        path = f"{OPERATIONS_PATH}/{operation_id}"
        payload = self._request_json("GET", path)
        return OperationStateResult.from_payload(payload or {})

    def poll_operation(self, operation_id: str) -> OperationStateResult:
        """Alias of :meth:`get_operation` expressing polling intent.

        The OperationRunner (task 7) calls this repeatedly to track progress; it
        is a thin, well-named wrapper so the polling call-site reads clearly.
        """
        return self.get_operation(operation_id)
