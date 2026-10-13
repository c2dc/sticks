"""EnvironmentStageService — conclusão automática do Estágio 4 (task 4.2).

O Estágio 4 (Provisionamento do Ambiente) de um Caso **curado** não exige ação
do Pesquisador: assim que o Estágio 3 (Operacionalização) está ``concluido`` e o
ambiente curado está disponível e **verificado isolado**, o Backend marca o
``StageRun`` de ``estagio=4`` (:data:`ESTAGIO_PROVISIONAMENTO`) como ``concluido``
automaticamente (Req. 5.3, 9.2). Se o isolamento **não** puder ser verificado, o
Estágio 4 não conclui e o Estágio 5 permanece bloqueado pelo gate sequencial
(Req. 5.6, 9.3).

Este serviço é a contraparte, para o ambiente, da auto-conclusão do DAG
(``DagService.sincronizar_dags_curados`` + conclusão do Estágio 2): mesma costura
de *service com ``Session`` injetada*, mesmo *upsert idempotente* do ``StageRun``
por ``(caso_id, estagio)`` e mesmo *padrão transacional* (um único ``commit``;
``rollback`` preserva o estado anterior em falha de persistência) de
:class:`~app.services.session.session_state_service.SessionStateService` e
:class:`~app.services.dag.dag_service.DagService`.

Decisões de design
------------------

**Gate sequencial (Req. 9.3).** A auto-conclusão só avança quando o
``StageRun`` do Estágio 3 do Caso está ``concluido``. Enquanto não estiver, o
método não faz nada — o Estágio 4 fica não concluído (bloqueado), exatamente como
:meth:`StageService.is_stage_startable` deriva. A decisão depende **apenas** do
estado do Estágio anterior, nunca do próprio Estágio 4, mantendo o gate puramente
sequencial.

**Material curado do ambiente (Req. 9.4).** A auto-conclusão **não** ocorre para
um Caso sem o material de ambiente correspondente. O "arquivo de configuração do
ambiente" dos Casos curados é o ``docker/docker-compose.yml`` do laboratório, cujo
caminho é resolvido por :func:`...topologia_builder.compose_path` (devolve
``None`` quando o arquivo não existe). Sem esse material, o Estágio 4 não conclui.
A disponibilidade do arquivo é injetável (``compose_disponivel``) para os testes
exercitarem o caminho "sem material" sem tocar no sistema de arquivos.

**Verificação de isolamento (Req. 5.6).** Com o gate liberado e o material
presente, o serviço chama
:meth:`CuradoriaProvisionadorDeAmbiente.preparar` e só conclui o Estágio 4 quando
``resultado.pronto``/``isolado`` é ``True``. Caso contrário o Estágio 4 fica não
concluído (e o Estágio 5 bloqueado), e a razão fica disponível no
:class:`ResultadoProvisionamento` para a tela exibir.

**Inspector indisponível (Docker ausente em dev/CI) — fail-closed.** Em produção,
quando nenhum inspector é injetado, :func:`inspect_isolation` constrói
preguiçosamente o adaptador real do Docker e **levanta ``RuntimeError``** se o
daemon/SDK não estiver disponível (o caso típico do ambiente de desenvolvimento
Windows). Nesse caso o isolamento **genuinamente não pode ser verificado**.
Decisão: **não concluir** o Estágio 4 (fail-closed), em vez de concluir
assumindo isolamento. Isso preserva o contrato de segurança do Req. 5.6/9.2 — "o
Backend marca concluído **após verificar** o isolamento" — sem enfraquecê-lo
silenciosamente. A impossibilidade de verificar é sinalizada de forma explícita no
resultado tipado (``isolamento_indisponivel=True`` + ``mensagem``), para que a
camada de API/tela mostre "ambiente não verificado" e o Estágio 5 permaneça
bloqueado. Em ambiente com Docker (ou com um inspector injetado — produção ou
testes), a verificação roda normalmente e, isolado, o Estágio 4 auto-conclui.

Idempotência / transacionalidade
---------------------------------
A conclusão é um *upsert absoluto* do ``StageRun`` de ``(caso_id, estagio=4)``
para ``concluido``/progresso 100 em uma única transação. Reexecutar produz
exatamente o mesmo estado persistido (nenhuma linha nova, nenhum campo
divergente). Um ``rollback`` em falha de persistência deixa o estado anterior
intacto, sem mutação parcial — mesmo padrão de ``escolher_dag`` /
``persist_stage_completion``.

_Requisitos: 5.2, 5.3, 5.6, 9.2, 9.3, 9.4_
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import StageRun
from app.models.enums import StageState
from app.services.containment import ContainerInspector
from app.services.environment.provisionador import (
    CuradoriaProvisionadorDeAmbiente,
    ProvisionadorDeAmbiente,
)
from app.services.environment.topologia_builder import compose_path
from app.services.stage.stage_service import (
    ESTAGIO_OPERACIONALIZACAO,
    ESTAGIO_PROVISIONAMENTO,
)


def _utcnow() -> dt.datetime:
    """Timestamp UTC *naive* (portável entre PostgreSQL e o SQLite de teste)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class ProvisionamentoAutoResult(BaseModel):
    """Resultado tipado de :meth:`EnvironmentStageService.auto_concluir_estagio4`.

    Em vez de levantar exceções, a auto-conclusão devolve este resultado tipado
    (mesmo contrato de ``EscolhaDagResult`` / ``StageCompletionResult``):
    ``concluido`` indica se o Estágio 4 passou a ``concluido`` nesta chamada (ou
    já estava, de forma idempotente); os demais campos explicam **por que** não
    concluiu, de modo que a camada de API/tela possa surfacear a razão e manter o
    Estágio 5 bloqueado.

    Attributes
    ----------
    caso_id:
        O Caso cuja auto-conclusão do Estágio 4 foi tentada.
    concluido:
        ``True`` quando, ao fim da chamada, o ``StageRun`` do Estágio 4 do Caso
        está ``concluido`` (progresso 100) — por esta chamada ou já de antes
        (idempotência). ``False`` em qualquer caminho que **não** conclua (gate
        não liberado, material ausente, não isolado, isolamento não verificável,
        ou falha de persistência).
    bloqueado_pelo_gate:
        ``True`` quando o Estágio 3 do Caso **não** está ``concluido``; o
        Estágio 4 permanece bloqueado e nada é persistido (Req. 9.3).
    material_ausente:
        ``True`` quando não há material curado de ambiente disponível para o
        Caso (``compose`` ausente); a auto-conclusão não ocorre (Req. 9.4).
    isolado:
        ``True`` sse a verificação de isolamento rodou e passou. ``False`` quando
        não rodou (gate/material/indisponível) ou falhou.
    isolamento_indisponivel:
        ``True`` quando o isolamento **não pôde ser verificado** porque o
        inspector/daemon Docker está indisponível (fail-closed): o Estágio 4
        **não** conclui (Req. 5.6). Distinto de ``isolado=False`` por ambiente
        comprovadamente não isolado.
    persistencia_falhou:
        ``True`` quando a verificação aprovou mas o ``commit`` falhou; o estado
        anterior foi preservado sem alteração (rollback).
    mensagem:
        Texto pt-BR explicando o resultado (razão da não-conclusão), para a
        API/UI surfacearem; ``None`` quando concluiu sem ressalvas.
    stage_run_id:
        O id do ``StageRun`` do Estágio 4, quando concluído por esta chamada;
        ``None`` caso contrário.
    """

    caso_id: str
    concluido: bool = False
    bloqueado_pelo_gate: bool = False
    material_ausente: bool = False
    isolado: bool = False
    isolamento_indisponivel: bool = False
    persistencia_falhou: bool = False
    mensagem: Optional[str] = None
    stage_run_id: Optional[int] = None


