"""Verificação de saúde dos contêineres-alvo (Estágio 5) — read-only.

Este módulo implementa a etapa ``aguardar_saude`` do pipeline de provisionamento
do Estágio 5 (Criação do ambiente de simulação): **confirmar**, por inspeção
somente-leitura do Docker, que **todos** os contêineres-alvo do laboratório estão
no estado ``running`` e, quando o serviço define ``healthcheck`` no ``compose``,
com status de saúde ``healthy`` (Req. 5.1). O ``docker compose up -d --wait`` do
:class:`~app.services.environment.compose_executor.DockerComposeExecutor` já
espera a saúde pelo próprio Compose; esta verificação **confirma** o estado antes
de prosseguir e produz a linha do checklist de pré-requisitos (Req. 5.6).

Garantia read-only / sem efeitos (Req. 9.2)
-------------------------------------------
Como a verificação de isolamento (:mod:`app.services.containment.isolation`), a
verificação de saúde faz **apenas inspeção**: nunca inicia, para, cria ou
modifica contêineres ou redes. O acesso ao Docker fica atrás do seam injetável
:class:`ContainerStateInspector` — um ``@runtime_checkable Protocol`` que, por
contrato, chama somente APIs de leitura (``containers.get`` / ``.attrs``).

Reuso do seam do inspector read-only
------------------------------------
O desenho reaproveita o **mesmo padrão** do inspector read-only que
``inspect_isolation`` usa (:mod:`app.services.containment.isolation`): um
``Protocol`` injetável + um ``dataclass`` de dados simples (mockável sem daemon) +
um adaptador de produção (:class:`DockerSdkStateInspector`) construído
**preguiçosamente** apenas quando nenhum inspector é injetado — exatamente como
:class:`~app.services.containment.DockerSdkInspector`. Assim importar e
unit-testar este módulo nunca exige um daemon Docker (amigável a dev Windows), e a
inspeção de ``State``/``Health`` fica atrás da mesma costura que a de
``NetworkSettings``/``HostConfig`` da verificação de isolamento.

> O :class:`~app.services.containment.ContainerInspector` existente expõe apenas
> ``NetworkSettings`` (redes) e ``HostConfig.Dns`` — não o ``State``/``Health`` que
> a saúde exige. Em vez de inflar aquele seam de isolamento com um método alheio à
> sua responsabilidade, este módulo define um seam **focado** em ``State`` seguindo
> o mesmo padrão (``Protocol`` + ``dataclass`` + adaptador SDK lazy), mantendo cada
> inspeção read-only coesa com a sua responsabilidade.

Regra de saúde (Req. 5.1)
-------------------------
Um contêiner-alvo está **saudável** sse:

1. Está no estado ``running`` (``State.Running == true``); e
2. Quando o serviço define ``healthcheck`` (``State.Health`` presente), o status
   de saúde é ``healthy``. Quando o serviço **não** define ``healthcheck``, não há
   status de saúde a exigir — ``saudavel`` fica ``None`` e **não reprova** (o
   estado ``running`` basta).

Se **qualquer** contêiner-alvo não estiver saudável, o veredito geral reprova e o
contêiner ofensor é identificado com uma razão legível. Fail-closed: uma lista
vazia de contêineres-alvo reprova (não há nada a provar saudável), e um contêiner
ausente na Docker Engine API reprova com a razão.

_Requisitos: 5.1, 9.2_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Seam do inspector read-only de estado/saúde + carrier de dados (mockável)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerStateInfo:
    """Visão read-only do estado/saúde de **um** contêiner (Req. 5.1).

    Espelha os campos que a regra de saúde precisa de ``Container.attrs["State"]``
    do SDK ``docker`` (``State.Running`` e, quando há ``healthcheck``,
    ``State.Health.Status``). Mantido como ``dataclass`` *frozen* simples para que
    os testes o fabriquem **sem** um daemon Docker, exatamente como
    :class:`~app.services.containment.ContainerNetworkInfo` faz na verificação de
    isolamento.

    Attributes:
        name: Nome/id do contêiner inspecionado (usado em veredictos/mensagens).
        running: ``True`` sse o contêiner está no estado ``running``
            (``State.Running``).
        health: O status de saúde do contêiner quando o serviço define
            ``healthcheck`` — ``"healthy"``, ``"unhealthy"`` ou ``"starting"``
            (``State.Health.Status``). ``None`` quando o serviço **não** define
            ``healthcheck`` (não há status de saúde a exigir — Req. 5.1).
    """

    name: str
    running: bool = False
    health: Optional[str] = None


class ContainerStateNotFound(LookupError):
    """Levantada por um inspector quando o contêiner requisitado não existe."""


@runtime_checkable
class ContainerStateInspector(Protocol):
    """Superfície read-only de inspeção de estado/saúde, injetável para testes.

    Segue o mesmo padrão do :class:`~app.services.containment.ContainerInspector`
    (o seam que ``inspect_isolation`` usa): as implementações DEVEM fazer apenas
    operações de leitura (``containers.get`` / ``.attrs``) — jamais iniciar, parar,
    criar ou modificar contêineres (Req. 9.2). Os testes injetam um *fake* que
    devolve :class:`ContainerStateInfo` fabricadas (running/unhealthy/sem
    healthcheck) sem um daemon real; a produção usa o
    :class:`DockerSdkStateInspector`.
    """

    def get_container_state(self, container: str) -> ContainerStateInfo:
        """Devolve o estado/saúde read-only de ``container``.

        Raises:
            ContainerStateNotFound: se o contêiner não existe.
        """
        ...


# ---------------------------------------------------------------------------
# Modelos tipados da Saída (projeção Pydantic para os contratos JSON pt-BR)
# ---------------------------------------------------------------------------


class VereditoSaude(BaseModel):
    """Veredito de saúde de **um** contêiner-alvo (Req. 5.1).

    Modelo Pydantic serializável que compõe o :class:`ResumoSaude` e os contratos
    JSON pt-BR da API/tela, espelhando a :class:`ContainerStateInfo` com a decisão
    da regra de saúde já aplicada (``running`` + ``healthy``), de modo análogo ao
    :class:`~app.services.environment.provisionador.VereditoContainer` do
    isolamento.

    Attributes:
        container: Nome/id do contêiner inspecionado.
        rodando: ``True`` sse o contêiner está no estado ``running`` (Req. 5.1).
        saudavel: ``True`` quando o ``healthcheck`` do serviço reporta ``healthy``;
            ``False`` quando reporta ``unhealthy``/``starting``; ``None`` quando o
            serviço **não** define ``healthcheck`` (não há saúde a exigir — não
            reprova por si só, Req. 5.1).
        razao: Causa legível quando o contêiner **não** está saudável (não
            ``running`` ou ``unhealthy``); ``None`` quando aprovado.
    """

    container: str
    rodando: bool = False
    saudavel: Optional[bool] = None
    razao: Optional[str] = None

    @property
    def aprovado(self) -> bool:
        """``True`` sse o contêiner está ``running`` e (sem healthcheck **ou** ``healthy``).

        Um contêiner é saudável sse ``rodando is True`` **e** (``saudavel is None``
        — serviço sem ``healthcheck`` — **ou** ``saudavel is True``). Um
        ``saudavel is False`` (``unhealthy``/``starting``) reprova (Req. 5.1).
        """
        return self.rodando and (self.saudavel is None or self.saudavel is True)


class ResumoSaude(BaseModel):
    """Resumo tipado da saúde — veredito por contêiner + geral (Req. 5.1, 5.6).

    Projeção serializável para a Saída do Estágio 5 e os contratos JSON pt-BR,
    análoga ao :class:`~app.services.environment.provisionador.IsolationResumo` do
    isolamento. Carrega o resultado **geral** (``aprovado``) e o veredito **por
    contêiner** (``veredictos``), de modo que a tela do Estágio 5 exiba a razão de
    cada contêiner não saudável (Req. 5.6, 7.2).

    Attributes:
        aprovado: ``True`` sse **todos** os contêineres-alvo estão saudáveis
            (resultado geral pass/fail — Req. 5.1). Fail-closed: uma lista vazia de
            contêineres reprova.
        veredictos: Veredito por contêiner, na ordem em que foram verificados.
    """

    aprovado: bool = False
    veredictos: list[VereditoSaude] = Field(default_factory=list)

    @property
    def nao_saudaveis(self) -> list[VereditoSaude]:
        """Os veredictos dos contêineres que falharam na verificação de saúde."""
        return [v for v in self.veredictos if not v.aprovado]


# ---------------------------------------------------------------------------
# Lógica central de saúde (pura; opera sobre o seam do inspector)
# ---------------------------------------------------------------------------


def _classificar_container(
    container: str, inspector: ContainerStateInspector
) -> VereditoSaude:
    """Classifica um único contêiner como saudável ou não, com a razão.

    Puro com relação ao Docker: todo acesso passa pelo ``inspector``. Contêiner
    ausente é tratado como veredito não saudável (fail-closed): se não podemos
    provar que está ``running``/``healthy``, não aprovamos (Req. 5.1).

    A regra (Req. 5.1): ``running`` é exigido; quando há ``healthcheck``
    (``health`` não ``None``), exige-se ``healthy``; sem ``healthcheck``
    (``health is None``) o ``running`` basta e ``saudavel`` fica ``None``.
    """
    try:
        estado = inspector.get_container_state(container)
    except ContainerStateNotFound:
        return VereditoSaude(
            container=container,
            rodando=False,
            saudavel=None,
            razao="container não encontrado na Docker Engine API",
        )

    if not estado.running:
        return VereditoSaude(
            container=container,
            rodando=False,
            saudavel=None,
            razao="container não está no estado running",
        )

    # Está running. Sem healthcheck -> saudavel=None, não reprova (Req. 5.1).
    if estado.health is None:
        return VereditoSaude(container=container, rodando=True, saudavel=None)

    saudavel = estado.health == "healthy"
    razao = None if saudavel else f"healthcheck reportou status '{estado.health}'"
    return VereditoSaude(
        container=container,
        rodando=True,
        saudavel=saudavel,
        razao=razao,
    )


def verificar_saude(
    containers: list[str], inspector: ContainerStateInspector
) -> ResumoSaude:
    """Verifica que todos os contêineres-alvo estão saudáveis (read-only, puro).

    Ponto de entrada puro usado pelos testes e pela etapa ``aguardar_saude`` do
    provisionamento (Req. 5.1). Aprova sse **todos** os contêineres-alvo estão
    ``running`` e (sem ``healthcheck`` **ou** ``healthy``); caso contrário reprova
    e identifica o(s) contêiner(es) ofensor(es) com a razão.

    Args:
        containers: Nomes/ids dos contêineres-alvo do ambiente.
        inspector: Qualquer implementação de :class:`ContainerStateInspector` (o
            adaptador real do SDK em produção, um *fake* devolvendo estados
            fabricados nos testes).

    Returns:
        Um :class:`ResumoSaude` com o resultado geral pass/fail e os veredictos por
        contêiner. Uma lista vazia devolve ``aprovado=False`` (nada a provar
        saudável — fail-closed, em vez de aprovar vacuamente), análogo a
        :func:`~app.services.containment.verify_isolation`.
    """
    if not containers:
        return ResumoSaude(aprovado=False, veredictos=[])

    veredictos = [_classificar_container(c, inspector) for c in containers]
    aprovado = all(v.aprovado for v in veredictos)
    return ResumoSaude(aprovado=aprovado, veredictos=veredictos)


# ---------------------------------------------------------------------------
# Adaptador real do SDK docker (construído só quando nenhum inspector é injetado)
# ---------------------------------------------------------------------------


class DockerSdkStateInspector:
    """Adapta o SDK real ``docker`` à interface :class:`ContainerStateInspector`.

    READ-ONLY: usa apenas ``client.containers.get`` e lê ``.attrs`` — jamais
    inicia ou modifica nada (Req. 9.2). Segue o mesmo padrão do
    :class:`~app.services.containment.DockerSdkInspector`: o pacote ``docker`` é
    importado **preguiçosamente** dentro de ``from_env``, de modo que este módulo
    (e tudo que importa o pacote ``environment``) importe limpo numa máquina sem
    Docker, e os unit tests nunca toquem um daemon.
    """

    def __init__(self, client: object) -> None:
        """Envolve um ``docker.DockerClient`` existente (ou qualquer client de leitura)."""
        self._client = client

    @classmethod
    def from_env(cls) -> "DockerSdkStateInspector":
        """Monta um inspector a partir do ambiente Docker (uso read-only).

        Importa ``docker`` preguiçosamente; levanta ``RuntimeError`` com uma
        mensagem clara se o SDK/daemon estiver indisponível, para que os chamadores
        exponham uma falha contida em vez de um erro de import.
        """
        try:
            import docker  # import local: mantém o módulo importável sem docker
        except ImportError as exc:  # pragma: no cover - dependência declarada
            raise RuntimeError(
                "docker SDK indisponível; não é possível inspecionar a saúde"
            ) from exc
        try:
            client = docker.from_env()
        except Exception as exc:  # pragma: no cover - precisa de um daemon real
            raise RuntimeError(
                "Docker Engine API indisponível; não é possível inspecionar a saúde"
            ) from exc
        return cls(client)

    def get_container_state(self, container: str) -> ContainerStateInfo:
        """Lê ``State.Running`` e ``State.Health.Status`` do contêiner (read-only).

        ``State.Health`` só existe quando o serviço define ``healthcheck`` — na sua
        ausência, ``health`` fica ``None`` (não há saúde a exigir, Req. 5.1).
        """
        try:
            obj = self._client.containers.get(container)  # type: ignore[attr-defined]
        except Exception as exc:  # docker.errors.NotFound e afins
            raise ContainerStateNotFound(container) from exc
        attrs = getattr(obj, "attrs", {}) or {}
        state = attrs.get("State", {}) or {}
        running = bool(state.get("Running", False))
        health_obj = state.get("Health")
        health = None
        if isinstance(health_obj, dict):
            status = health_obj.get("Status")
            health = status if status else None
        return ContainerStateInfo(
            name=getattr(obj, "name", container) or container,
            running=running,
            health=health,
        )


def inspect_saude(
    containers: list[str],
    inspector: ContainerStateInspector | None = None,
) -> ResumoSaude:
    """Verifica a saúde, usando o SDK real do Docker quando nenhum inspector é dado.

    Wrapper de conveniência para a etapa ``aguardar_saude`` do provisionamento,
    análogo a :func:`~app.services.containment.inspect_isolation`: os testes
    injetam um *fake*, enquanto a produção omite o argumento e recebe um
    :class:`DockerSdkStateInspector` construído a partir do ambiente Docker
    (read-only).

    Args:
        containers: Nomes/ids dos contêineres-alvo.
        inspector: Inspector injetado opcional. Quando ``None``, um adaptador real
            do SDK é construído preguiçosamente (que levanta ``RuntimeError`` se o
            Docker estiver indisponível — nunca no import, só quando de fato
            chamado).

    Returns:
        O :class:`ResumoSaude` de :func:`verificar_saude`.
    """
    if inspector is None:
        inspector = DockerSdkStateInspector.from_env()
    return verificar_saude(containers, inspector)


__all__ = [
    "ContainerStateInfo",
    "ContainerStateNotFound",
    "ContainerStateInspector",
    "VereditoSaude",
    "ResumoSaude",
    "verificar_saude",
    "DockerSdkStateInspector",
    "inspect_saude",
]
