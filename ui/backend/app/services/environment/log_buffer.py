"""Buffer de logs de provisionamento por caso (Estágio 5).

Este módulo define :class:`ProvisioningLogBuffer` — o **buffer em memória**, por
caso, das linhas de log do provisionamento do Estágio 5. É a estrutura que o
:class:`~app.services.environment.stage_service.EnvironmentStageService` (tasks
5.1/5.2) alimenta a cada :class:`~app.services.environment.compose_executor.LinhaLog`
emitida pelo executor e pelas etapas do pipeline, e da qual o canal WebSocket
(``ws/channels.py``, task 4.3) e os endpoints REST (task 6.x) leem para:

- **acompanhamento ao vivo** — o canal envia as linhas novas desde o último frame
  (delta por ``seq``) e, no connect, o buffer completo (Req. 2.5);
- **recuperação por reconexão / tela aberta tarde** — o ``GET .../logs`` e o
  connect devolvem o buffer completo da execução corrente, de modo que a tela
  reconstrua o histórico sem perder linhas (Req. 2.7).

Por design é **efêmero** (em memória, por processo): serve ao acompanhamento ao
vivo e à reconexão da execução corrente (Req. 2.5, 2.7), **não** a um histórico
persistente de auditoria — a auditoria por comando da *execução* (Estágio 6, via
``AuditLogEntry``) está fora do escopo desta spec.

Decisões de design
------------------

**``seq`` monotônico por caso, começando em 1.** Cada caso tem seu próprio
contador. A primeira linha anexada recebe ``seq == 1``; cada anexo seguinte
incrementa. O ``seq`` **nunca é reusado**, mesmo quando a lista é aparada (trim)
ao exceder o limite: o contador segue avançando independentemente do tamanho
corrente da lista. Isso dá à tela uma chave estável para deduplicar/reordenar as
linhas na reconexão (Req. 2.5) — o cliente só precisa guardar o maior ``seq``
visto e pedir ``novas_desde`` esse valor.

**Lista limitada (bounded).** Para não crescer sem limite num processo de vida
longa, cada caso guarda no máximo :data:`DEFAULT_MAX_LINHAS` linhas (default
``2000``, configurável no construtor). Ao exceder, as linhas **mais antigas** são
descartadas (FIFO) — mas o ``seq`` segue monotônico, então o cliente percebe o
"salto" e sabe que linhas antigas foram aparadas (comportamento aceitável para um
terminal ao vivo; o histórico de longo prazo não é responsabilidade deste buffer).

**Carimbo de tempo naive-UTC no anexo.** A :class:`LinhaLog` do executor **não**
carrega timestamp (desacoplamento costura↔transporte). É o buffer que carimba o
``em`` (naive-UTC) ao anexar, materializando a :class:`LinhaLogView`. O relógio é
**injetável** (``clock``) e tem como default o mesmo helper naive-UTC do serviço
(:func:`datetime.now(UTC).replace(tzinfo=None)`), portável entre PostgreSQL e o
SQLite de teste e controlável pelos testes.

**Thread-safe o suficiente para o processo único do FastAPI.** Um
:class:`threading.Lock` leve guarda cada mutação/leitura, de modo que o anexo
disparado pela task de provisionamento e as leituras do canal/endpoints não
corram entre si. As leituras devolvem **cópias** (listas novas de objetos
imutáveis), de modo que o chamador nunca mute o buffer por referência.

_Requisitos: 2.5, 2.7_
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Callable, Optional

from app.services.environment.compose_executor import LinhaLog
from app.services.stage.stage_service import LinhaLogView

# Limite default de linhas por caso. Mantém o buffer de uma execução ao vivo numa
# faixa de memória previsível num processo de vida longa; ao exceder, as linhas
# mais antigas são aparadas (o ``seq`` segue monotônico). Efêmero por design — não
# é um histórico de auditoria (Req. 2.5, 2.7).
DEFAULT_MAX_LINHAS: int = 2000


def _utcnow() -> dt.datetime:
    """Timestamp UTC *naive* (portável entre PostgreSQL e o SQLite de teste).

    Mesmo contrato do helper homônimo de
    :mod:`app.services.environment.stage_service`: ``datetime.now(UTC)`` com o
    ``tzinfo`` removido, de modo que o ``em`` da :class:`LinhaLogView` seja
    naive-UTC e comparável de forma consistente em qualquer backend de banco.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class ProvisioningLogBuffer:
    """Buffer em memória, por caso, das linhas de log do provisionamento (Req. 2.5, 2.7).

    Mantém, para cada ``caso_id``, uma lista **limitada** de :class:`LinhaLogView`
    em ordem de ``seq`` monotônico. Alimentado pelo
    :class:`~app.services.environment.stage_service.EnvironmentStageService` a cada
    :class:`LinhaLog` emitida (via :meth:`anexar`), e lido pelo canal WebSocket e
    pelos endpoints REST (via :meth:`ler` para o buffer completo e
    :meth:`novas_desde` para o delta de reconexão).

    É **efêmero** (em memória, por processo) — serve ao acompanhamento ao vivo e à
    reconexão da execução corrente, não a um histórico persistente (Req. 2.5, 2.7).

    Parameters
    ----------
    max_linhas:
        Número máximo de linhas retidas por caso (default
        :data:`DEFAULT_MAX_LINHAS`). Ao exceder, as linhas mais antigas são
        descartadas (FIFO); o ``seq`` segue monotônico. Deve ser ``>= 1``.
    clock:
        Callable sem argumentos que devolve o carimbo de tempo (naive-UTC)
        atribuído a ``em`` no anexo. Default: :func:`_utcnow`. Injetável nos
        testes para controlar o tempo.
    """

    def __init__(
        self,
        *,
        max_linhas: int = DEFAULT_MAX_LINHAS,
        clock: Optional[Callable[[], dt.datetime]] = None,
    ) -> None:
        if max_linhas < 1:
            raise ValueError("max_linhas deve ser >= 1")
        self._max_linhas = max_linhas
        self._clock: Callable[[], dt.datetime] = clock if clock is not None else _utcnow
        self._lock = threading.Lock()
        # caso_id -> lista limitada de LinhaLogView (em ordem de seq).
        self._buffers: dict[str, list[LinhaLogView]] = {}
        # caso_id -> maior seq já atribuído (nunca reusado, mesmo após trim).
        self._seq: dict[str, int] = {}

    def anexar(self, caso_id: str, linha: LinhaLog) -> LinhaLogView:
        """Anexa uma :class:`LinhaLog` do executor ao buffer do caso (Req. 2.5).

        Atribui o próximo ``seq`` monotônico do caso (começando em ``1``) e o
        carimbo de tempo naive-UTC do ``clock``, materializando a
        :class:`LinhaLogView` armazenada. Ao exceder :attr:`max_linhas`, a linha
        mais antiga é descartada (FIFO) — mas o ``seq`` **não** é reusado.

        Parameters
        ----------
        caso_id:
            O caso cujo buffer recebe a linha.
        linha:
            A :class:`LinhaLog` emitida pelo executor/etapa (``etapa``/``fluxo``/
            ``texto``, sem timestamp).

        Returns
        -------
        LinhaLogView
            A linha materializada e armazenada (com ``seq`` e ``em`` atribuídos).
        """
        with self._lock:
            proximo = self._seq.get(caso_id, 0) + 1
            self._seq[caso_id] = proximo
            view = LinhaLogView(
                seq=proximo,
                etapa=linha.etapa,
                fluxo=linha.fluxo,
                texto=linha.texto,
                em=self._clock(),
            )
            buffer = self._buffers.setdefault(caso_id, [])
            buffer.append(view)
            # Apara as mais antigas ao exceder o limite (FIFO); seq segue avançando.
            excesso = len(buffer) - self._max_linhas
            if excesso > 0:
                del buffer[:excesso]
            return view

    def ler(self, caso_id: str) -> list[LinhaLogView]:
        """Devolve o buffer completo corrente do caso, em ordem de ``seq`` (Req. 2.5, 2.7).

        Usado no connect do WebSocket e no ``GET .../logs`` para recuperar o
        histórico da execução corrente. Devolve uma **cópia** da lista (de objetos
        imutáveis), de modo que o chamador nunca mute o buffer por referência.
        Caso ``caso_id`` não tenha linhas, devolve uma lista vazia.
        """
        with self._lock:
            return list(self._buffers.get(caso_id, ()))

    def novas_desde(self, caso_id: str, seq: int) -> list[LinhaLogView]:
        """Devolve as linhas do caso com ``seq`` estritamente maior que ``seq`` (Req. 2.5, 2.7).

        É o delta usado pelo canal para enviar apenas as linhas novas desde o
        último frame (o cliente guarda o maior ``seq`` visto). Devolve uma lista
        nova, em ordem de ``seq``; vazia quando não há linhas mais recentes. Passar
        ``seq=0`` equivale a :meth:`ler` (todas as linhas correntes).

        Parameters
        ----------
        caso_id:
            O caso de onde ler o delta.
        seq:
            O maior ``seq`` já conhecido pelo chamador; só linhas com ``seq``
            maior são devolvidas.
        """
        with self._lock:
            buffer = self._buffers.get(caso_id, ())
            return [linha for linha in buffer if linha.seq > seq]

    def limpar(self, caso_id: str) -> None:
        """Reseta o buffer do caso quando um novo provisionamento começa.

        Descarta as linhas acumuladas **e** o contador de ``seq`` do caso, de modo
        que a próxima execução recomece a numeração em ``1``. Chamado pelo serviço
        ao disparar um provisionamento fresco (tasks 5.1/5.2), separando o terminal
        ao vivo da execução anterior do da nova. Idempotente: limpar um caso sem
        linhas é um no-op.
        """
        with self._lock:
            self._buffers.pop(caso_id, None)
            self._seq.pop(caso_id, None)


