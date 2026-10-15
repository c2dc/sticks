"""DTOs de parâmetros de ambiente — 2ª Saída do Estágio 4 (Operacionalização).

Este módulo define os modelos tipados (Pydantic) da **segunda** Saída do Estágio
4 (Operacionalização): os :class:`ParametrosDeAmbiente`. A primeira Saída são as
Abilities ordenadas (reuso do ``Stage2*``/``CaseService``); esta é um artefato
tipado **separado**, destinado ao Estágio 5 (Criação do ambiente), e **derivado**
dos campos curados embutidos nos nós do DAG curado (``target_vuln_host_a/b``,
``network_context``, ``source_host_type``/``target_host_type``) e dos destinos
dos ``attacker_commands`` (Req. 8.3, 8.4, 8.7).

A plataforma do ambiente é, neste primeiro momento, **sempre contêiner** (Req.
17.1). O desenho admite máquina virtual futura via :class:`PlataformaAmbiente`
**sem redesenho** — o membro ``maquina_virtual`` fica como placeholder comentado
e **não** é implementado nesta spec (Req. 17.2), seguindo o precedente de
``OrigemDag`` (o enum serializa para VARCHAR+CHECK e novos valores encaixam sem
migração de esquema).

A origem (:class:`~app.models.enums.TranslationSource`) é explícita e extensível:
hoje toda a derivação curada usa ``curadoria_humana``; uma futura operacionalização
``gerado_ia`` encaixa sem alterar o modelo (Req. 16.2, 16.4).

_Requisitos: 8.3, 8.4, 17.1, 17.2_
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import PlataformaAmbiente, TranslationSource

# ``PlataformaAmbiente`` passou a viver na camada de modelos
# (:mod:`app.models.enums`) para que a persistência
# (``parametros_ambiente.plataforma``) possa referenciá-lo sem criar um ciclo de
# import entre modelos e serviços. É reexportado aqui para preservar o ponto de
# import histórico ``app.services.environment.operacionalizacao.PlataformaAmbiente``
# usado pelo restante do pacote de ambiente e pelos testes. O enum continua com
# apenas ``CONTAINER`` implementado (Req. 17.1); ``maquina_virtual`` fica fora do
# enum (Req. 17.2), admitido só no CHECK da coluna persistida.
__all__ = [
    "PlataformaAmbiente",
    "HostAlvo",
    "ServicoRequerido",
    "ParametrosDeAmbiente",
]


class HostAlvo(BaseModel):
    """Um host-alvo derivado dos comandos/nós do DAG curado (Req. 8.3).

    Attributes:
        rotulo: Rótulo lógico do host no DAG (ex.: ``"host_a"``, ``"host_b"``).
        endereco: IP interno quando resolvido (ex.: ``"192.168.20.30"``),
            derivado dos destinos dos ``attacker_commands`` via
            ``container_for_ip``; ``None`` quando não resolvido.
        container: Container do laboratório correspondente (via
            ``subnets.container_for_ip``); ``None`` quando não resolvido.
        host_type: ``source_host_type``/``target_host_type`` do nó do DAG.
        vulnerabilidades: Comandos ``target_vuln_host_*`` do nó
            correspondente a este host.
    """

    rotulo: str
    endereco: Optional[str] = None
    container: Optional[str] = None
    host_type: Optional[str] = None
    vulnerabilidades: list[str] = Field(default_factory=list)


class ServicoRequerido(BaseModel):
    """Serviço/vulnerabilidade a provisionar, inferido dos comandos (Req. 8.3).

    Attributes:
        descricao: Descrição do serviço/vulnerabilidade inferido dos comandos
            (ex.: ``"ssh root login"``, ``"flask na porta 5000"``).
        tecnica_origem: ``technique_id`` do nó do DAG que originou o serviço,
            quando disponível.
    """

    descricao: str
    tecnica_origem: Optional[str] = None


class ParametrosDeAmbiente(BaseModel):
    """Parâmetros de ambiente — 2ª Saída do Estágio 4 (Req. 8.3, 8.4, 8.7).

    Artefato tipado SEPARADO das Abilities, destinado ao Estágio 5. Derivado dos
    campos curados embutidos nos nós do DAG (``target_vuln_host_a``/``_b``,
    ``network_context``, ``source_host_type``/``target_host_type``) e dos
    destinos dos ``attacker_commands``.

    Attributes:
        caso_id: *Slug* do Caso a que estes parâmetros pertencem.
        plataforma: Plataforma do ambiente — sempre ``CONTAINER`` nesta spec
            (Req. 17.1); VM admitida sem redesenho (Req. 17.2).
        hosts: Hosts-alvo derivados dos comandos/nós do DAG.
        servicos: Serviços/vulnerabilidades inferidos dos comandos.
        network_context: Notas de rede por nó (``network_context`` do DAG).
        origem: Origem da derivação — ``curadoria_humana`` hoje; extensível a
            ``gerado_ia`` sem alterar o modelo (Req. 16.2, 16.4).
    """

    caso_id: str
    plataforma: PlataformaAmbiente = PlataformaAmbiente.CONTAINER
    hosts: list[HostAlvo] = Field(default_factory=list)
    servicos: list[ServicoRequerido] = Field(default_factory=list)
    network_context: list[str] = Field(default_factory=list)
    origem: TranslationSource = TranslationSource.HUMAN_CURATION


__all__ = [
    "PlataformaAmbiente",
    "HostAlvo",
    "ServicoRequerido",
    "ParametrosDeAmbiente",
]
