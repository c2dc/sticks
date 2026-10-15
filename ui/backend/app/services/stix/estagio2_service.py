"""Estágio 2 — Seleção da campanha: serviço fino de orquestração (task 3.1).

Este módulo implementa o **serviço fino** do Estágio 2 do pipeline de seis
Estágios ("Seleção da campanha"), reusando integralmente serviços já existentes:
o :class:`~app.services.stix.stix_service.StixService` (listagem das campanhas do
bundle, marcando ``curada``/``slug_curado`` via ``SLUG_PARA_STIX``) e o
:class:`~app.services.stage.stage_service.StageService` (o DTO histórico
:class:`~app.services.stage.stage_service.CampanhaStixEntrada` montado por
``StageService._build_campanha_stix`` para a identidade/aliases, e a extração
estrutural curada do DAG para as técnicas associadas). Ele **não** reimplementa
listagem, leitura de bundle nem extração — apenas *orquestra* essas operações na
ordem definida pelo Estágio 2 e deriva o veredito de conclusão (Req. 6.1–6.7,
2.3).

Alinhamento com o design ("Backend — Estágio 2: Seleção da campanha")
---------------------------------------------------------------------
A orquestração do Estágio 2 (serviço fino, novo) é:

1. **Listar campanhas** — :meth:`Estagio2Service.listar_campanhas` delega a
   ``StixService.listar_campanhas``, que já marca cada campanha com
   ``curada``/``slug_curado`` via ``SLUG_PARA_STIX`` (Req. 6.2). Para a versão
   18 (curada) as 8 campanhas curadas ficam selecionáveis (``selecionavel``);
   para campanhas não curadas, a seleção fica indisponível neste primeiro
   momento, pois a extração dos atributos depende dos artefatos curados em disco
   (Req. 6.5). A Entrada é o bundle selecionado no Estágio 1 (Req. 6.1).
2. **Selecionar / extrair atributos** — :meth:`Estagio2Service.selecionar`
   extrai os atributos da campanha selecionada (Req. 6.3): a **identidade** e os
   **aliases** vêm de ``StageService._build_campanha_stix(slug, versao)``
   (defensivo, sem rede nos caminhos curados — a leitura do bundle é local), e
   as **técnicas** vêm da extração estrutural do DAG curado via
   ``StageService.run_stage1(slug, versao)`` (reuso do DTO histórico
   ``Stage1*`` que, após a renumeração, serve o Estágio 2). A Saída são os
   atributos extraídos (``CampanhaStixEntrada`` + técnicas), que alimentam o
   Estágio 3 (Req. 6.4, 2.3).
3. **Concluir** — :meth:`Estagio2Service.selecionar` conclui o Estágio
   (``concluido = True``) **somente quando** a extração teve sucesso (Req. 6.6).
   Uma falha de extração (slug inexistente, DAG ausente/ilegível) mantém o
   Estágio **não concluído** com a causa reportada (Req. 6.7), sem levantar.

Este serviço só **lê** o disco nos caminhos curados; nenhuma rede é tocada ao
listar/selecionar uma campanha curada e nenhuma IA é invocada em qualquer
caminho (os artefatos curados são lidos localmente).

_Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 2.3_
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import StageState
from app.services.stage.stage_service import (
    CampanhaStixEntrada,
    StageService,
    TechniqueElement,
)
from app.services.stix.stix_service import (
    CampanhaStix,
    StixService,
)


# ---------------------------------------------------------------------------
# Typed output DTOs (pt-BR field names, consistent with the design contracts)
# ---------------------------------------------------------------------------


class CampanhaListada(CampanhaStix):
    """Uma campanha do bundle na listagem do Estágio 2, com a flag de seleção.

    Estende :class:`~app.services.stix.stix_service.CampanhaStix` (``stix_id``,
    ``nome``, ``descricao``, ``curada``, ``slug_curado``) com o campo
    ``selecionavel``: só é ``True`` quando a campanha é curada (``curada`` com
    ``slug_curado`` preenchido), pois a extração dos atributos depende dos
    artefatos curados em disco. Para a versão 18 (curada) as 8 campanhas curadas
    são selecionáveis (Req. 6.2, 6.5).
    """

    selecionavel: bool = False


class ResultadoSelecaoCampanha(BaseModel):
    """O resultado tipado da seleção de uma campanha no Estágio 2 (Req. 6.3–6.7).

    Carrega o veredito de conclusão do Estágio e a sua Saída:

    * ``caso_id`` — o slug curado da campanha selecionada.
    * ``versao`` — a versão do bundle STIX (a Entrada do Estágio 1/2, Req. 6.1).
    * ``concluido`` — ``True`` **somente quando** a extração dos atributos teve
      sucesso (identidade + técnicas, Req. 6.6); ``False`` em qualquer falha de
      extração, mantendo o Estágio não concluído (Req. 6.7).
    * ``identidade`` — o bloco de identidade/aliases da campanha
      (:class:`CampanhaStixEntrada`), sempre presente (montado defensivamente,
      nunca levanta — Req. 6.3).
    * ``tecnicas`` — as técnicas associadas extraídas do DAG curado (Saída do
      Estágio 2, Req. 6.4); vazio quando a extração falhou.
    * ``mensagem_erro`` — a causa reportada ao Pesquisador quando o Estágio não
      concluiu (``None`` em sucesso, Req. 6.7).
    * ``etapa_falha`` — a etapa da extração estrutural onde a falha ocorreu
      (``None`` em sucesso).

    A Saída (identidade + técnicas) de um Estágio 2 concluído é a Entrada do
    Estágio 3 (Req. 6.4, 2.3).
    """

    caso_id: str
    versao: Optional[str] = None
    concluido: bool = False
    identidade: Optional[CampanhaStixEntrada] = None
    tecnicas: list[TechniqueElement] = Field(default_factory=list)
    mensagem_erro: Optional[str] = None
    etapa_falha: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class Estagio2Service:
    """Orquestra o Estágio 2 "Seleção da campanha" reusando serviços existentes.

    Serviço **fino**: não reimplementa listagem de campanhas nem extração de
    atributos — delega a listagem ao
    :class:`~app.services.stix.stix_service.StixService` e a extração ao
    :class:`~app.services.stage.stage_service.StageService` (``_build_campanha_stix``
    para a identidade/aliases e ``run_stage1`` para as técnicas), apenas
    orquestrando a sequência do Estágio 2 e derivando o veredito de conclusão
    (Req. 6.1–6.7, 2.3).

    O :class:`StixService` reusado para a listagem é, por padrão, o mesmo que o
    :class:`StageService` já carrega (``stage_service._stix_service``), de modo
    que listagem e extração enxergam o mesmo ``data_dir`` — essencial em testes
    com um diretório temporário isolado. Um ``stix_service`` explícito pode ser
    injetado quando necessário.
    """

    def __init__(
        self,
        stage_service: StageService,
        stix_service: Optional[StixService] = None,
    ) -> None:
        self._stage: StageService = stage_service
        # Reusa o StixService do StageService por padrão para compartilhar o
        # mesmo data_dir (listagem e extração sobre o mesmo bundle em disco).
        self._stix: StixService = (
            stix_service
            if stix_service is not None
            else stage_service._stix_service  # noqa: SLF001 — reuso intencional
        )

    @property
    def stage_service(self) -> StageService:
        """O :class:`StageService` reusado para a extração dos atributos."""
        return self._stage

    @property
    def stix_service(self) -> StixService:
        """O :class:`StixService` reusado para listar as campanhas do bundle."""
        return self._stix

    # ---- listar ------------------------------------------------------------

    def listar_campanhas(self, versao: str) -> list[CampanhaListada]:
        """Lista as campanhas do bundle da ``versao``, marcando a selecionável.

        Delega a ``StixService.listar_campanhas`` (que já resolve
        ``curada``/``slug_curado`` via ``SLUG_PARA_STIX``, preservando a ordem do
        bundle) e projeta ``selecionavel`` em cada entrada: só é ``True`` para as
        campanhas curadas (com ``slug_curado``), pois a extração dos atributos
        depende dos artefatos curados em disco. Para a versão 18 (curada) as 8
        campanhas curadas são selecionáveis (Req. 6.2, 6.5).

        A :class:`~app.services.stix.stix_service.StixFileError` tipada **não** é
        capturada aqui — propaga para o endpoint mapeá-la ao contrato de bundle
        inválido (precedente do Estágio 1).

        Raises:
            StixFileError: quando o bundle está ausente/malformado/inválido.
        """
        return [
            CampanhaListada(
                stix_id=campanha.stix_id,
                nome=campanha.nome,
                descricao=campanha.descricao,
                curada=campanha.curada,
                slug_curado=campanha.slug_curado,
                selecionavel=campanha.curada and campanha.slug_curado is not None,
            )
            for campanha in self._stix.listar_campanhas(versao)
        ]

    # ---- selecionar --------------------------------------------------------

    def selecionar(self, slug: str, versao: Optional[str]) -> ResultadoSelecaoCampanha:
        """Seleciona a campanha ``slug`` e extrai os seus atributos (Req. 6.3–6.7).

        Fluxo:

        1. **Identidade/aliases** — monta o bloco :class:`CampanhaStixEntrada`
           via ``StageService._build_campanha_stix(slug, versao)`` (defensivo,
           sem rede nos caminhos curados; nunca levanta). Para uma campanha
           curada da versão curada ele carrega os campos ricos do objeto
           ``campaign`` (identidade, external_id, descrição, aliases, datas).
        2. **Técnicas** — extrai as técnicas associadas do DAG curado via
           ``StageService.run_stage1(slug, versao)``. ``run_stage1`` reusa o DTO
           histórico ``Stage1*`` (que, após a renumeração, serve o Estágio 2) e
           **não levanta** para um caso com DAG ausente/ilegível — reporta a
           falha no resultado (``estado = erro``, ``etapa_falha``).
        3. **Veredito** — conclui o Estágio (``concluido = True``) **somente
           quando** a extração teve sucesso (``estado`` concluído). Uma falha de
           extração mantém o Estágio não concluído com a causa reportada (slug
           inexistente, DAG ausente/ilegível) — Req. 6.6, 6.7.

        Nunca levanta por um caso inválido: a falha é refletida em
        ``concluido=False`` com ``mensagem_erro``/``etapa_falha`` preenchidos.
        Um slug genuinamente desconhecido (erro de programação) ainda propaga o
        ``KeyError`` do ``CaseService`` por ``run_stage1``, consistente com o
        precedente do Estágio 1.

        Args:
            slug: O slug curado da campanha selecionada (ex.: ``"shadowray"``).
            versao: A versão do bundle STIX selecionada no Estágio 1 (ex.:
                ``"18.0"``). Opcional; quando omitida, a identidade cai para o
                fallback ``versao_ausente`` sem erro, mas a extração das técnicas
                (do DAG curado) permanece possível.

        Returns:
            Um :class:`ResultadoSelecaoCampanha` com a identidade, as técnicas e
            o veredito de conclusão.
        """
        # 1) Identidade/aliases — defensivo, nunca levanta (Property 1).
        identidade = self._stage._build_campanha_stix(slug, versao)  # noqa: SLF001

        # 2) Técnicas — extração estrutural do DAG curado (reuso Stage1*). Não
        #    levanta para um caso com DAG ausente/ilegível: reporta no resultado.
        resultado = self._stage.run_stage1(slug, versao=versao)

        concluido = resultado.estado == StageState.COMPLETED
        tecnicas = (
            list(resultado.saida.techniques)
            if concluido and resultado.saida is not None
            else []
        )

        return ResultadoSelecaoCampanha(
            caso_id=slug,
            versao=versao,
            concluido=concluido,
            identidade=identidade,
            tecnicas=tecnicas,
            mensagem_erro=None if concluido else resultado.mensagem_erro,
            etapa_falha=None if concluido else resultado.etapa_falha,
        )

    # ---- ler (visão read-only) --------------------------------------------

    def ler(self, slug: str, versao: Optional[str]) -> ResultadoSelecaoCampanha:
        """Visão read-only do Estágio 2 — NUNCA executa nem persiste.

        Reflete o estado persistido do Estágio sem rodar a extração: monta a
        identidade (sempre derivável, sem escrita) e, quando o Estágio já foi
        concluído, deriva as técnicas do DAG curado puramente via
        ``StageService.read_stage1`` (os mesmos extratores de ``run_stage1``,
        sem persistir). Enquanto o Estágio não foi concluído, ``tecnicas`` fica
        vazio e ``concluido=False`` — abrir a tela nunca fabrica um estado
        concluído.

        Espelha o precedente do Estágio 1 (``read_stage1`` vs ``run_stage1``):
        o ``GET .../estagio/2`` usa esta visão; a seleção efetiva usa
        :meth:`selecionar`.
        """
        identidade = self._stage._build_campanha_stix(slug, versao)  # noqa: SLF001
        resultado = self._stage.read_stage1(slug, versao=versao)

        concluido = resultado.estado == StageState.COMPLETED
        tecnicas = (
            list(resultado.saida.techniques)
            if concluido and resultado.saida is not None
            else []
        )

        return ResultadoSelecaoCampanha(
            caso_id=slug,
            versao=versao,
            concluido=concluido,
            identidade=identidade,
            tecnicas=tecnicas,
            mensagem_erro=None if concluido else resultado.mensagem_erro,
            etapa_falha=None if concluido else resultado.etapa_falha,
        )
