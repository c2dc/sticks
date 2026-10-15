"""Property test da origem explícita e extensível — Estágio 5 / Ambiente (task 7.5).

# Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível nos
# Estágios 3, 4 e 5. Parte Estágio 5 (Criação do ambiente de simulação): a
# implementação de curadoria ``CuradoriaProvisionadorDeAmbiente`` sempre carimba
# ``curadoria_humana`` em ``AmbienteProvisionado.origem``; a costura
# ``ProvisionadorDeAmbiente`` é satisfeita estruturalmente (``@runtime_checkable``
# Protocol); e um stub ``gerado_ia`` também satisfaz a costura — provando que a
# origem ``gerado_ia`` encaixa sem redesenho (sem alterar a assinatura nem o
# artefato ``AmbienteProvisionado``).

Validates: Requirements 16.3, 16.4

Esta é uma property test de *feature* (``pipeline-seis-estagios`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual.
Foca a parte **Estágio 5** da Property 13 — a costura de criação do ambiente
(:class:`~app.services.environment.provisionador.ProvisionadorDeAmbiente` /
:class:`~app.services.environment.provisionador.CuradoriaProvisionadorDeAmbiente`).
As partes dos Estágios 3 e 4 são cobertas pelas tasks 4.4 e 5.5.

A Property 13 (lado Estágio 5) afirma, para qualquer entrada:

1. **Carimbo de curadoria** (Req. 16.3) — ``CuradoriaProvisionadorDeAmbiente.origem()``
   devolve exatamente ``TranslationSource.HUMAN_CURATION`` e **todo**
   ``AmbienteProvisionado`` que ``provisionar`` devolve carrega
   ``origem == TranslationSource.HUMAN_CURATION`` — qualquer que sejam os
   ``ParametrosDeAmbiente``, o veredito de isolamento e o estado da carga das
   Abilities.
2. **Conformidade estrutural do Protocol** (Req. 16.3) — a instância de curadoria
   satisfaz o ``@runtime_checkable`` ``ProvisionadorDeAmbiente`` por *duck typing*
   estrutural (``isinstance`` é ``True``), sem herança explícita.
3. **Extensibilidade sem redesenho** (Req. 16.3, 16.4) — um stub ``gerado_ia``
   que apenas expõe ``origem()`` + ``provisionar(params)`` + ``desprovisionar(...)``
   (sem herdar de ``ProvisionadorDeAmbiente``) também satisfaz o Protocol **sem**
   alterar a assinatura da costura nem o artefato ``AmbienteProvisionado`` —
   exatamente a extensão por origem exigida por Req. 16.3/16.4. O valor
   ``gerado_ia`` já pertence à enum ``OrigemDag`` (persistida como VARCHAR+CHECK
   que o aceita), logo persistir um artefato ``gerado_ia`` no futuro não exige
   alteração de esquema (Req. 16.4).

Estratégia / isolamento
-----------------------
Reusa os *fakes* de ``tests/test_provisionador_estagio5.py`` (``FakeCaldera``,
``FakeInspector``, o semeador de caso curado e os construtores de
``ParametrosDeAmbiente``): tudo é puro/in-memory — nenhum daemon Docker, nenhuma
rede, nenhuma IA. Cada exemplo semeia um caso curado em um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` e injeta um
``CaseService`` apontado para ele. Hypothesis varia os contêineres-alvo, o
estado de isolamento e a disponibilidade do Caldera para asserir que o carimbo
de origem **não depende** de nenhum deles. Hypothesis roda ≥100 iterações.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.enums import OrigemDag, TranslationSource
from app.services.case_service import CaseService
from app.services.environment.operacionalizacao import (
    HostAlvo,
    ParametrosDeAmbiente,
    PlataformaAmbiente,
)
from app.services.environment.provisionador import (
    AmbienteProvisionado,
    CuradoriaProvisionadorDeAmbiente,
    IsolationResumo,
    ProvisionadorDeAmbiente,
)

# Reuso dos fakes/semeadores da suíte de exemplo do Estágio 5 (sem rede, sem
# daemon Docker, sem IA) — a mesma fonte das fixtures pedidas pela task 7.5.
from tests.test_provisionador_estagio5 import (
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
# Stub ``gerado_ia`` — satisfaz ``ProvisionadorDeAmbiente`` sem herança explícita.
# Modela a origem futura (IA) que CRIA o ambiente sob medida e encaixa na costura
# por duck typing estrutural, carimbando ``gerado_ia`` (Req. 16.3, 16.4). A spec
# NÃO implementa o agente de IA (Req. 16.5); este stub existe apenas para provar
# a extensibilidade da costura.
# ---------------------------------------------------------------------------


class _StubGeradoIa:
    """Stub ``gerado_ia`` estrutural.

    Expõe a superfície completa do ``@runtime_checkable``
    :class:`ProvisionadorDeAmbiente` — ``origem``/``provisionar``/``desprovisionar``
    (costura do Estágio 5) e ``preparar`` (mantido para compat. do Estágio 4) —
    sem herdar da costura, provando a conformidade estrutural por *duck typing*.
    """

    def origem(self) -> str:
        # A implementação de IA carimba a origem ``gerado_ia`` — o mesmo valor em
        # pt-BR já aceito pela enum ``OrigemDag`` (VARCHAR+CHECK), de modo que
        # persistir um artefato ``gerado_ia`` não exige alteração de esquema
        # (Req. 16.4). Devolver a string prova que a assinatura da costura não
        # muda para a nova origem.
        return OrigemDag.GERADO_IA.value

    def provisionar(self, params: ParametrosDeAmbiente) -> AmbienteProvisionado:
        # Uma IA real CRIARIA o ambiente sob medida; o stub apenas honra a
        # assinatura e devolve o MESMO artefato ``AmbienteProvisionado``, provando
        # que a extensão por origem não redesenha a costura (Req. 16.3).
        return AmbienteProvisionado(
            caso_id=params.caso_id,
            plataforma=params.plataforma,
            containers_alvo=[h.container for h in params.hosts if h.container],
            isolamento=IsolationResumo(aprovado=True),
            abilities_carregadas=True,
        )

    def desprovisionar(self, ambiente: AmbienteProvisionado) -> None:
        _ = ambiente

    def preparar(self, caso: str):  # noqa: ANN201 - compat. Estágio 4 (não usada aqui)
        # Mantida apenas para a conformidade estrutural com a costura (o Protocol
        # declara ``preparar`` por compat. do Estágio 4). A extensibilidade do
        # Estágio 5 exercita ``provisionar``; esta operação não é usada no teste.
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — contêineres-alvo arbitrários (podem ser vazios) e os
# toggles que fazem o isolamento aprovar/reprovar e o Caldera disponível/indispon.
# A curadoria IGNORA tudo isso ao carimbar a origem; variamos para asserir que o
# carimbo é invariante.
# ---------------------------------------------------------------------------


_CONTAINER = st.sampled_from(["nginx", "flask", "kali", "caldera", "mysql"])


@st.composite
def _cenario(draw) -> tuple[list[str], bool, bool]:
    """(contêineres-alvo únicos, isolar?, caldera disponível?)."""
    containers = draw(
        st.lists(_CONTAINER, min_size=0, max_size=4, unique=True)
    )
    isolar = draw(st.booleans())
    caldera_ok = draw(st.booleans())
    return containers, isolar, caldera_ok


def _inspector_para(containers: list[str], isolar: bool) -> FakeInspector:
    """Inspector falso: isolado, ou com um contêiner anexado a um bridge externo."""
    if isolar or not containers:
        return _isolated_inspector(containers)
    insp = _isolated_inspector(containers)
    # Anexa o primeiro contêiner a uma rede externa -> não isolado (reprova).
    primeiro = containers[0]
    insp.containers[primeiro] = ContainerNetworkInfo(
        name=primeiro, network_names=(_NET, "local-network"), dns=()
    )
    return insp


# ===========================================================================
# Property 13 (Estágio 5) — para quaisquer parâmetros, isolamento e Caldera:
#   (1) a curadoria carimba curadoria_humana em origem() e em AmbienteProvisionado;
#   (2) a curadoria satisfaz o Protocol ProvisionadorDeAmbiente estruturalmente;
#   (3) um stub gerado_ia também satisfaz o Protocol, com assinatura e artefato
#       inalterados (extensível sem redesenho).
# ===========================================================================


# Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível nos
# Estágios 3, 4 e 5 (parte Estágio 5 / Ambiente).
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cenario=_cenario())
def test_property13_estagio5_origem_explicita_e_extensivel(
    cenario: tuple[list[str], bool, bool],
) -> None:
    """A origem do ambiente é explícita (curadoria_humana) e extensível (gerado_ia).

    Validates: Requirements 16.3, 16.4
    """
    containers, isolar, caldera_ok = cenario

    data_dir = Path(tempfile.mkdtemp(prefix="origem_extensivel_estagio5_"))
    try:
        (data_dir / "api").mkdir(parents=True, exist_ok=True)
        (data_dir / "dag").mkdir(parents=True, exist_ok=True)
        _seed_case(data_dir, _SLUG)

        # Fakes de compose + estado mantêm ``provisionar`` sem subprocess/daemon
        # (o pipeline da task 3.2 chama ``subir`` e ``aguardar_saude`` antes do
        # isolamento). A origem é carimbada em TODO ``AmbienteProvisionado``
        # devolvido, qualquer que seja a etapa que falhe — é o invariante aqui.
        from app.services.environment.topologia_builder import container_names

        verificaveis = containers or container_names()
        curadoria = CuradoriaProvisionadorDeAmbiente(
            inspector=_inspector_para(containers, isolar),
            case_service=CaseService(data_dir=data_dir),
            caldera=FakeCaldera(fail=not caldera_ok),
            compose_executor=FakeComposeExecutor(),
            state_inspector=_healthy_state_inspector(verificaveis),
        )

        # (1) A curadoria carimba curadoria_humana — em origem() e no
        #     AmbienteProvisionado devolvido, independentemente dos parâmetros,
        #     do veredito de isolamento e do estado do Caldera (Req. 16.3).
        assert curadoria.origem() is TranslationSource.HUMAN_CURATION
        hosts = [HostAlvo(rotulo=f"h{i}", container=c) for i, c in enumerate(containers)]
        params = ParametrosDeAmbiente(caso_id=_SLUG, hosts=hosts)
        ambiente = curadoria.provisionar(params)
        assert isinstance(ambiente, AmbienteProvisionado)
        assert ambiente.origem is TranslationSource.HUMAN_CURATION

        # (2) A curadoria satisfaz o Protocol @runtime_checkable estruturalmente,
        #     sem herança explícita (Req. 16.3).
        assert isinstance(curadoria, ProvisionadorDeAmbiente)

        # (3) Um stub gerado_ia também satisfaz o Protocol — extensibilidade sem
        #     redesenho: mesma assinatura, mesmo artefato AmbienteProvisionado,
        #     origem distinta (Req. 16.3, 16.4).
        stub = _StubGeradoIa()
        assert isinstance(stub, ProvisionadorDeAmbiente)
        assert stub.origem() == OrigemDag.GERADO_IA.value == "gerado_ia"
        ambiente_ia = stub.provisionar(params)
        assert isinstance(ambiente_ia, AmbienteProvisionado)

        # A assinatura da costura permanece centrada no único parâmetro posicional
        # ``params`` para ambas as origens — a extensão não redesenha a costura
        # (Req. 16.3). O pipeline observável da task 3.2 acrescentou um
        # ``on_log`` **keyword-only** com default no-op, de modo que os chamadores
        # existentes (``provisionar(params)``) seguem válidos sem alteração; por
        # isso o parâmetro posicional-ou-keyword continua sendo exatamente
        # ``["params"]`` e ``on_log`` é KEYWORD_ONLY com default.
        sig_curadoria = inspect.signature(curadoria.provisionar)
        posicionais = [
            nome
            for nome, p in sig_curadoria.parameters.items()
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
        assert list(inspect.signature(stub.provisionar).parameters) == ["params"]

        # ``gerado_ia`` é um valor legítimo da enum de origem persistida como
        # VARCHAR+CHECK que já o aceita — persistir um artefato gerado_ia no
        # futuro não exige alteração de esquema (Req. 16.4).
        assert OrigemDag.GERADO_IA.value == "gerado_ia"
        assert OrigemDag.GERADO_IA in set(OrigemDag)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
