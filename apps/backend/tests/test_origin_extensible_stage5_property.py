"""Property test of the explicit, extensible origin — Stage 5 / Environment (task 7.5).

# Feature: pipeline-seis-stages, Property 13: Origem explícita e extensível nodes
# Estágios 3, 4 e 5. Parte Estágio 5 (Criação do environment de simulação): a
# implementação de curation ``CurationEnvironmentProvisioner`` sempre carimba
# ``curadoria_humana`` em ``ProvisionedEnvironment.origin``; a costura
# ``EnvironmentProvisioner`` é satisfeita estruturalmente (``@runtime_checkable``
# Protocol); e um stub ``gerado_ia`` também satisfaz a costura — provando que a
# origin ``gerado_ia`` fits without redesign (without changing the signature nor the
# artefato ``ProvisionedEnvironment``).

Validates: Requirements 16.3, 16.4

Esta é uma property test de *feature* (``pipeline-seis-stages`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual.
Foca a parte **Estágio 5** da Property 13 — a costura de criação do environment
(:class:`~app.services.environment.provisioner.EnvironmentProvisioner` /
:class:`~app.services.environment.provisioner.CurationEnvironmentProvisioner`).
As partes dos Estágios 3 e 4 são cobertas pelas tasks 4.4 e 5.5.

A Property 13 (lado Estágio 5) afirma, para qualquer input:

1. **Carimbo de curation** (Req. 16.3) — ``CurationEnvironmentProvisioner.origin()``
   devolve exatamente ``TranslationSource.HUMAN_CURATION`` e **todo**
   ``ProvisionedEnvironment`` que ``provision`` devolve carrega
   ``origin == TranslationSource.HUMAN_CURATION`` — qualquer que sejam os
   ``EnvironmentParameters``, o verdict de isolation e o state da carga das
   Abilities.
2. **Conformidade estrutural do Protocol** (Req. 16.3) — a instância de curation
   satisfaz o ``@runtime_checkable`` ``EnvironmentProvisioner`` por *duck typing*
   estrutural (``isinstance`` é ``True``), sem herança explícita.
3. **Extensibility without redesign** (Req. 16.3, 16.4) — a ``gerado_ia`` stub
   que apenas expõe ``origin()`` + ``provision(params)`` + ``deprovision(...)``
   (sem herdar de ``EnvironmentProvisioner``) também satisfaz o Protocol **sem**
   alterar a assinatura da costura nem o artefato ``ProvisionedEnvironment`` —
   exatamente a extensão por origin exigida por Req. 16.3/16.4. O valor
   ``gerado_ia`` já pertence à enum ``DagOrigin`` (persistida como VARCHAR+CHECK
   que o aceita), logo persistir um artefato ``gerado_ia`` no futuro não exige
   alteração de esquema (Req. 16.4).

Estratégia / isolation
-----------------------
Reusa os *fakes* de ``tests/test_provisioner_stage5.py`` (``FakeCaldera``,
``FakeInspector``, o semeador de campaign curated e os construtores de
``EnvironmentParameters``): tudo é puro/in-memory — nenhum daemon Docker, nenhuma
rede, nenhuma IA. Cada exemplo semeia um campaign curated em um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` e injeta um
``CaseService`` apontado para ele. Hypothesis varia os contêineres-target, o
state de isolation e a disponibilidade do Caldera para asserir que o carimbo
de origin **não depende** de nenhum deles. Hypothesis roda ≥100 iterações.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.enums import DagOrigin, TranslationSource
from app.services.campaign_service import CaseService
from app.services.environment.operationalization import (
    TargetHost,
    EnvironmentParameters,
    EnvironmentPlatform,
)
from app.services.environment.provisioner import (
    ProvisionedEnvironment,
    CurationEnvironmentProvisioner,
    IsolationSummary,
    EnvironmentProvisioner,
)

# Reuso dos fakes/semeadores da suíte de exemplo do Estágio 5 (sem rede, sem
# daemon Docker, sem IA) — a mesma fonte das fixtures pedidas pela task 7.5.
from tests.test_provisioner_stage5 import (
    _NET,
    _SLUG,
    FakeCaldera,
    FakeComposeExecutor,
    FakeInspector,
    _healthy_state_inspector,
    _isolated_inspector,
    _params,
    _seed_case,
)
from app.services.containment.isolation import ContainerNetworkInfo


# ---------------------------------------------------------------------------
# Stub ``gerado_ia`` — satisfaz ``EnvironmentProvisioner`` sem herança explícita.
# Modela a origin futura (IA) que CRIA o environment sob medida e encaixa na costura
# por duck typing estrutural, carimbando ``gerado_ia`` (Req. 16.3, 16.4). A spec
# NÃO implementa o agente de IA (Req. 16.5); este stub existe apenas para provar
# a extensibilidade da costura.
# ---------------------------------------------------------------------------


class _StubGeradoIa:
    """Stub ``gerado_ia`` estrutural.

    Expõe a superfície completa do ``@runtime_checkable``
    :class:`EnvironmentProvisioner` — ``origin``/``provision``/``deprovision``
    (costura do Estágio 5) e ``prepare`` (mantido para compat. do Estágio 4) —
    sem herdar da costura, provando a conformidade estrutural por *duck typing*.
    """

    def origin(self) -> str:
        # A implementação de IA carimba a origin ``gerado_ia`` — o mesmo valor em
        # pt-BR já aceito pela enum ``DagOrigin`` (VARCHAR+CHECK), de modo que
        # persistir um artefato ``gerado_ia`` não exige alteração de esquema
        # (Req. 16.4). Devolver a string prova que a assinatura da costura não
        # muda para a nova origin.
        return DagOrigin.AI_GENERATED.value

    def provision(self, params: EnvironmentParameters) -> ProvisionedEnvironment:
        # Uma IA real CRIARIA o environment sob medida; o stub apenas honra a
        # assinatura e devolve o MESMO artefato ``ProvisionedEnvironment``, provando
        # that the origin extension does not redesign the seam (Req. 16.3).
        return ProvisionedEnvironment(
            campaign_id=params.campaign_id,
            platform=params.platform,
            target_containers=[h.container for h in params.hosts if h.container],
            isolation=IsolationSummary(approved=True),
            loaded_abilities=True,
        )

    def deprovision(self, environment: ProvisionedEnvironment) -> None:
        _ = environment

    def prepare(self, campaign: str):  # noqa: ANN201 - compat. Estágio 4 (não usada aqui)
        # Mantida apenas para a conformidade estrutural com a costura (o Protocol
        # declara ``prepare`` por compat. do Estágio 4). A extensibilidade do
        # Estágio 5 exercita ``provision``; esta operação não é usada no teste.
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — contêineres-target arbitrários (podem ser vazios) e os
# toggles que fazem o isolation aprovar/reprovar e o Caldera disponível/indispon.
# A curation IGNORA tudo isso ao carimbar a origin; variamos para asserir que o
# carimbo é invariante.
# ---------------------------------------------------------------------------


_CONTAINER = st.sampled_from(["nginx", "flask", "kali", "caldera", "mysql"])


@st.composite
def _cenario(draw) -> tuple[list[str], bool, bool]:
    """(contêineres-target únicos, isolar?, caldera disponível?)."""
    containers = draw(
        st.lists(_CONTAINER, min_size=0, max_size=4, unique=True)
    )
    isolar = draw(st.booleans())
    caldera_ok = draw(st.booleans())
    return containers, isolar, caldera_ok


def _inspector_para(containers: list[str], isolar: bool) -> FakeInspector:
    """Inspector falso: isolated, ou com um contêiner anexado a um bridge external."""
    if isolar or not containers:
        return _isolated_inspector(containers)
    insp = _isolated_inspector(containers)
    # Anexa o primeiro contêiner a uma rede externa -> não isolated (reprova).
    primeiro = containers[0]
    insp.containers[primeiro] = ContainerNetworkInfo(
        name=primeiro, network_names=(_NET, "local-network"), dns=()
    )
    return insp


# ===========================================================================
# Property 13 (Estágio 5) — para quaisquer parâmetros, isolation e Caldera:
#   (1) a curation carimba curadoria_humana em origin() e em ProvisionedEnvironment;
#   (2) a curation satisfaz o Protocol EnvironmentProvisioner estruturalmente;
#   (3) um stub gerado_ia também satisfaz o Protocol, com assinatura e artefato
#       unchanged (extensible without redesign).
# ===========================================================================


# Feature: pipeline-seis-stages, Property 13: Origem explícita e extensível nodes
# Stages 3, 4 and 5 (Stage 5 / Environment part).
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cenario=_cenario())
def test_property13_stage5_origin_explicit_and_extensible(
    cenario: tuple[list[str], bool, bool],
) -> None:
    """A origin do environment é explícita (curadoria_humana) e extensível (gerado_ia).

    Validates: Requirements 16.3, 16.4
    """
    containers, isolar, caldera_ok = cenario

    data_dir = Path(tempfile.mkdtemp(prefix="origin_extensible_stage5_"))
    try:
        (data_dir / "api").mkdir(parents=True, exist_ok=True)
        (data_dir / "dag").mkdir(parents=True, exist_ok=True)
        _seed_case(data_dir, _SLUG)

        # Fakes de compose + state mantêm ``provision`` sem subprocess/daemon
        # (o pipeline da task 3.2 chama ``subir`` e ``aguardar_saude`` antes do
        # isolation). A origin é carimbada em TODO ``ProvisionedEnvironment``
        # devolvido, qualquer que seja a step que falhe — é o invariante aqui.
        from app.services.environment.topology_builder import container_names

        verificaveis = containers or container_names()
        curation = CurationEnvironmentProvisioner(
            inspector=_inspector_para(containers, isolar),
            case_service=CaseService(data_dir=data_dir),
            caldera=FakeCaldera(fail=not caldera_ok),
            compose_executor=FakeComposeExecutor(),
            state_inspector=_healthy_state_inspector(verificaveis),
        )

        # (1) A curation carimba curadoria_humana — em origin() e no
        #     ProvisionedEnvironment devolvido, independentemente dos parâmetros,
        #     do verdict de isolation e do state do Caldera (Req. 16.3).
        assert curation.origin() is TranslationSource.HUMAN_CURATION
        hosts = [TargetHost(label=f"h{i}", container=c) for i, c in enumerate(containers)]
        params = EnvironmentParameters(campaign_id=_SLUG, hosts=hosts)
        environment = curation.provision(params)
        assert isinstance(environment, ProvisionedEnvironment)
        assert environment.origin is TranslationSource.HUMAN_CURATION

        # (2) A curation satisfaz o Protocol @runtime_checkable estruturalmente,
        #     sem herança explícita (Req. 16.3).
        assert isinstance(curation, EnvironmentProvisioner)

        # (3) Um stub gerado_ia também satisfaz o Protocol — extensibilidade sem
        #     redesign: same signature, same ProvisionedEnvironment artifact,
        #     origin distinta (Req. 16.3, 16.4).
        stub = _StubGeradoIa()
        assert isinstance(stub, EnvironmentProvisioner)
        assert stub.origin() == DagOrigin.AI_GENERATED.value == "gerado_ia"
        ai_environment = stub.provision(params)
        assert isinstance(ai_environment, ProvisionedEnvironment)

        # A assinatura da costura permanece centrada no único parâmetro posicional
        # ``params`` for both origins — the extension does not redesign the seam
        # (Req. 16.3). O pipeline observável da task 3.2 acrescentou um
        # ``on_log`` **keyword-only** com default no-op, de modo que os chamadores
        # existentes (``provision(params)``) seguem válidos sem alteração; por
        # isso o parâmetro posicional-ou-keyword continua sendo exatamente
        # ``["params"]`` e ``on_log`` é KEYWORD_ONLY com default.
        sig_curadoria = inspect.signature(curation.provision)
        posicionais = [
            name
            for name, p in sig_curadoria.parameters.items()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        assert posicionais == ["params"]
        on_log = sig_curadoria.parameters["on_log"]
        assert on_log.kind is inspect.Parameter.KEYWORD_ONLY
        assert on_log.default is not inspect.Parameter.empty
        # O stub ``gerado_ia`` honra a mesma assinatura central (``params``); ele
        # não precisa expor ``on_log`` para satisfazer a costura por duck typing.
        assert list(inspect.signature(stub.provision).parameters) == ["params"]

        # ``gerado_ia`` é um valor legítimo da enum de origin persistida como
        # VARCHAR+CHECK que já o aceita — persistir um artefato gerado_ia no
        # futuro não exige alteração de esquema (Req. 16.4).
        assert DagOrigin.AI_GENERATED.value == "gerado_ia"
        assert DagOrigin.AI_GENERATED in set(DagOrigin)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
