"""Unit tests for the Stage 4 ``AiOperationalizer`` with a faked LLM (task 7.6).

These example-based unit tests exercise :class:`AiOperationalizer`
(``app/services/ai/agents/op_agent.py``) end to end with **fakes only** — no
network, no SEPINF endpoint, no database, no real LLM. They pin the behaviour the
task asks for (Req. 2.2, 2.4, 2.7, 12.1):

1. **Fixed-JSON LLM → ``gerado_ia`` artifact.** A fake chat model whose
   ``invoke`` returns a *fixed* JSON array of command strings drives the
   ``_choose_with_llm`` path: the agent matches those strings back against the
   injected Atomic Red Team pool and proposes exactly them. The resulting
   :class:`ProducedOperationalization` carries ``origin=gerado_ia``
   (``TranslationSource.AI_GENERATED``) on both the top-level artifact and the
   embedded :class:`EnvironmentParameters`, the Abilities are populated in attack flow
   order, and the derived parameters resolve the hosts/services from the
   LLM-chosen commands (Req. 2.2, 2.4).

2. **Downstream substitutability (Req. 2.7, 12.1).** The AI-origin artifact is
   consumed by the downstream path **identically** to a curation-origin one: the
   two producers emit the *same* :class:`ProducedOperationalization` shape
   (same fields/types, same ``EnvironmentParameters`` structure), differing only
   in the ``origin`` stamp. A single origin-agnostic consumer handles both.

Isolation note: the fake atomic pool uses the lab-internal IP ``192.168.20.30``
(which resolves to the ``nginx`` container via ``container_for_ip``) so the
host-derivation path produces a concrete, resolvable :class:`TargetHost` without
any I/O. The containment parser used for the self-check is the real pure-Python
one — it touches neither the network nor a shell.

_Requirements: 2.2, 2.4, 2.7, 12.1_
"""

from __future__ import annotations

import json
from typing import Any

from app.models.enums import TranslationSource
from app.services.flow.flow_view import FlowEdgeView, FlowNodeView, FlowView
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
)
from app.services.environment.operationalizer import ProducedOperationalization
from app.services.ai.agents.op_agent import AiOperationalizer

# A lab-internal target IP that resolves to a known container
# (192.168.20.30 -> nginx), reused from the Stage-4 example suite so the
# host-derivation produces a concrete, resolvable TargetHost with no I/O.
_INTERNAL_IP = "192.168.20.30"
_INTERNAL_CONTAINER = "nginx"
_SLUG = "ai-op-faked"


# ---------------------------------------------------------------------------
# Fakes — a fixed-JSON chat model and an in-memory Atomic Red Team pool
# ---------------------------------------------------------------------------


class _FakeReply:
    """A minimal chat reply exposing ``.content`` like a LangChain message."""

    def __init__(self, content: str) -> None:
        self.content = content


class _FixedJsonChat:
    """Fake chat model whose ``invoke`` always returns a fixed JSON array.

    Mirrors the single surface the agent uses (``chat.invoke(prompt) -> reply``
    with ``reply.content`` a string). It records every prompt it is handed so a
    test can assert the agent actually consulted the LLM, and returns a fixed
    JSON array of command strings — exactly the contract ``_choose_with_llm``
    expects — so the agent proposes those commands verbatim after matching them
    back against the pool.
    """

    def __init__(self, commands: list[str]) -> None:
        self._payload = json.dumps(commands)
        self.prompts: list[str] = []

    def invoke(self, prompt: Any) -> _FakeReply:
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))
        return _FakeReply(self._payload)


