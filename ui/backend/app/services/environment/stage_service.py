"""EnvironmentStageService — Estágio 4 (auto-conclusão) + Estágio 5 (provisionamento ativo).

Este serviço *session-bound* orquestra o ambiente para o pipeline de seis
Estágios. Ele cobre dois papéis complementares:

* **Estágio 4 — auto-conclusão (task 4.2).** :meth:`~EnvironmentStageService.auto_concluir_estagio4`
  conclui automaticamente o Estágio 4 de um Caso curado quando o Estágio 3 está
  ``concluido``, o material curado existe e o isolamento é **verificado**
  (read-only). Preservado sem alteração de comportamento.
* **Estágio 5 — provisionamento ativo (task 5.1).**
  :meth:`~EnvironmentStageService.provisionar_estagio5` dispara, **sem ação
  manual** (Req. 1.1), o pipeline de provisionamento da costura
  :class:`ProvisionadorDeAmbiente` (subir contêineres → aguardar saúde →
  carregar Abilities → verificar isolamento), encaminha cada linha de log ao
  :class:`ProvisioningLogBuffer` compartilhado — que o canal WebSocket lê para o
  terminal ao vivo da tela (Req. 2.2) — e atualiza o ``StageRun`` do Estágio 5
  com o progresso por etapa e, em falha, ``etapa_falha`` + ``mensagem_erro``
  (Req. 7.1). A **conclusão** do Estágio 5 ocorre **apenas** quando os três
  pré-requisitos passam (saúde + Abilities + isolamento — ``ambiente.pronto``,
  Req. 5.5), concluindo o ``StageRun`` para ``concluido``/100 e liberando o
  Estágio 6 pelo gate sequencial (task 5.3 — Req. 6.2, 6.4); a idempotência
  (task 5.2) reaproveita um ambiente pronto já persistido.

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

from app.models.domain import AmbienteProvisionadoRow, StageRun
from app.models.enums import PlataformaAmbientePersistida, StageState
from app.services.containment import ContainerInspector
from app.services.environment.compose_executor import LinhaLog
from app.services.environment.log_buffer import ProvisioningLogBuffer
from app.services.environment.operacionalizacao import ParametrosDeAmbiente
from app.services.environment.provisionador import (
    AmbienteProvisionado,
    CuradoriaProvisionadorDeAmbiente,
    ProvisionadorDeAmbiente,
)
from app.services.environment.topologia_builder import compose_path
from app.services.stage.stage_service import (
    ESTAGIO_AMBIENTE,
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


class ProvisionamentoEstagio5Result(BaseModel):
    """Resultado tipado de :meth:`EnvironmentStageService.provisionar_estagio5` (task 5.1).

    O provisionamento **ativo** do Estágio 5 (subir o laboratório → aguardar
    saúde → carregar Abilities → verificar isolamento) é disparado
    automaticamente ao iniciar o Estágio 5 (Req. 1.1). Como as demais costuras
    *session-bound*, em vez de levantar por caminhos esperados a orquestração
    devolve este resultado tipado, carregando o que a camada de API/tela precisa
    para refletir o andamento e a (não-)conclusão e manter o Estágio 6 bloqueado
    enquanto o ambiente não estiver pronto (Req. 7.1).

    .. note::
        A orquestração cobre o **disparo automático** (task 5.1), o
        encaminhamento das linhas de log ao buffer (que alimenta o canal —
        Req. 2.2), a idempotência (task 5.2 — reaproveitar um ambiente pronto já
        persistido) e a atualização do ``StageRun`` conforme o veredito do
        pipeline (Req. 7.1): ``em_andamento`` no início; ``etapa_falha`` +
        ``mensagem_erro`` + ``erro`` em falha; e — agora (task 5.3) — a
        **conclusão** para ``concluido``/progresso 100 **apenas quando os três
        pré-requisitos passam** (``ambiente.pronto`` — saúde + Abilities +
        isolamento, Req. 5.5). Como o Estágio 5 só fica ``COMPLETED`` nesse
        caminho, o gate sequencial de :meth:`StageService.is_stage_startable`
        mantém o Estágio 6 bloqueado enquanto o Estágio 5 não concluir (Req. 6.2,
        6.4). A persistência do :class:`AmbienteProvisionado` como JSON portável
        (Req. 6.3) acompanha a conclusão na mesma transação.

    Attributes
    ----------
    caso_id:
        O Caso cujo provisionamento do Estágio 5 foi disparado.
    bloqueado_pelo_gate:
        ``True`` quando o Estágio 4 (Operacionalização) **não** está
        ``concluido``; o gate sequencial impede o provisionamento e **nada** é
        subido/persistido (Req. 1.1 — o Estágio 5 só inicia com o Estágio 4
        concluído).
    pronto:
        ``True`` sse o pipeline percorreu todas as etapas e os três
        pré-requisitos passaram (``ambiente.pronto``) — contêineres saudáveis,
        Abilities carregadas e isolamento aprovado (Req. 5.5). Quando ``True``, o
        ``StageRun`` do Estágio 5 é **concluído** (``concluido``/progresso 100)
        nesta chamada (task 5.3), o que — pelo gate sequencial — libera o Estágio
        6 (Req. 6.2, 6.4); quando ``False``, o Estágio 5 **não** conclui e o
        Estágio 6 permanece bloqueado.
    etapa_falha:
        A etapa que falhou (``"subir_conteineres"`` / ``"aguardar_saude"`` /
        ``"carregar_abilities"`` / ``"verificar_isolamento"``) ou ``None`` quando
        o pipeline concluiu sem falha (Req. 7.1).
    mensagem_erro:
        Causa legível (pt-BR) da falha da etapa, **derivada da última linha de
        erro** que a etapa que falhou deixou no buffer de logs — o mesmo texto
        que o terminal ao vivo/console da tela mostra —, garantindo por
        construção a consistência tela ↔ console (task 5.4 — Req. 7.1, 7.5). É o
        **mesmo** valor gravado em ``StageRun.mensagem_erro``. Recorre ao
        ``ambiente.mensagem_erro`` do provisionador quando a etapa não deixou
        linha de erro no buffer; ``None`` em sucesso.
    ambiente:
        A Saída tipada :class:`AmbienteProvisionado` do pipeline — presente em
        todos os caminhos que **rodaram** o provisionamento (mesmo em falha, com
        os resumos parciais e ``etapa_falha``); ``None`` apenas quando o gate
        bloqueou e o pipeline não rodou.
    persistencia_falhou:
        ``True`` quando a atualização do ``StageRun`` (``em_andamento`` ou
        ``erro``) ou a persistência da Saída (``AmbienteProvisionadoRow``) não
        pôde ser salva; o estado anterior foi preservado (rollback).
    reaproveitado:
        ``True`` quando o ambiente **já havia** sido provisionado com sucesso
        para o Caso e permanecia provisionado — uma :class:`AmbienteProvisionadoRow`
        ``pronto`` persistida **e** o ``StageRun`` do Estágio 5 já ``concluido`` —
        e, por isso, o provisionamento foi **reaproveitado** (Req. 1.6): a Saída
        tipada é reconstruída do JSON persistido, ``pronto=True``, **sem** subir
        um novo ambiente (``provisionador.provisionar`` não é chamado). ``False``
        em todos os demais caminhos (o pipeline rodou de fato).
    stage_run_id:
        O id do ``StageRun`` do Estágio 5 atualizado por esta chamada; ``None``
        quando o gate bloqueou ou a persistência falhou.
    """

    caso_id: str
    bloqueado_pelo_gate: bool = False
    pronto: bool = False
    etapa_falha: Optional[str] = None
    mensagem_erro: Optional[str] = None
    ambiente: Optional[AmbienteProvisionado] = None
    persistencia_falhou: bool = False
    reaproveitado: bool = False
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

# Mensagens pt-BR do provisionamento ativo do Estágio 5 (task 5.1).
_MSG_GATE_ESTAGIO5 = (
    "O Estágio 4 (Operacionalização) deste Caso ainda não está concluído; o "
    "Estágio 5 (Criação do ambiente de simulação) não é provisionado e permanece "
    "bloqueado."
)
_MSG_FALHA_PERSISTENCIA_ESTAGIO5 = (
    "O andamento do provisionamento do Estágio 5 não foi salvo: falha ao "
    "persistir na Base_de_Dados. O estado anterior foi preservado sem alteração."
)
_MSG_FALHA_PERSISTENCIA_SAIDA_ESTAGIO5 = (
    "A Saída do provisionamento do Estágio 5 não foi salva: falha ao persistir "
    "na Base_de_Dados. O estado anterior foi preservado sem alteração."
)

# Progresso agregado por etapa do pipeline (progresso por etapa — task 5.1).
# As quatro etapas do pipeline mapeiam para marcos de progresso crescentes. O
# marco é derivado da ``etapa_falha`` em falha (quanto a etapa chegou antes de
# falhar). Quando o pipeline percorre tudo e os três pré-requisitos passam
# (``ambiente.pronto``), o Estágio 5 conclui em 100 (task 5.3 — Req. 5.5); o
# caminho defensivo "percorreu tudo mas não ``pronto``" fica em 90 sem concluir.
_PROGRESSO_POR_ETAPA: dict[str, int] = {
    "subir_conteineres": 10,
    "aguardar_saude": 40,
    "carregar_abilities": 60,
    "verificar_isolamento": 80,
}

# Fluxos considerados "linha de erro" ao derivar a ``mensagem_erro`` do
# ``StageRun`` da última linha do terminal (task 5.4 — Req. 7.5). O executor de
# ``compose`` e as etapas do pipeline (``_verificar_saude``, ``carregar_abilities``,
# ``verificar_isolamento``) emitem a **causa** da falha como uma linha ``stderr``
# no buffer (o mesmo buffer que o console da tela renderiza). Aceitamos também
# ``erro`` por robustez — é o rótulo que a tela usa para colorir essas linhas —,
# de modo que a derivação acompanhe qualquer fluxo de erro efetivamente emitido.
_FLUXOS_DE_ERRO: tuple[str, ...] = ("stderr", "erro")


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
        log_buffer: Optional[ProvisioningLogBuffer] = None,
        params_provider: Optional[Callable[[str], ParametrosDeAmbiente]] = None,
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
        # Buffer de logs do provisionamento (task 5.1). Em produção, o
        # chamador/costura de wiring (task 6.x) injeta a **instância compartilhada
        # a nível de processo** — a mesma que o canal WebSocket lê (``ws/channels``,
        # task 4.3), de modo que encaminhar ``on_log`` ao buffer alimente o
        # terminal ao vivo da tela (Req. 2.2). Quando ``None``, cria um buffer
        # próprio fresco: o provisionamento ainda bufferiza (nada quebra), mas as
        # linhas não chegam ao canal compartilhado — útil nos testes que exercitam
        # o serviço de forma isolada e para chamadas sem terminal ao vivo.
        self._log_buffer: ProvisioningLogBuffer = (
            log_buffer if log_buffer is not None else ProvisioningLogBuffer()
        )
        # Provedor dos ``ParametrosDeAmbiente`` (Entrada do Estágio 5 — a Saída do
        # Estágio 4). Injetável nos testes para evitar tocar disco/costura de
        # operacionalização; em produção (``None``) a mesma via canônica que o
        # ``POST .../estagio/5/provisionar`` usa é resolvida **preguiçosamente**
        # em :meth:`_params_do_caso` (sem rede e sem IA — Req. 11.3, 11.4), de
        # modo que importar este módulo jamais toque a operacionalização.
        self._params_provider = params_provider

    @property
    def provisionador(self) -> ProvisionadorDeAmbiente:
        """O :class:`ProvisionadorDeAmbiente` usado para preparar/verificar."""
        return self._provisionador

    @property
    def log_buffer(self) -> ProvisioningLogBuffer:
        """O :class:`ProvisioningLogBuffer` que recebe as linhas do provisionamento.

        É o mesmo buffer do qual o canal WebSocket (``ws/channels``) lê para
        alimentar o terminal ao vivo da tela (Req. 2.2); em produção, a instância
        compartilhada a nível de processo é injetada no construtor.
        """
        return self._log_buffer

    def ambiente_persistido(self, caso: str) -> Optional[AmbienteProvisionado]:
        """Reconstrói a Saída :class:`AmbienteProvisionado` persistida do Caso, se houver.

        A leitura do Estágio 5 (``GET .../estagio/5``, task 6.1) usa este helper
        **read-only** para obter o :class:`~app.services.environment.saude.ResumoSaude`
        de um provisionamento anterior/corrente sem subir contêineres: carrega a
        última :class:`AmbienteProvisionadoRow` do Caso (JSON portável, Req. 6.3) e
        reconstrói o modelo via ``model_validate``. Devolve ``None`` quando ainda
        não há Saída persistida para o Caso — nesse caso a tela exibe um resumo de
        saúde vazio (não aprovado) sem provisionar (Req. 6.1).
        """
        row = self._ambiente_row(caso)
        if row is None:
            return None
        return AmbienteProvisionado.model_validate(row.conteudo)

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

    def provisionar_estagio5(self, caso: str) -> ProvisionamentoEstagio5Result:
        """Dispara o provisionamento **ativo** do Estágio 5 do Caso ``caso`` (task 5.1).

        Ao iniciar o Estágio 5 (Criação do ambiente de simulação), o Backend
        sobe o ambiente **por conta própria** — sem uma ação manual de
        "provisionar" (Req. 1.1). Esta orquestração conduz o pipeline de etapas
        da costura :class:`ProvisionadorDeAmbiente` (subir contêineres → aguardar
        saúde → carregar Abilities → verificar isolamento), encaminhando cada
        :class:`~app.services.environment.compose_executor.LinhaLog` emitida ao
        :class:`ProvisioningLogBuffer` compartilhado — que o canal WebSocket lê
        para alimentar o terminal ao vivo da tela (Req. 2.2) — e atualizando o
        ``StageRun`` de ``(caso, estagio=5)`` com o progresso por etapa e, em
        falha, ``etapa_falha`` + ``mensagem_erro`` (Req. 7.1).

        Fluxo:

        1. **Gate sequencial (Req. 1.1).** Se o Estágio 4 (Operacionalização)
           **não** está ``concluido``, não sobe nada e devolve
           ``bloqueado_pelo_gate=True`` — o Estágio 5 só inicia com o Estágio 4
           concluído, exatamente como :meth:`StageService.is_stage_startable`
           deriva.
        2. **Entrada do Estágio 5.** Resolve os
           :class:`~app.services.environment.operacionalizacao.ParametrosDeAmbiente`
           do Caso (a Saída do Estágio 4) pela mesma via canônica que o endpoint
           ``POST .../estagio/5/provisionar`` usa (Req. 1.2).
        3. **``em_andamento``.** Marca o ``StageRun`` do Estágio 5 como
           ``em_andamento`` (progresso 0, limpando ``etapa_falha``/``mensagem_erro``
           de uma execução anterior) e **limpa** o buffer de logs do Caso
           (``log_buffer.limpar``), de modo que uma execução fresca recomece a
           numeração de ``seq`` em 1 (separa o terminal ao vivo da execução
           anterior do da nova — Req. 2.5).
        4. **Pipeline em streaming.** Chama
           :meth:`ProvisionadorDeAmbiente.provisionar` passando um ``on_log`` que
           anexa cada :class:`LinhaLog` ao buffer (``log_buffer.anexar``) — o
           canal (task 4.3) lê o mesmo buffer, então as linhas chegam ao terminal
           da tela (Req. 2.2).
        5. **Atualização do ``StageRun`` conforme o veredito do pipeline
           (Req. 7.1).**

           * **Falha** (``ambiente.etapa_falha`` definido): ``estado = erro``,
             ``etapa_falha``/``mensagem_erro`` espelhando a Saída, progresso no
             marco da etapa que falhou — o Estágio 5 **não** conclui e o Estágio 6
             permanece bloqueado.
           * **Pronto** (``ambiente.pronto`` — os **três** pré-requisitos
             passaram: saúde + Abilities + isolamento, Req. 5.5): o ``StageRun``
             é **concluído** — ``estado = concluido``, progresso 100, limpando
             ``etapa_falha``/``mensagem_erro`` — e a Saída
             :class:`AmbienteProvisionado` é persistida como JSON portável na
             mesma transação (Req. 6.3). Só aqui o Estágio 5 passa a
             ``COMPLETED``, o que — pelo gate sequencial de
             :meth:`StageService.is_stage_startable` — libera o Estágio 6
             (Req. 6.2); enquanto o Estágio 5 não concluir, o Estágio 6 segue
             bloqueado (Req. 6.4).

           A atualização é transacional (um ``commit``); ``rollback`` em falha de
           persistência preserva o estado anterior e devolve
           ``persistencia_falhou=True``.

        Não levanta por caminhos esperados — a razão é sempre um sinal tipado no
        :class:`ProvisionamentoEstagio5Result`, como ``auto_concluir_estagio4``.
        """
        estados = self._estados_map(caso)

        # (1) Gate sequencial (Req. 1.1): o Estágio 5 só inicia com o Estágio 4
        # (Operacionalização) concluído. Enquanto não, não sobe/persiste nada.
        if estados.get(ESTAGIO_OPERACIONALIZACAO) is not StageState.COMPLETED:
            return ProvisionamentoEstagio5Result(
                caso_id=caso,
                bloqueado_pelo_gate=True,
                mensagem_erro=_MSG_GATE_ESTAGIO5,
            )

        # (1b) Idempotência (Req. 1.6): se o ambiente **já** foi provisionado com
        # sucesso para o Caso e permanece provisionado — há uma
        # :class:`AmbienteProvisionadoRow` ``pronto`` persistida **e** o
        # ``StageRun`` do Estágio 5 já está ``concluido`` —, **reaproveita** a
        # Saída sem subir um novo ambiente: reconstrói o
        # :class:`AmbienteProvisionado` do JSON persistido e devolve ``pronto=True``
        # **sem** chamar ``provisionador.provisionar`` nem limpar/re-rodar. O
        # ``StageRun`` concluído confirma que a conclusão (task 5.3) homologou os
        # três pré-requisitos — "ready+persisted" é o sinal da reutilização.
        if estados.get(ESTAGIO_AMBIENTE) is StageState.COMPLETED:
            row = self._ambiente_row(caso)
            if row is not None and row.pronto:
                ambiente = AmbienteProvisionado.model_validate(row.conteudo)
                run = self._stage_run_row(caso, ESTAGIO_AMBIENTE)
                return ProvisionamentoEstagio5Result(
                    caso_id=caso,
                    pronto=True,
                    ambiente=ambiente,
                    reaproveitado=True,
                    stage_run_id=(run.id if run is not None else None),
                )

        # (2) Entrada do Estágio 5: os ParametrosDeAmbiente (Saída do Estágio 4).
        params = self._params_do_caso(caso)

        # (3) Marca em_andamento e reinicia o buffer de logs do Caso para a nova
        # execução (seq recomeça em 1). Persiste o início transacionalmente.
        stage_run = self._get_or_create_stage_run(caso, estagio=ESTAGIO_AMBIENTE)
        stage_run.estado = StageState.IN_PROGRESS
        stage_run.progresso = 0
        stage_run.mensagem_erro = None
        stage_run.etapa_falha = None
        stage_run.atualizado_em = _utcnow()
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            return ProvisionamentoEstagio5Result(
                caso_id=caso,
                persistencia_falhou=True,
                mensagem_erro=_MSG_FALHA_PERSISTENCIA_ESTAGIO5,
            )

        self._log_buffer.limpar(caso)

        # (4) Pipeline em streaming. O ``on_log`` encaminha cada linha ao buffer
        # compartilhado — o canal WebSocket (task 4.3) lê o mesmo buffer e
        # alimenta o terminal ao vivo da tela (Req. 2.2).
        def _on_log(linha: LinhaLog) -> None:
            self._log_buffer.anexar(caso, linha)

        ambiente = self._provisionador.provisionar(params, on_log=_on_log)

        # Mensagem de falha exibida na tela/checklist e devolvida no resultado.
        # Em sucesso é ``None`` (``ambiente.mensagem_erro`` é ``None``); em falha é
        # **sobrescrita** no ramo abaixo pela última linha de erro do console
        # (task 5.4 — Req. 7.5), de modo que tanto o ``StageRun`` quanto o
        # :class:`ProvisionamentoEstagio5Result` carreguem o **mesmo** texto que o
        # terminal ao vivo mostra para a etapa que falhou.
        mensagem_erro_tela = ambiente.mensagem_erro

        # (5) Atualiza o StageRun conforme o veredito do pipeline (Req. 7.1, 5.5,
        # 6.2, 6.4).
        if ambiente.etapa_falha is not None:
            # Alguma etapa falhou ⇒ o Estágio 5 **não** conclui: ``erro`` com a
            # etapa/causa, progresso no marco da etapa que falhou. Como o Estágio
            # 5 não fica ``concluido``, o gate sequencial de
            # :meth:`StageService.is_stage_startable` mantém o Estágio 6 bloqueado
            # (``is_stage_startable(caso, 6)`` só é ``True`` com o Estágio 5
            # ``COMPLETED``) — Req. 6.4.
            stage_run.estado = StageState.ERROR
            stage_run.etapa_falha = ambiente.etapa_falha
            # Consistência tela ↔ console (task 5.4 — Req. 7.5): deriva a
            # ``mensagem_erro`` do ``StageRun`` da **última linha de erro** que a
            # etapa que falhou deixou no mesmo buffer que o console renderiza
            # (alimentado por ``_on_log`` acima), garantindo por construção que o
            # checklist/tela mostre exatamente o texto do terminal. Recorre ao
            # ``ambiente.mensagem_erro`` (já computado pelo provisionador) quando a
            # etapa não deixou linha de erro no buffer, de modo que nada regrida.
            mensagem_erro_tela = self._mensagem_da_ultima_linha_de_erro(
                caso, ambiente.etapa_falha, fallback=ambiente.mensagem_erro
            )
            stage_run.mensagem_erro = mensagem_erro_tela
            stage_run.progresso = _PROGRESSO_POR_ETAPA.get(ambiente.etapa_falha, 0)
        elif ambiente.pronto:
            # Os **três** pré-requisitos passaram — ``ambiente.pronto`` encode
            # exatamente ``etapa_falha is None`` **e** saúde aprovada **e**
            # Abilities carregadas **e** isolamento aprovado (Req. 5.5): conclui o
            # Estágio 5 (``concluido``/progresso 100, limpando
            # ``etapa_falha``/``mensagem_erro``). Só aqui o Estágio 5 passa a
            # ``COMPLETED``, o que — pelo gate sequencial — libera o Estágio 6
            # (Req. 6.2, 6.4).
            stage_run.estado = StageState.COMPLETED
            stage_run.etapa_falha = None
            stage_run.mensagem_erro = None
            stage_run.progresso = 100
        else:
            # Caminho defensivo: o pipeline percorreu todas as etapas sem
            # ``etapa_falha`` mas ``pronto`` é ``False`` (algum pré-requisito não
            # homologou sem registrar etapa de falha). Não concluímos — mantém
            # ``em_andamento`` com progresso avançado, de modo que o Estágio 6
            # permaneça bloqueado até os três pré-requisitos passarem (Req. 6.4).
            stage_run.estado = StageState.IN_PROGRESS
            stage_run.etapa_falha = None
            stage_run.mensagem_erro = None
            stage_run.progresso = 90
        stage_run.atualizado_em = _utcnow()

        # (5b) Persistência da Saída (Req. 6.3): quando o pipeline fica **pronto**
        # (os três pré-requisitos passaram), persiste o
        # :class:`AmbienteProvisionado` como JSON portável associado ao Caso —
        # upsert *latest-row-wins* numa única linha por Caso — de modo que o
        # Estágio 6 e a tela consumam a Saída após a conclusão e a idempotência
        # (passo 1b) a reaproveite. É feito na **mesma transação** que o
        # ``StageRun`` (um único ``commit``); um ``rollback`` em falha de
        # persistência preserva o estado anterior.
        if ambiente.pronto:
            self._persistir_ambiente(caso, ambiente)

        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            return ProvisionamentoEstagio5Result(
                caso_id=caso,
                pronto=ambiente.pronto,
                etapa_falha=ambiente.etapa_falha,
                mensagem_erro=mensagem_erro_tela,
                ambiente=ambiente,
                persistencia_falhou=True,
            )

        self._session.refresh(stage_run)
        return ProvisionamentoEstagio5Result(
            caso_id=caso,
            pronto=ambiente.pronto,
            etapa_falha=ambiente.etapa_falha,
            mensagem_erro=mensagem_erro_tela,
            ambiente=ambiente,
            stage_run_id=stage_run.id,
        )

    # Alias de intenção: o provisionamento do Estágio 5 é **iniciado** sem ação
    # manual (Req. 1.1). Expõe o mesmo comportamento com um nome que lê melhor na
    # costura de wiring que o dispara ao entrar no Estágio 5.
    def iniciar_provisionamento(self, caso: str) -> ProvisionamentoEstagio5Result:
        """Alias de :meth:`provisionar_estagio5` — dispara o provisionamento (Req. 1.1)."""
        return self.provisionar_estagio5(caso)

    def _mensagem_da_ultima_linha_de_erro(
        self, caso: str, etapa: str, *, fallback: Optional[str] = None
    ) -> Optional[str]:
        """Deriva a ``mensagem_erro`` do ``StageRun`` da última linha de erro do console (task 5.4 — Req. 7.5).

        Garante por construção a **consistência tela ↔ console** (Req. 7.5): a
        mensagem de falha que o checklist/tela exibe (``StageRun.mensagem_erro``)
        é o **texto da própria linha** que o terminal ao vivo mostra para a etapa
        que falhou — e não um texto derivado em paralelo que poderia divergir.

        Lê o buffer de logs corrente do Caso (:meth:`ProvisioningLogBuffer.ler` —
        o **mesmo** buffer que o canal WebSocket/console renderiza, alimentado por
        ``on_log`` durante o pipeline), filtra as linhas emitidas pela ``etapa``
        que falhou e devolve o ``texto`` da **última** linha de erro
        (``fluxo in`` :data:`_FLUXOS_DE_ERRO`). O executor de ``compose`` e todas
        as etapas do pipeline emitem a causa da falha como uma linha ``stderr`` no
        buffer, de modo que a última linha de erro da etapa carrega exatamente a
        causa que o console exibe.

        Fail-safe (defensivo): quando a etapa que falhou **não** deixou nenhuma
        linha de erro no buffer (p.ex. um buffer isolado que o canal não alimentou,
        ou uma etapa sem ``stderr``), devolve o ``fallback`` — tipicamente o
        ``ambiente.mensagem_erro`` já computado pelo provisionador — para que nada
        regrida: a mensagem continua presente, apenas não comprovadamente idêntica
        à linha do console.

        Parameters
        ----------
        caso:
            O Caso cujo buffer de logs é lido.
        etapa:
            A etapa que falhou (``ambiente.etapa_falha``) — ``"subir_conteineres"``
            / ``"aguardar_saude"`` / ``"carregar_abilities"`` /
            ``"verificar_isolamento"``.
        fallback:
            Mensagem usada quando a etapa não deixou linha de erro no buffer
            (default ``None``) — normalmente ``ambiente.mensagem_erro``.

        Returns
        -------
        Optional[str]
            O ``texto`` da última linha de erro da etapa no buffer, ou ``fallback``
            quando não há nenhuma.
        """
        ultima_erro: Optional[str] = None
        for linha in self._log_buffer.ler(caso):
            if linha.etapa == etapa and linha.fluxo in _FLUXOS_DE_ERRO:
                ultima_erro = linha.texto
        return ultima_erro if ultima_erro is not None else fallback

    def _params_do_caso(self, caso: str) -> ParametrosDeAmbiente:
        """Resolve os :class:`ParametrosDeAmbiente` do Caso (Entrada do Estágio 5).

        Quando um ``params_provider`` é injetado (testes/costura), delega a ele.
        Em produção (``None``), resolve pela **mesma via canônica** que o endpoint
        ``POST .../estagio/5/provisionar`` usa — o DAG curado (Saída do Estágio 3)
        operacionalizado por :class:`CuradoriaOperacionalizador` — importada
        **preguiçosamente** aqui para não acoplar o import deste módulo à costura
        de operacionalização nem tocar disco/rede no import. Sem rede e sem IA em
        nenhum caminho (Req. 11.3, 11.4).
        """
        if self._params_provider is not None:
            return self._params_provider(caso)

        # Import local: evita acoplar o import deste módulo à costura de
        # operacionalização e mantém o import barato/sem efeitos colaterais.
        from app.services.case_service import CaseService
        from app.services.dag.dag_service import DagService
        from app.services.environment.operacionalizador import (
            CuradoriaOperacionalizador,
        )
        from app.services.stage.stage_service import StageService

        case_service = CaseService()
        stage_service = StageService(self._session, case_service=case_service)
        dag = DagService(session=self._session, case_service=case_service).obter_dag(
            caso
        )
        operacionalizador = CuradoriaOperacionalizador(
            stage_service=stage_service, case_service=case_service
        )
        return operacionalizador.operacionalizar(caso, dag).parametros

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

    def _ambiente_row(self, caso_id: str) -> Optional[AmbienteProvisionadoRow]:
        """Última :class:`AmbienteProvisionadoRow` persistida de ``caso_id``, se houver.

        A Saída do Estágio 5 é persistida como JSON portável associado ao Caso
        (Req. 6.3). A idempotência (Req. 1.6) lê esta linha para reaproveitar o
        ambiente pronto sem subir um novo. Havendo mais de uma linha para o mesmo
        Caso (não deveria, dado o upsert *latest-row-wins*), a de maior ``id``
        (mais recente) prevalece — mesmo padrão de ``_stage_run_row``.
        """
        stmt = (
            select(AmbienteProvisionadoRow)
            .where(AmbienteProvisionadoRow.caso_id == caso_id)
            .order_by(AmbienteProvisionadoRow.id.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return rows[-1] if rows else None

    def _persistir_ambiente(
        self, caso_id: str, ambiente: AmbienteProvisionado
    ) -> AmbienteProvisionadoRow:
        """Faz *upsert* da Saída do Estágio 5 como JSON portável do Caso (Req. 6.3).

        Serializa o :class:`AmbienteProvisionado` via ``model_dump(mode="json")``
        — a forma portável que ``AmbienteProvisionado.model_validate`` reconstrói
        na idempotência (passo 1b) — e grava ``plataforma``/``origem`` como enums
        portáveis e ``pronto`` como o veredito de homologação. Mantém **uma única
        linha por Caso** (upsert *latest-row-wins*, mesmo contrato de upsert do
        ``StageRun``): atualiza a linha existente quando há uma, cria quando não há.

        A linha é apenas **adicionada/atualizada** na ``Session``; o ``commit`` é
        do chamador (``provisionar_estagio5``), de modo que a persistência da
        Saída e a do ``StageRun`` compartilhem a **mesma transação** — um
        ``rollback`` em falha preserva o estado anterior sem mutação parcial.
        """
        agora = _utcnow()
        conteudo = ambiente.model_dump(mode="json")
        plataforma = PlataformaAmbientePersistida(ambiente.plataforma.value)

        row = self._ambiente_row(caso_id)
        if row is None:
            row = AmbienteProvisionadoRow(caso_id=caso_id, criado_em=agora)
            self._session.add(row)
        row.plataforma = plataforma
        row.conteudo = conteudo
        row.origem = ambiente.origem
        row.pronto = ambiente.pronto
        row.atualizado_em = agora
        return row

    def _get_or_create_stage_run(self, caso_id: str, *, estagio: int) -> StageRun:
        """Devolve o ``StageRun`` de ``(caso_id, estagio)``, criando-o se ausente.

        Uma linha por ``(caso_id, estagio)`` (mesmo contrato de upsert de
        ``StageService`` / ``SessionStateService`` / ``DagService``), de modo que
        a conclusão atualiza a execução existente em vez de acumular linhas.

        Havendo mais de uma linha para o mesmo ``(caso_id, estagio)`` (resíduo de
        execuções concorrentes), a de **maior ``id``** (mais recente) prevalece —
        *latest-row-wins*, consistente com :meth:`_estados_map` e
        :meth:`_stage_run_row` (que a leitura de estado usa), de modo que a
        atualização recaia sobre a mesma linha que a UI lê.
        """
        stmt = (
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == estagio)
            .order_by(StageRun.id.desc())
        )
        stage_run = self._session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(caso_id=caso_id, estagio=estagio)
            self._session.add(stage_run)
        return stage_run


__all__ = [
    "EnvironmentStageService",
    "ProvisionamentoAutoResult",
    "ProvisionamentoEstagio5Result",
]
