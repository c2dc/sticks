"""Estágio 1 — Seleção da base STIX: serviço fino de orquestração (task 2.2).

Este módulo implementa o **serviço fino** do Estágio 1 do pipeline de seis
Estágios ("Seleção da base STIX"), reusando integralmente o
:class:`~app.services.stix.stix_service.StixService` já existente. Ele **não**
reimplementa listagem, download, leitura de bundle nem dashboard: apenas
*orquestra* essas operações na ordem definida pelo Estágio 1 e deriva o veredito
de conclusão do Estágio (Req. 5.1, 5.2, 5.3, 5.4, 5.7, 5.8, 5.9, 5.10, 2.2).

Alinhamento com o design ("Backend — Estágio 1: Seleção da base STIX")
---------------------------------------------------------------------
A orquestração do Estágio 1 (serviço fino, novo, reusando ``StixService``) é:

1. **Escolher versão / listar** — :meth:`Estagio1Service.listar_bases` delega a
   ``StixService.listar_bases``; ``baixada``/``selecionavel`` são checados contra
   o disco a cada chamada e **nenhuma versão vem baixada por padrão** (Req. 5.4).
2. **Baixar quando ausente** — :meth:`Estagio1Service.baixar_base` baixa o bundle
   **apenas** quando ele não está em disco (reusa o em disco caso presente,
   Req. 5.3), delegando ao ``StixService.baixar_base`` — o **único egresso de
   rede** do sistema, que valida ``versao`` contra ``VERSOES_CONHECIDAS`` antes
   de tocar disco/rede, escreve atomicamente e levanta ``StixDownloadError`` sem
   deixar arquivo parcial em falha (Req. 5.2, 5.9).
3. **Dashboard** — :meth:`Estagio1Service.dashboard` delega ao
   ``StixService.dashboard`` (contagens por tipo) e as campanhas do bundle
   (Req. 5.7).
4. **Concluir** — :meth:`Estagio1Service.concluir` monta a Saída do Estágio
   (bundle em disco + dashboard + campanhas, Req. 5.8) e devolve um
   :class:`ResultadoEstagio1` com ``concluido = True`` **somente quando** o
   bundle está em disco e o dashboard foi montado (Req. 5.10). Uma falha de
   download/leitura mantém o Estágio **não concluído** com a causa reportada
   (Req. 5.9), sem levantar.

A flag de versão curada (``VERSAO_CURADA`` / ``versao_eh_curada``, task 2.1) é
projetada em cada versão da Saída para que a UI saiba qual base carrega os 8
casos curados (Req. 5.5, 5.6) — sem request-time lookup.

Este serviço só **lê** o disco; o único egresso de rede possível é delegado ao
``StixService.baixar_base`` e só ocorre por pedido explícito quando a base ainda
não está em disco. Nenhuma IA é invocada em qualquer caminho.

_Requisitos: 5.1, 5.2, 5.3, 5.4, 5.7, 5.8, 5.9, 5.10, 2.2_
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from app.services.stix.stix_service import (
    BaseStixInfo,
    CampanhaStix,
    DashboardStix,
    StixDownloadError,
    StixFileError,
    StixService,
    versao_eh_curada,
)


# ---------------------------------------------------------------------------
# Typed output DTOs (pt-BR field names, consistent with the design contracts)
# ---------------------------------------------------------------------------


class BaseStixListada(BaseStixInfo):
    """Uma base STIX na listagem do Estágio 1 com a flag de versão curada.

    Estende :class:`~app.services.stix.stix_service.BaseStixInfo` (``versao``,
    ``nome``, ``baixada``, ``selecionavel``, ``arquivo``) com o campo ``curada``,
    projetado a partir de :func:`~app.services.stix.stix_service.versao_eh_curada`
    (fonte única em ``VERSAO_CURADA``, Req. 5.5, 5.6). Assim a tela do Estágio 1
    sabe, sem lookup por request, qual versão carrega os 8 casos curados — a
    versão 18 é a única com ``curada == True``.
    """

    curada: bool = False


class ResultadoEstagio1(BaseModel):
    """O resultado tipado da orquestração do Estágio 1 — "Seleção da base STIX".

    Carrega o veredito de conclusão do Estágio e a sua Saída (Req. 5.8, 5.10):

    * ``versao`` — a versão do pacote STIX escolhida (a Entrada do Estágio 1,
      Req. 5.1).
    * ``concluido`` — ``True`` **somente quando** o bundle da versão está em
      disco **e** o dashboard foi montado com sucesso (Req. 5.10); ``False`` em
      qualquer falha (download/leitura), mantendo o Estágio não concluído
      (Req. 5.9).
    * ``bundle_em_disco`` — se o arquivo ``enterprise-attack-{versao}.json`` está
      presente em disco (a primeira metade da Saída do Estágio, Req. 5.8).
    * ``dashboard`` — o dashboard do bundle (contagens por tipo), a segunda
      metade da Saída do Estágio (Req. 5.7, 5.8); ``None`` quando o Estágio não
      concluiu.
    * ``campanhas`` — as campanhas contidas no bundle (Entrada do Estágio 2,
      Req. 2.2); vazio quando o Estágio não concluiu.
    * ``curada`` — se a versão escolhida é a versão curada (Req. 5.5, 5.6).
    * ``mensagem_erro`` — a causa reportada ao Pesquisador quando o Estágio não
      concluiu (``None`` em sucesso, Req. 5.9).

    A Saída (bundle em disco + dashboard + campanhas) de um Estágio concluído é a
    Entrada do Estágio 2 (Req. 2.2).
    """

    versao: str
    concluido: bool = False
    bundle_em_disco: bool = False
    dashboard: Optional[DashboardStix] = None
    campanhas: list[CampanhaStix] = Field(default_factory=list)
    curada: bool = False
    mensagem_erro: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class Estagio1Service:
    """Orquestra o Estágio 1 "Seleção da base STIX" reusando ``StixService``.

    Serviço **fino**: não reimplementa listagem/download/leitura/dashboard —
    delega tudo ao :class:`~app.services.stix.stix_service.StixService` injetado
    e apenas orquestra a sequência do Estágio 1 e deriva o veredito de conclusão
    (Req. 5.1–5.10, 2.2).

    Um ``StixService`` customizado pode ser injetado (ex.: testes, com um
    ``data_dir`` isolado e um ``http_client`` falso), de modo que o único egresso
    de rede (``baixar_base``) permaneça mockável e nenhum arquivo real seja
    tocado nos testes.
    """

    def __init__(self, stix_service: Optional[StixService] = None) -> None:
        self._stix: StixService = (
            stix_service if stix_service is not None else StixService()
        )

    @property
    def stix_service(self) -> StixService:
        """O :class:`StixService` reusado para todas as operações de disco/rede."""
        return self._stix

    # ---- listar ------------------------------------------------------------

    def listar_bases(self) -> list[BaseStixListada]:
        """Lista as versões conhecidas com ``baixada``/``selecionavel`` e ``curada``.

        Delega a ``StixService.listar_bases`` (ordenação newest→oldest, presença
        em disco checada ao vivo, nenhuma versão baixada por padrão — Req. 5.4) e
        projeta a flag de versão curada em cada entrada via
        :func:`~app.services.stix.stix_service.versao_eh_curada` (Req. 5.5, 5.6).
        Nenhum egresso de rede.
        """
        return [
            BaseStixListada(
                versao=base.versao,
                nome=base.nome,
                baixada=base.baixada,
                selecionavel=base.selecionavel,
                arquivo=base.arquivo,
                curada=versao_eh_curada(base.versao),
            )
            for base in self._stix.listar_bases()
        ]

    # ---- baixar ------------------------------------------------------------

    def _bundle_em_disco(self, versao: str) -> bool:
        """Se o bundle da ``versao`` já está presente em disco (sem rede)."""
        return self._stix.base_path(versao).exists()

    def baixar_base(self, versao: str) -> BaseStixInfo:
        """Garante o bundle da ``versao`` em disco, baixando-o **apenas se ausente**.

        Reutiliza o pacote em disco sem baixá-lo de novo quando já presente
        (Req. 5.3); caso contrário, baixa via ``StixService.baixar_base`` — o
        **único egresso de rede** (valida ``versao`` contra ``VERSOES_CONHECIDAS``
        antes de tocar disco/rede, escrita atômica, Req. 5.2). Em falha de
        download propaga ``StixDownloadError`` (nenhum arquivo parcial fica em
        disco, Req. 5.9); o chamador — :meth:`concluir` — a captura e mantém o
        Estágio não concluído com a causa reportada.

        Returns:
            Um :class:`BaseStixInfo` com ``baixada=True`` e ``arquivo`` apontando
            para o bundle em disco (recém-baixado ou já existente).

        Raises:
            StixDownloadError: quando ``versao`` é desconhecida, o download falha,
                o conteúdo não é um bundle STIX válido, ou a escrita falha.
        """
        if self._bundle_em_disco(versao):
            caminho = self._stix.base_path(versao)
            return BaseStixInfo(
                versao=versao,
                nome=f"Enterprise ATT&CK v{versao}",
                baixada=True,
                selecionavel=True,
                arquivo=str(caminho),
            )
        return self._stix.baixar_base(versao)

    # ---- dashboard ---------------------------------------------------------

    def dashboard(self, versao: str) -> DashboardStix:
        """O dashboard do bundle (contagens por tipo) da ``versao`` (Req. 5.7).

        Delega a ``StixService.dashboard``, que lê o bundle de disco e deriva as
        contagens por tipo. A :class:`StixFileError` tipada **não** é capturada
        aqui — propaga para o chamador (:meth:`concluir`) tratar como Estágio não
        concluído com causa reportada (Req. 5.9).

        Raises:
            StixFileError: quando o bundle está ausente/malformado/inválido.
        """
        return self._stix.dashboard(versao)

    def campanhas(self, versao: str) -> list[CampanhaStix]:
        """As campanhas contidas no bundle da ``versao`` (Entrada do Estágio 2).

        Delega a ``StixService.listar_campanhas``; a lista é a Entrada do
        Estágio 2 (Req. 2.2). A :class:`StixFileError` propaga (não capturada
        aqui) para o veredito de conclusão tratá-la.
        """
        return self._stix.listar_campanhas(versao)

    # ---- concluir ----------------------------------------------------------

    def concluir(self, versao: str) -> ResultadoEstagio1:
        """Orquestra o Estágio 1 e devolve o veredito de conclusão + Saída.

        Fluxo (Req. 5.2, 5.3, 5.7, 5.8, 5.9, 5.10, 2.2):

        1. **Garantir o bundle em disco** — baixa via :meth:`baixar_base` apenas
           se ausente (único egresso de rede, reusa o disco caso presente). Uma
           :class:`StixDownloadError` mantém o Estágio **não concluído** com a
           causa reportada e nenhum arquivo parcial em disco (Req. 5.9).
        2. **Montar o dashboard** — via :meth:`dashboard`; uma
           :class:`StixFileError` (bundle ausente/malformado/inválido) também
           mantém o Estágio **não concluído** com a causa reportada (Req. 5.9).
        3. **Montar as campanhas** — a Entrada do Estágio 2 (Req. 2.2).
        4. **Concluir** — ``concluido = True`` **somente quando** o bundle está
           em disco **e** o dashboard foi montado (Req. 5.10). A Saída (bundle +
           dashboard + campanhas) é a Entrada do Estágio 2.

        Nunca levanta: toda falha é capturada e refletida em ``concluido=False``
        com ``mensagem_erro`` preenchida, de modo que a tela do Estágio sempre
        receba um resultado reportável (Req. 5.9).
        """
        curada = versao_eh_curada(versao)

        # 1) Garantir o bundle em disco (único egresso de rede, só se ausente).
        try:
            self.baixar_base(versao)
        except StixDownloadError as exc:
            # Download falhou: Estágio não concluído, causa reportada, sem
            # arquivo parcial em disco (Req. 5.9).
            return ResultadoEstagio1(
                versao=versao,
                concluido=False,
                bundle_em_disco=self._bundle_em_disco(versao),
                dashboard=None,
                campanhas=[],
                curada=curada,
                mensagem_erro=exc.detail,
            )

        # 2) Montar o dashboard (lê o bundle de disco).
        try:
            dashboard = self.dashboard(versao)
            campanhas = self.campanhas(versao)
        except StixFileError as exc:
            # Bundle ausente/malformado/inválido após o passo 1: Estágio não
            # concluído, causa reportada (Req. 5.9).
            return ResultadoEstagio1(
                versao=versao,
                concluido=False,
                bundle_em_disco=self._bundle_em_disco(versao),
                dashboard=None,
                campanhas=[],
                curada=curada,
                mensagem_erro=exc.detail,
            )

        # 3+4) Concluir só quando bundle em disco E dashboard montado (Req. 5.10).
        bundle_em_disco = self._bundle_em_disco(versao)
        return ResultadoEstagio1(
            versao=versao,
            concluido=bundle_em_disco,
            bundle_em_disco=bundle_em_disco,
            dashboard=dashboard,
            campanhas=campanhas,
            curada=curada,
            mensagem_erro=None,
        )