def _pool_for(technique_id: str) -> list[dict]:
    """In-memory Atomic Red Team pool keyed by technique id (fake ``atomic_lookup``).

    Shapes each entry exactly like :func:`app.services.ai.tools.tool_atomic_lookup`
    (``technique_id``/``technique_name``/``ability_id``/``name``/``platform``/
    ``command``). Every command targets the lab-internal IP so the derived hosts
    resolve to a real container without touching the network.
    """
    pools: dict[str, list[dict]] = {
        "T1190": [
            {
                "technique_id": "T1190",
                "technique_name": "Exploit Public-Facing Application",
                "ability_id": "ab-T1190",
                "ability_name": "Exploit web app",
                "tactic": "initial-access",
                "name": "curl-probe",
                "platform": "linux",
                "command": f'curl "http://{_INTERNAL_IP}:5000/?cmd=id"',
            },
            {
                "technique_id": "T1190",
                "technique_name": "Exploit Public-Facing Application",
                "ability_id": "ab-T1190",
                "ability_name": "Exploit web app",
                "tactic": "initial-access",
                "name": "curl-probe-alt",
                "platform": "linux",
                "command": f'curl "http://{_INTERNAL_IP}:8080/?cmd=whoami"',
            },
        ],
        "T1059": [
            {
                "technique_id": "T1059",
                "technique_name": "Command and Scripting Interpreter",
                "ability_id": "ab-T1059",
                "ability_name": "Run script",
                "tactic": "execution",
                "name": "ssh-exec",
                "platform": "linux",
                "command": f"sshpass -p pw ssh root@{_INTERNAL_IP} id",
            },
        ],
    }
    return pools.get((technique_id or "").strip(), [])


def _flow() -> FlowView:
    """A two-node curated attack flow carrying the two techniques the pool knows."""
    return FlowView(
        campaign_id=_SLUG,
        name="AI op faked",
        curated=True,
        can_advance=True,
        nodes=[
            FlowNodeView(
                node_id="n0",
                technique_id="T1190",
                technique_name="Exploit Public-Facing Application",
                tactic="initial-access",
            ),
            FlowNodeView(
                node_id="n1",
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
            ),
        ],
        edges=[FlowEdgeView(source="n0", target="n1")],
    )


# ---------------------------------------------------------------------------
# 1. Fixed-JSON LLM -> gerado_ia ProducedOperationalization (Req. 2.2, 2.4)
# ---------------------------------------------------------------------------


def test_fixed_json_llm_produces_gerado_ia_artifact() -> None:
    """The faked LLM drives selection; the output is stamped ``gerado_ia``.

    The fake chat returns a fixed JSON array naming one command per technique;
    the agent matches those strings against the injected pool and proposes
    exactly them, with ``origin=gerado_ia`` on the artifact and the embedded
    ``EnvironmentParameters`` (Req. 2.2, 2.4).
    """
    chosen_t1190 = f'curl "http://{_INTERNAL_IP}:8080/?cmd=whoami"'
    chosen_t1059 = f"sshpass -p pw ssh root@{_INTERNAL_IP} id"
    chat = _FixedJsonChat([chosen_t1190, chosen_t1059])

    agent = AiOperationalizer(chat=chat, atomic_lookup=_pool_for)
    produced = agent.operationalize(_SLUG, _flow())

    # Origin stamping on both artifacts (Req. 2.4).
    assert agent.origin() is TranslationSource.AI_GENERATED
    assert isinstance(produced, ProducedOperationalization)
    assert produced.campaign_id == _SLUG
    assert produced.origin is TranslationSource.AI_GENERATED
    assert produced.parameters.origin is TranslationSource.AI_GENERATED

    # The LLM was actually consulted (once per operationalizable node).
    assert len(chat.prompts) == 2

    # Abilities populated in attack flow order, source_translation = gerado_ia.
    assert produced.abilities is not None
    assert produced.abilities.translation_source is TranslationSource.AI_GENERATED
    assert produced.abilities.has_abilities is True
    assert [a.technique_id for a in produced.abilities.abilities] == ["T1190", "T1059"]
    assert produced.abilities.ordering == ["ab-T1190", "ab-T1059"]

    # The LLM-chosen commands are the ones proposed (verbatim from the pool).
    commands_by_technique = {
        a.technique_id: [e.command for e in a.executors]
        for a in produced.abilities.abilities
    }
    assert commands_by_technique["T1190"] == [chosen_t1190]
    assert commands_by_technique["T1059"] == [chosen_t1059]