# ---------------------------------------------------------------------------
# Provedor compartilhado a nível de processo (Estágio 5 — tasks 5.1/4.3/6.x)
# ---------------------------------------------------------------------------
#
# O buffer de logs do provisionamento precisa ser **uma única instância por
# processo**, compartilhada entre três consumidores:
#
# * o :class:`~app.services.environment.stage_service.EnvironmentStageService`,
#   que **anexa** cada :class:`LinhaLog` emitida pelo pipeline (task 5.1);
# * o canal WebSocket de Estágios (``app.ws.channels.run_stage_progress_channel``),
#   que **lê** o buffer para alimentar o terminal ao vivo da tela (task 4.3); e
# * os endpoints REST do Estágio 5 (``GET .../estagio/5`` e ``.../estagio/5/logs``),
#   que **leem** o buffer corrente (task 6.x).
#
# Como as três costuras rodam no mesmo processo FastAPI, basta um *singleton* de
# módulo. :func:`get_log_buffer` é a dependência que o roteador WS e os endpoints
# resolvem; os testes a **sobrescrevem** (via ``app.dependency_overrides`` ou
# substituindo ``_LOG_BUFFER``) para injetar um buffer isolado e determinístico.
_LOG_BUFFER: ProvisioningLogBuffer = ProvisioningLogBuffer()


def get_log_buffer() -> ProvisioningLogBuffer:
    """Devolve o :class:`ProvisioningLogBuffer` compartilhado a nível de processo.

    É a **mesma** instância que o :class:`EnvironmentStageService` alimenta
    (anexa cada :class:`LinhaLog` do pipeline), que o canal WebSocket de Estágios
    lê para o terminal ao vivo (Req. 2.2) e que os endpoints REST do Estágio 5
    leem para devolver o buffer corrente (Req. 2.5, 2.7). Em produção devolve o
    *singleton* de módulo :data:`_LOG_BUFFER`; os testes **sobrescrevem** esta
    dependência (``dependency_overrides``) para injetar um buffer isolado.
    """
    return _LOG_BUFFER


__all__ = [
    "ProvisioningLogBuffer",
    "DEFAULT_MAX_LINHAS",
    "get_log_buffer",
]