# Mensagens pt-BR por caminho de não-conclusão (para a API/UI surfacearem).
_MSG_GATE = (
    "O Estágio 3 (Operacionalização) deste Caso ainda não está concluído; o "
    "Estágio 4 (Provisionamento do Ambiente) permanece bloqueado."
)
_MSG_SEM_MATERIAL = (
    "Este Caso não possui arquivo de configuração de ambiente disponível; o "
    "Estágio 4 não é concluído automaticamente."
)
_MSG_NAO_ISOLADO = (
    "O ambiente deste Caso não pôde ser verificado como isolado; o Estágio 4 "
    "não é concluído e o Estágio 5 permanece bloqueado."
)
_MSG_ISOLAMENTO_INDISPONIVEL = (
    "Não foi possível verificar o isolamento do ambiente (Docker indisponível); "
    "o Estágio 4 não é concluído sem verificação de isolamento."
)
_MSG_FALHA_PERSISTENCIA = (
    "A conclusão automática do Estágio 4 não foi salva: falha ao persistir na "
    "Base_de_Dados. O estado anterior foi preservado sem alteração."
)


class EnvironmentStageService:
    """Conclui automaticamente o Estágio 4 de Casos curados (task 4.2).

    Mesmo molde dos demais serviços *session-bound* (``DagService`` /
    ``SessionStateService``): recebe uma :class:`~sqlalchemy.orm.Session`
    injetada e um :class:`ProvisionadorDeAmbiente` (curadoria por padrão). O
    inspector Docker é **injetável** e repassado ao provisionador de curadoria;
    quando ``None``, a verificação de isolamento usa o adaptador real do Docker
    (que falha fechado se o daemon estiver ausente — ver a decisão
    "inspector indisponível" no módulo).

    Parameters
    ----------
    session:
        :class:`~sqlalchemy.orm.Session` aberta, injetada pelo chamador. O
        serviço não a abre nem fecha; faz ``commit`` na conclusão e ``rollback``
        em falha, preservando o estado anterior.
    provisionador:
        :class:`ProvisionadorDeAmbiente` que prepara/verifica o ambiente. Default:
        :class:`CuradoriaProvisionadorDeAmbiente` com o ``inspector`` injetado.
    inspector:
        :class:`~app.services.containment.ContainerInspector` **read-only**
        usado na verificação de isolamento. ``None`` em produção (o adaptador
        real é construído preguiçosamente apenas quando a verificação roda); os
        testes injetam um inspector falso. Ignorado quando ``provisionador`` é
        fornecido explicitamente.
    compose_disponivel:
        Callable que informa se o material curado de ambiente está disponível
        (Req. 9.4). Default: ``lambda: compose_path() is not None`` — o
        ``docker/docker-compose.yml`` do laboratório. Injetável nos testes para
        exercitar o caminho "sem material" sem tocar no disco.
    """

    def __init__(
        self,
        session: Session,
        *,
        provisionador: Optional[ProvisionadorDeAmbiente] = None,
        inspector: Optional[ContainerInspector] = None,
        compose_disponivel: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._session = session
        self._provisionador: ProvisionadorDeAmbiente = (
            provisionador
            if provisionador is not None
            else CuradoriaProvisionadorDeAmbiente(inspector=inspector)
        )
        self._compose_disponivel: Callable[[], bool] = (
            compose_disponivel
            if compose_disponivel is not None
            else (lambda: compose_path() is not None)
        )

    @property
    def provisionador(self) -> ProvisionadorDeAmbiente:
        """O :class:`ProvisionadorDeAmbiente` usado para preparar/verificar."""
        return self._provisionador

    def auto_concluir_estagio4(self, caso: str) -> ProvisionamentoAutoResult:
        """Tenta concluir automaticamente o Estágio 4 do Caso curado ``caso``.

        Fluxo (todos os caminhos são read-only exceto a conclusão final):

        1. **Gate sequencial (Req. 9.3).** Se o Estágio 3 do Caso **não** está
           ``concluido``, não faz nada e devolve ``bloqueado_pelo_gate=True``.
        2. **Idempotência.** Se o Estágio 4 já está ``concluido``, devolve
           ``concluido=True`` sem repersistir (o estado já é o desejado).
        3. **Material curado (Req. 9.4).** Se não há material de ambiente
           disponível, devolve ``material_ausente=True`` sem concluir.
        4. **Verificação de isolamento (Req. 5.6).** Chama
           ``provisionador.preparar(caso)``. Se o inspector/daemon Docker estiver
           indisponível (``RuntimeError``), devolve
           ``isolamento_indisponivel=True`` **sem** concluir (fail-closed). Se
           rodou mas o ambiente não é isolado, devolve ``isolado=False`` sem
           concluir.
        5. **Conclusão.** Isolado ⇒ *upsert* do ``StageRun`` de ``(caso, 4)``
           para ``concluido``/100 em uma única transação. Falha de ``commit`` ⇒
           ``rollback`` + ``persistencia_falhou=True`` (estado anterior
           preservado).

        Não levanta por caminhos esperados — a razão é sempre um sinal tipado no
        resultado, como ``EscolhaDagResult`` / ``StageCompletionResult``.
        """
        estados = self._estados_map(caso)

        # (1) Gate sequencial (Req. 9.3): só prossegue com o Estágio 3 concluído.
        if estados.get(ESTAGIO_OPERACIONALIZACAO) is not StageState.COMPLETED:
            return ProvisionamentoAutoResult(
                caso_id=caso,
                concluido=False,
                bloqueado_pelo_gate=True,
                mensagem=_MSG_GATE,
            )

        # (2) Idempotência: já concluído ⇒ nada a repersistir.
        if estados.get(ESTAGIO_PROVISIONAMENTO) is StageState.COMPLETED:
            row = self._stage_run_row(caso, ESTAGIO_PROVISIONAMENTO)
            return ProvisionamentoAutoResult(
                caso_id=caso,
                concluido=True,
                isolado=True,
                stage_run_id=(row.id if row is not None else None),
            )

        # (3) Material curado de ambiente disponível (Req. 9.4).
        if not self._compose_disponivel():
            return ProvisionamentoAutoResult(
                caso_id=caso,
                concluido=False,
                material_ausente=True,
                mensagem=_MSG_SEM_MATERIAL,
            )

        # (4) Verificação de isolamento (Req. 5.6). Fail-closed quando o Docker
        # está indisponível: o isolamento não pôde ser verificado, então não
        # concluímos (preserva o contrato de segurança sem enfraquecê-lo).
        try:
            resultado = self._provisionador.preparar(caso)
        except RuntimeError:
            return ProvisionamentoAutoResult(
                caso_id=caso,
                concluido=False,
                isolado=False,
                isolamento_indisponivel=True,
                mensagem=_MSG_ISOLAMENTO_INDISPONIVEL,
            )

        if not resultado.pronto:
            # Ambiente comprovadamente não isolado ⇒ Estágio 4 não conclui, a
            # topologia/razão já acompanham o resultado para a tela (Req. 5.6).
            return ProvisionamentoAutoResult(
                caso_id=caso,
                concluido=False,
                isolado=False,
                mensagem=_MSG_NAO_ISOLADO,
            )

        # (5) Isolado ⇒ conclui o Estágio 4 (upsert absoluto, transacional).
        stage_run = self._get_or_create_stage_run(
            caso, estagio=ESTAGIO_PROVISIONAMENTO
        )
        stage_run.estado = StageState.COMPLETED
        stage_run.progresso = 100
        stage_run.mensagem_erro = None
        stage_run.etapa_falha = None
        stage_run.atualizado_em = _utcnow()

        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            return ProvisionamentoAutoResult(
                caso_id=caso,
                concluido=False,
                isolado=True,
                persistencia_falhou=True,
                mensagem=_MSG_FALHA_PERSISTENCIA,
            )

        self._session.refresh(stage_run)
        return ProvisionamentoAutoResult(
            caso_id=caso,
            concluido=True,
            isolado=True,
            stage_run_id=stage_run.id,
        )

    # ---- helpers (leituras puras sobre StageRun) -------------------------

    def _estados_map(self, caso_id: str) -> dict[int, StageState]:
        """Mapa ``estagio -> StageState`` do Caso, a partir dos ``StageRun``.

        Mesma leitura de ``StageService._stage_states_map``: um único snapshot
        dirige a decisão do gate e a idempotência. Estágios sem linha ficam
        ausentes do mapa; os chamadores os tratam como ``nao_iniciado``. Havendo
        mais de uma linha para o mesmo Estágio, a de maior ``id`` (mais recente)
        prevalece.
        """
        stmt = (
            select(StageRun.estagio, StageRun.estado)
            .where(StageRun.caso_id == caso_id)
            .order_by(StageRun.id.asc())
        )
        estados: dict[int, StageState] = {}
        for estagio, estado in self._session.execute(stmt).all():
            estados[estagio] = estado
        return estados

    def _stage_run_row(self, caso_id: str, estagio: int) -> Optional[StageRun]:
        """Última linha ``StageRun`` de ``(caso_id, estagio)``, se houver."""
        stmt = (
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == estagio)
            .order_by(StageRun.id.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return rows[-1] if rows else None

    def _get_or_create_stage_run(self, caso_id: str, *, estagio: int) -> StageRun:
        """Devolve o ``StageRun`` de ``(caso_id, estagio)``, criando-o se ausente.

        Uma linha por ``(caso_id, estagio)`` (mesmo contrato de upsert de
        ``StageService`` / ``SessionStateService`` / ``DagService``), de modo que
        a auto-conclusão atualiza a execução existente em vez de acumular linhas.
        """
        stmt = (
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == estagio)
            .order_by(StageRun.id.asc())
        )
        stage_run = self._session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(caso_id=caso_id, estagio=estagio)
            self._session.add(stage_run)
        return stage_run


__all__ = [
    "EnvironmentStageService",
    "ProvisionamentoAutoResult",
]