def test_parameters_derived_from_llm_chosen_commands() -> None:
    """Derived ``EnvironmentParameters`` resolve hosts/services from the choices.

    The internal destinations of the LLM-chosen commands resolve to the known
    container, the platform is always ``CONTAINER``, and services are inferred
    from the proposed commands (Req. 2.2).
    """
    chosen_t1190 = f'curl "http://{_INTERNAL_IP}:5000/?cmd=id"'
    chosen_t1059 = f"sshpass -p pw ssh root@{_INTERNAL_IP} id"
    chat = _FixedJsonChat([chosen_t1190, chosen_t1059])

    agent = AiOperationalizer(chat=chat, atomic_lookup=_pool_for)
    params = agent.operationalize(_SLUG, _flow()).parameters

    assert isinstance(params, EnvironmentParameters)
    assert params.campaign_id == _SLUG
    assert params.platform is EnvironmentPlatform.CONTAINER

    # The single internal destination resolves to one concrete, deduplicated host.
    assert len(params.hosts) == 1
    host = params.hosts[0]
    assert host.address == _INTERNAL_IP
    assert host.container == _INTERNAL_CONTAINER

    # Services inferred from the proposed commands (the heuristic sees SSH).
    descriptions = {s.description for s in params.services}
    assert any("SSH" in d or "ssh" in d.lower() for d in descriptions)


def test_llm_reply_outside_pool_falls_back_to_deterministic() -> None:
    """Commands the LLM invents (absent from the pool) are ignored; agent degrades.

    ``_choose_with_llm`` only proposes commands that exist in the Atomic Red Team
    source, so a reply that does not map onto the pool yields a deterministic
    fallback selection — the agent still produces a usable ``gerado_ia``
    artifact. This is the "AI only proposes pool commands" guarantee (Req. 2.2).
    """
    chat = _FixedJsonChat(["rm -rf / # not in any pool", "echo hallucinated"])

    agent = AiOperationalizer(chat=chat, atomic_lookup=_pool_for)
    produced = agent.operationalize(_SLUG, _flow())

    assert produced.origin is TranslationSource.AI_GENERATED
    assert produced.abilities is not None
    # Every proposed command comes from the pool, never the hallucinated strings.
    pool_commands = {
        e["command"] for tech in ("T1190", "T1059") for e in _pool_for(tech)
    }
    for ability in produced.abilities.abilities:
        for executor in ability.executors:
            assert executor.command in pool_commands


# ---------------------------------------------------------------------------
# 2. Downstream consumes the AI-origin artifact identically to curation
#    (structural substitutability — Req. 2.7, 12.1)
# ---------------------------------------------------------------------------


class _CurationLikeOperationalizer:
    """A minimal ``curadoria_humana`` producer with the SAME artifact shape.

    Emits a :class:`ProducedOperationalization` built from the *same* fake pool
    as the AI agent, but stamped ``curadoria_humana`` — modelling exactly what
    a curated-origin operationalization returns (the same fields/types),
    so the test can assert the downstream path consumes both identically without
    seeding curated case files on disk.
    """

    def origin(self) -> TranslationSource:
        return TranslationSource.HUMAN_CURATION

    def operationalize(self, campaign: str, flow: FlowView) -> ProducedOperationalization:
        from app.services.environment.operationalizer import _infer_services
        from app.services.containment import container_for_ip, is_internal_ip, parse_command
        from app.services.environment.operationalization import TargetHost
        from app.services.emulation.views import (
            Stage2AbilityView,
            Stage2AdversaryView,
            Stage2ExecutorView,
            Stage2Output,
        )

        ability_views: list[Stage2AbilityView] = []
        ordering: list[str] = []
        hosts: list[TargetHost] = []
        seen_addr: set[str] = set()
        services = []
        seen_s: set[tuple[str, Any]] = set()

        for node in flow.nodes:
            pool = _pool_for(node.technique_id or "")
            if not pool:
                continue
            entry = pool[0]
            ordering.append(entry["ability_id"])
            ability_views.append(
                Stage2AbilityView(
                    ability_id=entry["ability_id"],
                    name=node.technique_name,
                    technique_id=node.technique_id,
                    tactic=node.tactic,
                    description=None,
                    executors=[
                        Stage2ExecutorView(
                            name=entry["name"],
                            platform=entry["platform"],
                            command=entry["command"],
                        )
                    ],
                    translation_source=TranslationSource.HUMAN_CURATION,
                )
            )
            for dest in parse_command(entry["command"]).destinations:
                if is_internal_ip(dest.value) and dest.value not in seen_addr:
                    seen_addr.add(dest.value)
                    hosts.append(
                        TargetHost(
                            label=f"host_{chr(ord('a') + len(hosts))}",
                            address=dest.value,
                            container=container_for_ip(dest.value),
                        )
                    )
            for service in _infer_services([entry["command"]], node.technique_id):
                key = (service.description, service.origin_technique)
                if key not in seen_s:
                    seen_s.add(key)
                    services.append(service)

        adversary = Stage2AdversaryView(
            id="adv-curation",
            name="Curated adversary",
            description="curated",
            atomic_ordering=list(ordering),
            translation_source=TranslationSource.HUMAN_CURATION,
        )
        abilities = Stage2Output(
            abilities=ability_views,
            ordering=ordering,
            adversary=adversary,
            translation_source=TranslationSource.HUMAN_CURATION,
            human_curation=True,
            has_abilities=bool(ability_views),
            has_adversary=True,
        )
        return ProducedOperationalization(
            campaign_id=campaign,
            abilities=abilities,
            parameters=EnvironmentParameters(
                campaign_id=campaign,
                platform=EnvironmentPlatform.CONTAINER,
                hosts=hosts,
                services=services,
                network_context=[],
                origin=TranslationSource.HUMAN_CURATION,
            ),
            origin=TranslationSource.HUMAN_CURATION,
        )


def _consume(produced: ProducedOperationalization) -> dict[str, Any]:
    """An origin-AGNOSTIC downstream consumer of a ``ProducedOperationalization``.

    Reads only the stable, origin-independent structure the downstream pipeline
    relies on (the abilities' commands and the derived host containers/services).
    It never branches on ``produced.origin`` — the whole point of the seam — so a
    ``gerado_ia`` artifact flows through exactly as a ``curadoria_humana`` one
    (Req. 2.7, 12.1).
    """
    return {
        "campaign_id": produced.campaign_id,
        "platform": produced.parameters.platform,
        "techniques": [a.technique_id for a in (produced.abilities.abilities if produced.abilities else [])],
        "ordering": list(produced.abilities.ordering) if produced.abilities else [],
        "containers": sorted(h.container for h in produced.parameters.hosts if h.container),
        "service_descriptions": sorted(s.description for s in produced.parameters.services),
    }


def test_ai_and_curation_artifacts_are_structurally_equivalent() -> None:
    """The AI-origin artifact has the SAME shape as a curation-origin one.

    Same fields/types, same ``EnvironmentParameters`` structure, same Pydantic
    field set — the two producers differ ONLY in the ``origin`` stamp. This is
    the structural substitutability the seam promises (Req. 2.7).
    """
    chosen_t1190 = f'curl "http://{_INTERNAL_IP}:5000/?cmd=id"'
    chosen_t1059 = f"sshpass -p pw ssh root@{_INTERNAL_IP} id"
    chat = _FixedJsonChat([chosen_t1190, chosen_t1059])

    ai_produced = AiOperationalizer(chat=chat, atomic_lookup=_pool_for).operationalize(
        _SLUG, _flow()
    )
    curation_produced = _CurationLikeOperationalizer().operationalize(_SLUG, _flow())

    # Same concrete type and same set of model fields (no extra/missing fields).
    assert type(ai_produced) is type(curation_produced) is ProducedOperationalization
    assert set(ProducedOperationalization.model_fields) == set(
        type(curation_produced).model_fields
    )
    assert set(type(ai_produced.parameters).model_fields) == set(
        type(curation_produced.parameters).model_fields
    )

    # They differ only by the origin stamp.
    assert ai_produced.origin is TranslationSource.AI_GENERATED
    assert curation_produced.origin is TranslationSource.HUMAN_CURATION
    assert ai_produced.parameters.origin is TranslationSource.AI_GENERATED
    assert curation_produced.parameters.origin is TranslationSource.HUMAN_CURATION


def test_downstream_consumes_ai_and_curation_identically() -> None:
    """The origin-agnostic consumer yields the SAME result for both origins.

    Feeding the AI-origin and curation-origin artifacts (built from the same pool
    + attack flow) through the same downstream consumer produces identical,
    origin-independent output — the downstream path cannot tell them apart beyond
    the explicit ``origin`` stamp (Req. 2.7, 12.1).
    """
    chosen_t1190 = f'curl "http://{_INTERNAL_IP}:5000/?cmd=id"'
    chosen_t1059 = f"sshpass -p pw ssh root@{_INTERNAL_IP} id"
    chat = _FixedJsonChat([chosen_t1190, chosen_t1059])

    ai_produced = AiOperationalizer(chat=chat, atomic_lookup=_pool_for).operationalize(
        _SLUG, _flow()
    )
    curation_produced = _CurationLikeOperationalizer().operationalize(_SLUG, _flow())

    assert _consume(ai_produced) == _consume(curation_produced)


# ---------------------------------------------------------------------------
# 5. ``_techniques_from_state`` reads ``stix_campaign_techniques`` (Req. 5.6, 8.1)
# ---------------------------------------------------------------------------


def _techniques_session(tmp_path: Any):
    """A SQLite session with one STIX campaign and two persisted techniques."""
    from sqlalchemy.orm import Session

    from app.core.config import Settings
    from app.db.base import Base, import_models
    from app.db.session import create_db_engine
    from app.models.domain import StixCampaign, StixCampaignTechnique, StixPackage

    engine = create_db_engine(
        Settings(database_url=f"sqlite:///{(tmp_path / 'op.db').as_posix()}")
    )
    import_models()
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine, expire_on_commit=False)

    package = StixPackage(version="18.0", name="Enterprise ATT&CK v18.0")
    session.add(package)
    session.flush()
    campaign = StixCampaign(
        package_id=package.id,
        stix_id="campaign--00000000-0000-0000-0000-000000000001",
        name="Op campaign",
        curated=True,
        curated_slug=_SLUG,
    )
    session.add(campaign)
    session.flush()
    session.add_all(
        [
            StixCampaignTechnique(
                campaign_id=campaign.id,
                technique_id="T1190",
                technique_name="Exploit Public-Facing Application",
                tactic="initial-access",
            ),
            StixCampaignTechnique(
                campaign_id=campaign.id,
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
            ),
        ]
    )
    session.commit()
    return session


def test_techniques_from_state_reads_campaign_techniques(tmp_path: Any) -> None:
    from app.services.ai.agents.op_agent import _techniques_from_state

    session = _techniques_session(tmp_path)
    try:
        view = _techniques_from_state({"campaign_id": _SLUG}, session)
    finally:
        session.close()

    assert [n.technique_id for n in view.nodes] == ["T1190", "T1059"]
    assert view.edges == []
    assert view.nodes[0].tactic == "initial-access"


def test_techniques_from_state_without_session_is_empty() -> None:
    from app.services.ai.agents.op_agent import _techniques_from_state

    view = _techniques_from_state({"campaign_id": _SLUG}, None)

    assert view.nodes == []
    assert view.unavailable is True


def test_node_without_session_suppresses_operationalization() -> None:
    from app.services.ai.agents import op_agent

    delta = op_agent.node({"campaign_id": _SLUG, "artifacts": {}})

    assert delta["artifacts"]["operationalization"]["abilities"] is None
    assert delta["history"][0]["has_abilities"] is False
