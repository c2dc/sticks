"""DagService — DAG loading, acyclic Mermaid and curado/stub distinction.

This service powers the explicit DAG Generation/Visualization phase of the new
flow (``GET /api/casos/{caso}/dag`` — Req. 5, 6). For a **curated** case it
reads the already-produced structural graph in ``data/dag/{slug}_dag.json`` and
assembles a (acyclic-by-construction) Mermaid ``flowchart`` the frontend hands
straight to the existing ``MermaidDiagram`` (``securityLevel: "strict"``). For a
**non-curated** slug it returns a "passa-direto" availability stub — **without
ever invoking any AI service** (there is no AI client on this path — Req. 9.1,
9.2). A curated case whose DAG is missing/malformed yields a typed error state
(``curado=True, pode_avancar=False`` — Req. 5.3, 6.4).

Design alignment
----------------
* Path resolution and typed parsing are **reused** from
  :class:`~app.services.case_service.CaseService`: ``dag_path(slug)`` locates
  ``data/dag/{slug}_dag.json`` and the service's typed parsing produces
  :class:`~app.services.case_service.DagData` / :class:`~app.services.case_service.DagNode`
  (the same ``{slug}_dag.json`` the StageService already loads). The DAG service
  never re-locates files or re-implements JSON parsing.
* The curated slugs are taken from :data:`~app.services.case_service.CURATED_CASES`
  (via ``CaseService``) — the 8 slugs are **not** hardcoded here.
* Mermaid assembly mirrors the style of ``app/api/topologia.py::_mermaid``: one
  node per structural node and one edge per edge entry, ``flowchart TD``, no
  embedded scripts. Implemented as a pure function in task 7.2.

DAG edges
---------
The design contract models edges as a list of ``{from, to}`` pairs keyed by
``node_id`` (see the ``GET /api/casos/{caso}/dag`` JSON, field ``arestas`` with
pt-BR keys ``de``/``para``). However, the real ``{slug}_dag.json`` files express
their relations via ``parent_nodes`` / ``child_nodes`` on each
``structural_node`` rather than a top-level ``edges`` array. The actual edge
derivation — turning those ``parent_nodes``/``child_nodes`` relations into the
``{from, to}`` pairs **without introducing extra edges** (preserving the
acyclicity guaranteed by ``metadata.validation.is_dag``) — is the subject of
task 7.2 (``montar_mermaid``) and task 7.3 (``obter_dag``). This skeleton only
defines the models and signatures.

The pure read path only ever **reads** from ``sticks/`` (through
``CaseService``) and performs **no network access**. The persistence path
(``sincronizar_dags_curados``/``listar_dags``/``escolher_dag``) uses the
injected :class:`~sqlalchemy.orm.Session`.

Escolha de DAG — conclusão do Estágio 2 (task 5.2)
--------------------------------------------------
:meth:`DagService.escolher_dag` grava o ``dag_escolhido_id`` no ``StageRun`` do
Estágio 2 (Geração do DAG) do Caso e o conclui (progresso 100), liberando o
Estágio 3 (Req. 6.3), validando que o ``dag_id`` pertence ao Caso (recusa tipada
``DAG_NAO_ENCONTRADO`` → 404) e que a Campanha tem ao menos um DAG persistido
(recusa tipada ``SEM_DAG`` → 409, Req. 6.2). É idempotente (atribuição absoluta
por upsert do ``StageRun`` de ``estagio=2``) e transacional (rollback preserva o
estado anterior em falha de persistência, como ``SessionStateService``).

Conclusão automática do Estágio 2 (task 5.1)
--------------------------------------------
No esquema de cinco Estágios o DAG (Estágio 2) é **derivado** da Modelagem
Estrutural (Estágio 1): a costura ``GeradorDeDag.gerar(caso, modelagem)`` passa a
receber a Modelagem (Req. 3.1, 10.1). Para um Caso curado,
:meth:`DagService.auto_concluir_estagio2` — espelhando
:meth:`~app.services.environment.stage_service.EnvironmentStageService.auto_concluir_estagio4`
— respeita o gate sequencial (Estágio 1 precisa estar ``concluido`` — Req. 9.3),
é idempotente, roda o backfill curado (``sincronizar_dags_curados``, que agora
passa a Modelagem ao gerador) e, havendo ao menos um DAG derivável persistido,
marca o ``StageRun`` de ``estagio=2`` ``concluido``. Sem DAG derivável o Estágio 2
permanece não concluído (botão "Gerar DAG" desabilitado, avanço bloqueado —
Req. 9.4).

_Requisitos: 3.2, 3.3, 3.4, 3.5, 4.7, 5.1, 5.2, 6.2, 6.3, 8.3, 9.1, 9.3, 9.4_
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import (
    Case as CaseModel,
    Dag as DagModel,
    StageRun,
)
from app.models.enums import OrigemDag, StageState
from app.services.case_service import (
    CaseLoadErrorType,
    CaseService,
    DagData,
)
from app.services.stage.stage_service import (
    ESTAGIO_GERACAO_DAG,
    ESTAGIO_MODELAGEM,
    StageService,
)

if TYPE_CHECKING:  # pragma: no cover - import only for typing (avoids a cycle)
    from app.services.dag.gerador import GeradorDeDag, ModelagemEstrutural


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite).

    Matches the rest of the backend (``StageService``/``SessionStateService``)
    so ``criado_em`` round-trips identically on both the product PostgreSQL and
    the ephemeral test SQLite.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# Characters that are safe inside a Mermaid node id. Anything else in a
# ``node_id`` is replaced with ``_`` before the ``n_`` prefix is applied, so the
# resulting id is always a valid Mermaid identifier (deterministic, no scripts).
_UNSAFE_NODE_ID_CHARS = re.compile(r"[^A-Za-z0-9_]")


# ---------------------------------------------------------------------------
# Typed response models (pt-BR field names per the design contract for
# GET /api/casos/{caso}/dag)
# ---------------------------------------------------------------------------


class DagNoView(BaseModel):
    """One node of the DAG as shown in the ``GET /api/casos/{caso}/dag`` payload.

    Mirrors the ``nos`` entries of the design JSON: the structural node's
    ``node_id`` plus its ATT&CK technique identity (``technique_id``,
    ``technique_name``, ``tactic``).
    """

    node_id: str
    technique_id: Optional[str] = None
    technique_name: Optional[str] = None
    tactic: Optional[str] = None


class DagArestaView(BaseModel):
    """One directed edge of the DAG (``arestas`` entry of the design JSON).

    The field names are the pt-BR keys ``de``/``para`` from the contract
    (``"arestas": [{ "de": "…", "para": "…" }]``): ``de`` is the source node_id,
    ``para`` the target node_id.
    """

    de: str
    para: str


class DagView(BaseModel):
    """The DAG view returned by ``GET /api/casos/{caso}/dag`` (Req. 5, 6).

    Covers the three states of the contract:

    * **Curated** valid case — ``curado=True``, ``pode_avancar=True``, with
      ``nos``/``arestas`` filled and ``mermaid`` assembled.
    * **Non-curated** case — "passa-direto": ``curado=False``,
      ``pode_avancar=False``, ``indisponivel=True`` and a ``mensagem`` (no AI is
      ever invoked — Req. 5.4, 5.5, 9.1, 9.2).
    * **Curated but missing/malformed** DAG — typed error: ``curado=True``,
      ``pode_avancar=False`` with an ``erro`` message (Req. 5.3, 6.4).
    """

    caso_id: str
    nome: str
    curado: bool
    pode_avancar: bool
    nos: list[DagNoView] = Field(default_factory=list)
    arestas: list[DagArestaView] = Field(default_factory=list)
    mermaid: str = ""
    # Stub / error fields (populated only in the non-curated or error states).
    indisponivel: bool = False
    mensagem: Optional[str] = None
    erro: Optional[str] = None


class DagResumo(BaseModel):
    """Resumo de um DAG persistido, para a listagem de uma Campanha (Req. 4.8).

    Espelha as colunas de identidade da entidade :class:`~app.models.domain.Dag`
    expostas no contrato ``GET /api/casos/{caso}/dags`` — ``id``, ``origem``,
    ``identificador``/versão, ``rotulo`` e ``criado_em`` — sem carregar o
    ``conteudo`` gráfico (que é obtido por ``obter_dag_persistido``). ``origem``
    é a :class:`~app.models.enums.OrigemDag` persistida (``curadoria_humana`` ou,
    no futuro, ``gerado_ia``).
    """

    id: int
    caso_id: str
    origem: OrigemDag
    identificador: str
    rotulo: Optional[str] = None
    criado_em: dt.datetime


class DagDetalhe(BaseModel):
    """Grafo + Mermaid de um DAG persistido (``GET /api/casos/{caso}/dags/{dag_id}``).

    Espelha a forma gráfica de :class:`DagView` (``nos``/``arestas``/``mermaid``,
    entregues direto ao ``MermaidDiagram`` com ``securityLevel: "strict"``) e
    acrescenta a identidade persistida do DAG (``dag_id``, ``origem``,
    ``identificador``, ``rotulo``), exatamente como o contrato do design
    ("Obter o grafo + Mermaid de um DAG" — Req. 4.4) e o wrapper de frontend
    ``getDagPorId`` (tipo ``DagDetalhe``) esperam. O ``conteudo`` gráfico é
    reconstruído a partir do ``Dag.conteudo`` persistido (``structural_nodes`` +
    ``arestas``), e o ``mermaid`` é montado de forma consistente com
    ``obter_dag`` (mesmo ``montar_mermaid``/``_derivar_arestas``).
    """

    caso_id: str
    dag_id: int
    origem: OrigemDag
    identificador: str
    rotulo: Optional[str] = None
    nos: list[DagNoView] = Field(default_factory=list)
    arestas: list[DagArestaView] = Field(default_factory=list)
    mermaid: str = ""


# ---------------------------------------------------------------------------
# Escolha de DAG para a execução (conclui o Estágio 1) — Req. 4.7, 6.2, 6.3
# ---------------------------------------------------------------------------

# O número do Estágio "Geração do DAG" é a fonte única de verdade de
# ``stage_service`` (``ESTAGIO_GERACAO_DAG == 2`` após a renumeração para cinco
# Estágios — Req. 1.1); importado acima em vez de redeclarado como literal local.
# A escolha grava ``dag_escolhido_id`` no ``StageRun`` deste Estágio e o conclui;
# a conclusão automática (``auto_concluir_estagio2``) marca o mesmo ``StageRun``
# ``concluido`` quando há DAG curado derivável, liberando o Estágio 3 pelo gate
# sequencial.


class EscolhaDagFalha(str, enum.Enum):
    """Motivo tipado de uma escolha de DAG recusada (Req. 6.2, 6.3).

    A API traduz cada motivo em um código HTTP (ver ``escolher_dag``):

    * ``SEM_DAG`` → 409 Conflict: a Campanha não possui nenhum DAG persistido,
      logo o avanço para além do Estágio 2 permanece bloqueado (Req. 6.2).
    * ``DAG_NAO_ENCONTRADO`` → 404 Not Found: o ``dag_id`` não existe ou não
      pertence ao Caso (Req. 6.3 — erro); o ``StageRun`` do Estágio 2 **não** é
      alterado.
    * ``FALHA_PERSISTENCIA`` → falha ao persistir a escolha; o estado
      anteriormente persistido é preservado sem alteração (padrão transacional
      de ``SessionStateService.persist_stage_completion``).
    """

    SEM_DAG = "sem_dag"
    DAG_NAO_ENCONTRADO = "dag_nao_encontrado"
    FALHA_PERSISTENCIA = "falha_persistencia"


# Mensagens pt-BR por motivo de recusa, para a API/UI surfacearem (Req. 6.2, 6.3).
_MENSAGEM_SEM_DAG = (
    "A Campanha não possui nenhum DAG persistido; o avanço para além do "
    "Estágio 2 (Geração do DAG) permanece bloqueado."
)
_MENSAGEM_DAG_NAO_ENCONTRADO = (
    "O DAG escolhido não existe ou não pertence a esta Campanha."
)
_MENSAGEM_FALHA_PERSISTENCIA = (
    "A escolha do DAG não foi salva: falha ao persistir na Base_de_Dados. "
    "O Estágio 2 foi preservado sem alteração."
)


class EscolhaDagResult(BaseModel):
    """Resultado tipado de :meth:`DagService.escolher_dag` (Req. 4.7, 6.2, 6.3).

    Em vez de levantar exceções, ``escolher_dag`` devolve este resultado tipado
    (mesmo contrato de ``SessionStateService.persist_stage_completion`` /
    ``Stage1Result``): ``sucesso`` indica se a escolha foi persistida e, em caso
    de recusa/falha, ``falha`` carrega o motivo e ``mensagem`` o texto pt-BR.

    Attributes
    ----------
    sucesso:
        ``True`` quando o ``dag_escolhido_id`` foi gravado no ``StageRun`` do
        Estágio 2 do Caso e esse ``StageRun`` foi marcado ``concluido``
        (progresso 100), liberando o Estágio 3 (Req. 6.3). ``False`` em qualquer
        recusa/falha, caso em que o ``StageRun`` do Estágio 2 fica inalterado.
    caso_id:
        A Campanha (Caso) para a qual a escolha foi solicitada.
    dag_id:
        O ``dag_id`` solicitado.
    falha:
        ``None`` em caso de sucesso; o :class:`EscolhaDagFalha` correspondente
        em caso de recusa/falha. A API o mapeia para 409 (``SEM_DAG``) ou 404
        (``DAG_NAO_ENCONTRADO``).
    mensagem:
        ``None`` em caso de sucesso; a mensagem pt-BR do motivo em caso de
        recusa/falha, para a API/UI surfacearem.
    estagio:
        O número do Estágio concluído pela escolha (sempre
        :data:`ESTAGIO_GERACAO_DAG`), presente também em caso de recusa para
        contexto.
    stage_run_id:
        O id do ``StageRun`` do Estágio 2 gravado, em caso de sucesso; ``None``
        quando a escolha foi recusada/falhou.
    """

    sucesso: bool
    caso_id: str
    dag_id: int
    falha: Optional[EscolhaDagFalha] = None
    mensagem: Optional[str] = None
    estagio: int = ESTAGIO_GERACAO_DAG
    stage_run_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Conclusão automática do Estágio 2 (DAG curado) — Req. 3.2–3.5, 9.1, 9.3, 9.4
# ---------------------------------------------------------------------------

# Mensagens pt-BR por caminho de não-conclusão da auto-conclusão do Estágio 2
# (para a API/UI surfacearem), espelhando ``EnvironmentStageService`` (Estágio 4).
_MSG_ESTAGIO2_GATE = (
    "O Estágio 1 (Modelagem Estrutural) deste Caso ainda não está concluído; o "
    "Estágio 2 (Geração do DAG) permanece bloqueado."
)
_MSG_ESTAGIO2_SEM_DAG = (
    "Este Caso não possui DAG derivável; o Estágio 2 (Geração do DAG) não é "
    "concluído automaticamente (o botão \"Gerar DAG\" permanece desabilitado e o "
    "avanço bloqueado)."
)
_MSG_ESTAGIO2_FALHA_PERSISTENCIA = (
    "A conclusão automática do Estágio 2 não foi salva: falha ao persistir na "
    "Base_de_Dados. O estado anterior foi preservado sem alteração."
)


class GeracaoDagAutoResult(BaseModel):
    """Resultado tipado de :meth:`DagService.auto_concluir_estagio2`.

    Espelha :class:`~app.services.environment.stage_service.ProvisionamentoAutoResult`
    (auto-conclusão do Estágio 4): em vez de levantar exceções, a auto-conclusão
    devolve este resultado tipado — ``concluido`` indica se o Estágio 2 passou a
    ``concluido`` nesta chamada (ou já estava, de forma idempotente); os demais
    campos explicam **por que** não concluiu, de modo que a camada de API/tela
    possa surfacear a razão e manter o avanço bloqueado.

    Attributes
    ----------
    caso_id:
        O Caso cuja auto-conclusão do Estágio 2 foi tentada.
    concluido:
        ``True`` quando, ao fim da chamada, o ``StageRun`` do Estágio 2 do Caso
        está ``concluido`` (progresso 100) — por esta chamada ou já de antes
        (idempotência). ``False`` em qualquer caminho que **não** conclua (gate
        não liberado, sem DAG derivável, ou falha de persistência).
    bloqueado_pelo_gate:
        ``True`` quando o Estágio 1 (Modelagem) do Caso **não** está
        ``concluido``; o Estágio 2 permanece bloqueado e nada é persistido
        (Req. 9.3).
    sem_dag:
        ``True`` quando não há DAG derivável para o Caso (o backfill curado não
        persistiu nenhum DAG); a auto-conclusão não ocorre e o Estágio 2 fica não
        concluído (Req. 9.4).
    total_dags:
        Quantidade de DAGs persistidos do Caso após o backfill curado.
    persistencia_falhou:
        ``True`` quando havia DAG derivável mas o ``commit`` falhou; o estado
        anterior foi preservado sem alteração (rollback).
    mensagem:
        Texto pt-BR explicando o resultado (razão da não-conclusão), para a
        API/UI surfacearem; ``None`` quando concluiu sem ressalvas.
    stage_run_id:
        O id do ``StageRun`` do Estágio 2, quando concluído; ``None`` caso
        contrário.
    """

    caso_id: str
    concluido: bool = False
    bloqueado_pelo_gate: bool = False
    sem_dag: bool = False
    total_dags: int = 0
    persistencia_falhou: bool = False
    mensagem: Optional[str] = None
    stage_run_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DagService:
    """Loads/persists DAGs, builds Mermaid, and distinguishes curado/stub.

    Reuses :class:`~app.services.case_service.CaseService` for path resolution
    (``dag_path``) and typed parsing (``DagData``/``DagNode``), and knows which
    slugs are curated via ``CaseService.CURATED_CASES`` — no slug is hardcoded
    here. A custom ``case_service`` may be injected (e.g. in tests / fixtures) to
    point at alternative local fixtures.

    Persistence / geração (task 5.1 — Req. 4.1–4.5, 4.8, 5.1)
    ---------------------------------------------------------
    The service also persists and lists first-class :class:`~app.models.domain.Dag`
    entities for a Campanha, consuming a :class:`~app.services.dag.gerador.GeradorDeDag`
    (``CuradoriaGeradorDeDag`` by default — Req. 5.1). It takes an injected
    SQLAlchemy :class:`~sqlalchemy.orm.Session` (same convention as
    ``StageService``/``SessionStateService``), so the same code works with the
    app's ``SessionLocal`` and an ephemeral test SQLite session. The ``session``
    is **optional**: the pure read path (``obter_dag``/``montar_mermaid``) does
    not need a database, so the legacy ``DagService(case_service=...)`` call used
    by ``GET /api/casos/{caso}/dag`` keeps working unchanged. The persistence
    methods (:meth:`sincronizar_dags_curados`, :meth:`listar_dags`) require a
    session and raise a clear error if none was injected.
    """

    def __init__(
        self,
        session: Optional[Session] = None,
        case_service: Optional[CaseService] = None,
        gerador: Optional["GeradorDeDag"] = None,
        stage_service: Optional[StageService] = None,
    ) -> None:
        self._session = session
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )
        # StageService sobre a MESMA Session, para obter a Modelagem do Estágio 1
        # (``read_stage1``/``run_stage1``) e ler o estado dos Estágios no gate da
        # conclusão automática. Construído preguiçosamente (só quando há Session),
        # reaproveitando o ``case_service`` para apontar às mesmas fixtures.
        self._stage_service: Optional[StageService] = stage_service
        if gerador is None:
            # Lazy import: ``gerador`` imports ``DagService`` from this module,
            # so importing it at module top-level would create a cycle. The
            # default gerador is the curadoria implementation (Req. 5.1).
            from app.services.dag.gerador import CuradoriaGeradorDeDag

            gerador = CuradoriaGeradorDeDag(self._case_service)
        self._gerador: "GeradorDeDag" = gerador

    @property
    def case_service(self) -> CaseService:
        """The underlying :class:`CaseService` used for paths and parsing."""
        return self._case_service

    @property
    def gerador(self) -> "GeradorDeDag":
        """The injected :class:`GeradorDeDag` (curadoria by default — Req. 5.1)."""
        return self._gerador

    def _require_session(self) -> Session:
        """Return the injected :class:`Session` or raise if persistence is unused.

        The pure read path (``obter_dag``/``montar_mermaid``) never needs a
        database, so ``session`` is optional. The persistence methods require
        one; calling them without an injected session is a programming error.
        """
        if self._session is None:
            raise RuntimeError(
                "DagService requer uma Session injetada para persistir/listar "
                "DAGs; nenhuma foi fornecida."
            )
        return self._session

    def _get_stage_service(self) -> StageService:
        """Devolve o :class:`StageService` sobre a mesma Session (lazy).

        O ``StageService`` é usado para obter a Modelagem do Estágio 1 (a entrada
        da geração do DAG — Req. 3.1, 10.1) e para ler o estado dos Estágios no
        gate da conclusão automática. Construído preguiçosamente sobre a Session
        injetada e o mesmo ``case_service``, de modo que opera nas mesmas
        fixtures/Base_de_Dados que o restante do serviço.
        """
        if self._stage_service is None:
            self._stage_service = StageService(
                session=self._require_session(),
                case_service=self._case_service,
            )
        return self._stage_service

    def _obter_modelagem(self, caso: str) -> "ModelagemEstrutural":
        """Obtém a Modelagem Estrutural (Estágio 1) do Caso para a geração do DAG.

        No esquema de cinco Estágios o DAG (Estágio 2) é derivado da Modelagem
        (Estágio 1 — Req. 3.1, 10.1), então a costura ``GeradorDeDag.gerar``
        recebe esta Modelagem. A saída do Estágio 1 (técnicas + relações) é lida
        via :meth:`StageService.read_stage1` quando o Estágio 1 já está
        ``concluido`` (caminho sem efeito colateral) e, caso contrário,
        materializada por :meth:`StageService.run_stage1` (a Modelagem precisa
        existir para o DAG ser derivado dela). Os elementos ``techniques`` /
        ``relationships`` da ``Stage1Output`` são mapeados 1:1 para
        :class:`~app.services.dag.gerador.ModelagemEstrutural` (os mesmos
        ``TechniqueElement`` / ``RelationshipElement``), de modo que o fluxo de
        dados Modelagem → DAG fique correto mesmo que a implementação de
        curadoria ignore a Modelagem (o grafo curado já existe em disco).

        Um Estágio 1 que não produziu Saída (vazio/erro de leitura) resulta em
        uma Modelagem vazia — fiel à ausência de material estrutural; a geração
        de curadoria segue produzindo o DAG curado do disco (ou lista vazia sem
        DAG derivável).
        """
        # Import local para evitar o ciclo (``gerador`` importa ``DagService``).
        from app.services.dag.gerador import ModelagemEstrutural

        stage_service = self._get_stage_service()
        estados = stage_service._stage_states_map(caso)
        if estados.get(ESTAGIO_MODELAGEM) is StageState.COMPLETED:
            # Estágio 1 já concluído: leitura pura, sem reexecutar/persistir.
            resultado = stage_service.read_stage1(caso)
        else:
            # Materializa a Modelagem (executa + persiste o Estágio 1), de modo
            # que o DAG do Estágio 2 seja de fato derivado dela.
            resultado = stage_service.run_stage1(caso)

        saida = resultado.saida
        if saida is None:
            return ModelagemEstrutural()
        return ModelagemEstrutural(
            techniques=list(saida.techniques),
            relationships=list(saida.relationships),
        )

    # ---- persistência / geração (task 5.1) -------------------------------

    def sincronizar_dags_curados(self, caso: str) -> list[DagResumo]:
        """Backfill idempotente dos DAGs curados de uma Campanha (Req. 4.5).

        Chama ``gerador.gerar(caso, modelagem)`` (``CuradoriaGeradorDeDag`` por
        padrão, que lê ``data/dag/{slug}_dag.json`` via ``CaseService`` — **sem
        rede e sem IA**, Req. 5.5, 9.1) e persiste cada :class:`DagProduzido` como uma
        entidade :class:`~app.models.domain.Dag` associada ao Caso, **se ainda
        não existir** uma equivalente.

        Idempotência (Req. 4.5): a reconciliação é feita pela chave
        ``(caso_id, origem, identificador)``. Como ``CuradoriaGeradorDeDag`` usa
        ``identificador = slug`` e ``origem = curadoria_humana``, isso equivale a
        "um DAG curado por Caso": rodar este passo uma ou várias vezes produz
        exatamente o mesmo conjunto persistido, sem duplicar. Um Caso cujo DAG
        curado está ausente/malformado faz o gerador devolver lista vazia — nada
        é persistido (nenhum ``Dag`` inválido), e os demais Casos não são
        afetados (mesmo isolamento por Caso de ``GET /api/casos``).

        A FK ``dags.caso_id → casos.id`` exige a linha ``Case``: ela é feita
        *upsert* defensivamente (mesmo padrão de ``StageService._persist``) antes
        de inserir o DAG, de modo que o backfill funcione mesmo antes de o
        Estágio 1 ter persistido o Caso. Tudo em uma única transação (commit no
        fim; rollback preserva o estado anterior).

        No esquema de cinco Estágios o DAG (Estágio 2) é derivado da Modelagem
        Estrutural (Estágio 1): a costura ``GeradorDeDag.gerar(caso, modelagem)``
        passa a **receber** a Modelagem (Req. 3.1, 10.1). Este backfill obtém a
        Modelagem do Estágio 1 do Caso (via :meth:`_obter_modelagem`) e a repassa
        ao gerador. A implementação de curadoria a **ignora** (o grafo curado já
        existe em disco), mas a Modelagem é construída fielmente a partir da saída
        real do Estágio 1 para que o fluxo de dados fique correto para a futura
        implementação de IA.

        Devolve a listagem resultante dos DAGs do Caso (:meth:`listar_dags`).
        """
        session = self._require_session()

        modelagem = self._obter_modelagem(caso)
        produzidos = self._gerador.gerar(caso, modelagem)
        for produzido in produzidos:
            self._upsert_case_row(caso, produzido.rotulo)
            # Reconciliação idempotente por (caso_id, origem, identificador).
            existe = (
                session.execute(
                    select(DagModel.id)
                    .where(DagModel.caso_id == caso)
                    .where(DagModel.origem == produzido.origem)
                    .where(DagModel.identificador == produzido.identificador)
                )
                .scalars()
                .first()
            )
            if existe is not None:
                # Já persistido: não duplicar (Req. 4.5).
                continue
            session.add(
                DagModel(
                    caso_id=caso,
                    origem=produzido.origem,
                    identificador=produzido.identificador,
                    rotulo=produzido.rotulo,
                    conteudo=produzido.conteudo,
                    criado_em=_utcnow(),
                )
            )

        session.commit()
        return self.listar_dags(caso)

    def listar_dags(self, caso: str) -> list[DagResumo]:
        """DAGs persistidos de uma Campanha (Req. 4.8).

        Devolve um :class:`DagResumo` por linha ``Dag`` do Caso — ``id``,
        ``origem``, ``identificador``/versão, ``rotulo`` e ``criado_em`` — sem o
        ``conteudo`` gráfico (obtido sob demanda por ``obter_dag_persistido``).
        Ordenado por ``id`` ascendente (ordem de criação) para uma listagem
        estável. Um Caso sem DAG persistido devolve lista vazia.
        """
        session = self._require_session()
        stmt = (
            select(DagModel)
            .where(DagModel.caso_id == caso)
            .order_by(DagModel.id.asc())
        )
        linhas = session.execute(stmt).scalars().all()
        return [
            DagResumo(
                id=linha.id,
                caso_id=linha.caso_id,
                origem=linha.origem,
                identificador=linha.identificador,
                rotulo=linha.rotulo,
                criado_em=linha.criado_em,
            )
            for linha in linhas
        ]

    def obter_dag_persistido(self, caso: str, dag_id: int) -> Optional[DagDetalhe]:
        """Grafo + Mermaid de um DAG persistido pelo seu ``dag_id`` (Req. 4.4).

        Reconstrói o grafo a partir do ``Dag.conteudo`` persistido
        (``structural_nodes`` + ``arestas``), montando um :class:`DagData`
        equivalente de modo a reaproveitar :meth:`montar_mermaid` /
        :meth:`_derivar_arestas` — exatamente como :meth:`obter_dag` faz para o
        DAG curado em disco. Assim, as ``arestas`` do payload e as arestas
        desenhadas no ``mermaid`` são sempre consistentes.

        Validação (Req. 4.4 — 404): o ``dag_id`` precisa existir **e** pertencer
        ao Caso (``Dag.caso_id == caso``). Um ``dag_id`` inexistente ou de outra
        Campanha devolve ``None`` — a API o mapeia para 404 Not Found. Em caso de
        sucesso devolve um :class:`DagDetalhe` tipado.

        Pure read sobre a Base_de_Dados: não acessa a rede nem invoca IA.
        """
        session = self._require_session()

        dag = session.get(DagModel, dag_id)
        if dag is None or dag.caso_id != caso:
            # Inexistente ou alheio ao Caso → 404 na API (Req. 4.4).
            return None

        # Reconstrói um DagData a partir do conteudo persistido para reusar a
        # mesma montagem de nós/arestas/Mermaid de ``obter_dag``. O conteudo
        # guarda ``structural_nodes`` (com node_id/technique_*/relations) e
        # ``arestas``; os nós preservam ``child_nodes``, então
        # ``_derivar_arestas`` reproduz exatamente as arestas persistidas.
        conteudo = dag.conteudo if isinstance(dag.conteudo, dict) else {}
        nodes_raw = conteudo.get("structural_nodes") or []
        dag_data = DagData.model_validate({"structural_nodes": nodes_raw})

        nos = [
            DagNoView(
                node_id=node.node_id,
                technique_id=node.technique_id,
                technique_name=node.technique_name,
                tactic=node.tactic,
            )
            for node in dag_data.structural_nodes
            if node.node_id is not None
        ]
        arestas = [
            DagArestaView(de=src, para=dst)
            for src, dst in DagService._derivar_arestas(dag_data)
        ]
        mermaid = DagService.montar_mermaid(dag_data)

        return DagDetalhe(
            caso_id=dag.caso_id,
            dag_id=dag.id,
            origem=dag.origem,
            identificador=dag.identificador,
            rotulo=dag.rotulo,
            nos=nos,
            arestas=arestas,
            mermaid=mermaid,
        )

    def escolher_dag(self, caso: str, dag_id: int) -> EscolhaDagResult:
        """Escolhe um DAG para a execução, concluindo o Estágio 2 (Req. 6.3).

        Grava ``dag_escolhido_id = dag_id`` no ``StageRun`` do Estágio 2
        (``estagio=2`` — Geração do DAG) do Caso e marca esse ``StageRun`` como
        ``concluido`` (progresso 100), liberando o avanço para o Estágio 3 pelo
        gate sequencial (Req. 6.3). O ``StageRun`` do Estágio 2 é feito *upsert*
        pelo par ``(caso_id, estagio=2)`` — mesmo contrato de ``StageService`` /
        ``SessionStateService._get_or_create_stage_run`` — então uma execução
        ainda sem linha de Estágio 2 recebe uma.

        Validação (Req. 6.2, 6.3) — resultado tipado, sem exceções
        -----------------------------------------------------------
        * **Sem DAG persistido** para a Campanha (Req. 6.2): devolve
          ``sucesso=False`` com ``falha=SEM_DAG`` (a API mapeia para 409
          Conflict). O ``StageRun`` do Estágio 2 **não** é alterado.
        * **``dag_id`` inexistente ou alheio ao Caso** (Req. 6.3 — erro):
          devolve ``sucesso=False`` com ``falha=DAG_NAO_ENCONTRADO`` (a API
          mapeia para 404 Not Found). A validação exige que o ``Dag`` exista
          **e** que ``Dag.caso_id == caso``; um ``dag_id`` de outra Campanha é
          tratado como não encontrado. O ``StageRun`` do Estágio 2 **não** é
          alterado.

        Idempotência / consistência
        ----------------------------
        Como a gravação é feita por *upsert* do ``StageRun`` de ``estagio=2`` e a
        escolha é uma atribuição absoluta (``dag_escolhido_id = dag_id`` +
        ``concluido``/100), escolher o **mesmo** ``dag_id`` novamente produz
        exatamente o mesmo estado persistido (nenhuma linha nova, nenhum campo
        divergente); re-escolher um ``dag_id`` **diferente** (válido) do Caso
        sobrescreve a escolha anterior de forma determinística.

        Atomicidade / preservação do estado anterior
        ---------------------------------------------
        A gravação roda em uma única transação (``commit``). Se o ``commit``
        falhar, a sessão é revertida (``rollback``), descartando a mutação
        pendente do ``StageRun`` e deixando o estado anteriormente persistido
        **exatamente** como estava — mesmo padrão de
        ``SessionStateService.persist_stage_completion``. Nesse caso devolve
        ``sucesso=False`` com ``falha=FALHA_PERSISTENCIA`` em vez de levantar.

        Parameters
        ----------
        caso:
            O slug da Campanha (Caso) cuja execução terá o DAG escolhido.
        dag_id:
            O id do ``Dag`` persistido a escolher. Deve existir e pertencer ao
            Caso.

        Returns
        -------
        EscolhaDagResult
            ``sucesso=True`` com o ``stage_run_id`` do Estágio 2 em caso de
            sucesso; ``sucesso=False`` com o motivo tipado em ``falha`` em caso
            de recusa (``SEM_DAG`` / ``DAG_NAO_ENCONTRADO``) ou falha de
            persistência (``FALHA_PERSISTENCIA``).
        """
        session = self._require_session()

        # Req. 6.2: a Campanha precisa ter ao menos um DAG persistido; caso
        # contrário o avanço permanece bloqueado (409 na API). Verificar antes de
        # validar o dag_id específico distingue "nenhum DAG" de "DAG alheio".
        tem_algum_dag = (
            session.execute(
                select(DagModel.id).where(DagModel.caso_id == caso).limit(1)
            )
            .scalars()
            .first()
        )
        if tem_algum_dag is None:
            return EscolhaDagResult(
                sucesso=False,
                caso_id=caso,
                dag_id=dag_id,
                falha=EscolhaDagFalha.SEM_DAG,
                mensagem=_MENSAGEM_SEM_DAG,
            )

        # Req. 6.3: o dag_id precisa existir E pertencer ao Caso. Um dag_id de
        # outra Campanha (ou inexistente) é "não encontrado" (404 na API). O
        # StageRun do Estágio 2 não é tocado neste caminho.
        dag = session.get(DagModel, dag_id)
        if dag is None or dag.caso_id != caso:
            return EscolhaDagResult(
                sucesso=False,
                caso_id=caso,
                dag_id=dag_id,
                falha=EscolhaDagFalha.DAG_NAO_ENCONTRADO,
                mensagem=_MENSAGEM_DAG_NAO_ENCONTRADO,
            )

        # Grava a escolha no StageRun do Estágio 2 (upsert por (caso_id, 2)) e o
        # conclui (progresso 100), liberando o Estágio 3 (Req. 6.3). Atribuição
        # absoluta ⇒ idempotente ao re-escolher o mesmo dag_id.
        stage_run = self._get_or_create_stage_run_geracao_dag(caso)
        stage_run.dag_escolhido_id = dag_id
        stage_run.estado = StageState.COMPLETED
        stage_run.progresso = 100
        # Concluído: limpa quaisquer marcadores de erro anteriores do Estágio 2.
        stage_run.mensagem_erro = None
        stage_run.etapa_falha = None
        stage_run.atualizado_em = _utcnow()

        try:
            session.commit()
        except SQLAlchemyError:
            # Falha ao persistir: rollback descarta a mutação pendente, deixando
            # o estado anteriormente persistido intacto (sem atualização parcial).
            session.rollback()
            return EscolhaDagResult(
                sucesso=False,
                caso_id=caso,
                dag_id=dag_id,
                falha=EscolhaDagFalha.FALHA_PERSISTENCIA,
                mensagem=_MENSAGEM_FALHA_PERSISTENCIA,
            )

        session.refresh(stage_run)
        return EscolhaDagResult(
            sucesso=True,
            caso_id=caso,
            dag_id=dag_id,
            stage_run_id=stage_run.id,
        )

    def auto_concluir_estagio2(self, caso: str) -> GeracaoDagAutoResult:
        """Tenta concluir automaticamente o Estágio 2 (DAG curado) do Caso ``caso``.

        Contraparte, para o DAG, de
        :meth:`~app.services.environment.stage_service.EnvironmentStageService.auto_concluir_estagio4`:
        mesma costura de gate sequencial, idempotência, upsert transacional do
        ``StageRun`` e resultado tipado.

        Fluxo (todos os caminhos são read-only exceto a conclusão final):

        1. **Gate sequencial (Req. 9.3).** Se o Estágio 1 (Modelagem) do Caso
           **não** está ``concluido``, não faz nada e devolve
           ``bloqueado_pelo_gate=True`` — o Estágio 2 permanece bloqueado.
        2. **Idempotência.** Se o Estágio 2 já está ``concluido``, devolve
           ``concluido=True`` sem repersistir (o estado já é o desejado).
        3. **Backfill curado (Req. 9.1, 9.4).** Roda
           :meth:`sincronizar_dags_curados` (que agora passa a Modelagem do
           Estágio 1 ao gerador — **sem rede e sem IA**). Se **nenhum** DAG
           derivável foi persistido para o Caso, devolve ``sem_dag=True`` sem
           concluir — a UI mostra "Gerar DAG" desabilitado e o avanço fica
           bloqueado (Req. 9.4).
        4. **Conclusão.** Havendo ao menos um DAG persistido ⇒ *upsert* do
           ``StageRun`` de ``(caso, 2)`` para ``concluido``/100 em uma única
           transação. Falha de ``commit`` ⇒ ``rollback`` +
           ``persistencia_falhou=True`` (estado anterior preservado).

        Não levanta por caminhos esperados — a razão é sempre um sinal tipado no
        resultado, como ``EscolhaDagResult`` / ``ProvisionamentoAutoResult``.
        """
        session = self._require_session()
        stage_service = self._get_stage_service()
        estados = stage_service._stage_states_map(caso)

        # (1) Gate sequencial (Req. 9.3): só prossegue com o Estágio 1 concluído.
        if estados.get(ESTAGIO_MODELAGEM) is not StageState.COMPLETED:
            return GeracaoDagAutoResult(
                caso_id=caso,
                concluido=False,
                bloqueado_pelo_gate=True,
                mensagem=_MSG_ESTAGIO2_GATE,
            )

        # (2) Idempotência: já concluído ⇒ nada a repersistir.
        if estados.get(ESTAGIO_GERACAO_DAG) is StageState.COMPLETED:
            row = stage_service._stage_run_row(caso, ESTAGIO_GERACAO_DAG)
            dags = self.listar_dags(caso)
            return GeracaoDagAutoResult(
                caso_id=caso,
                concluido=True,
                total_dags=len(dags),
                stage_run_id=(row.id if row is not None else None),
            )

        # (3) Backfill curado (passa a Modelagem ao gerador). Sem DAG derivável,
        # o Estágio 2 não conclui (Req. 9.4).
        dags = self.sincronizar_dags_curados(caso)
        if not dags:
            return GeracaoDagAutoResult(
                caso_id=caso,
                concluido=False,
                sem_dag=True,
                total_dags=0,
                mensagem=_MSG_ESTAGIO2_SEM_DAG,
            )

        # (4) Há DAG derivável ⇒ conclui o Estágio 2 (upsert absoluto, transacional).
        stage_run = self._get_or_create_stage_run_geracao_dag(caso)
        stage_run.estado = StageState.COMPLETED
        stage_run.progresso = 100
        stage_run.mensagem_erro = None
        stage_run.etapa_falha = None
        stage_run.atualizado_em = _utcnow()

        try:
            session.commit()
        except SQLAlchemyError:
            session.rollback()
            return GeracaoDagAutoResult(
                caso_id=caso,
                concluido=False,
                total_dags=len(dags),
                persistencia_falhou=True,
                mensagem=_MSG_ESTAGIO2_FALHA_PERSISTENCIA,
            )

        session.refresh(stage_run)
        return GeracaoDagAutoResult(
            caso_id=caso,
            concluido=True,
            total_dags=len(dags),
            stage_run_id=stage_run.id,
        )

    def _get_or_create_stage_run_geracao_dag(self, caso: str) -> StageRun:
        """Devolve o ``StageRun`` do Estágio 2 (Geração do DAG), criando-o se ausente.

        Mantém uma linha por ``(caso_id, estagio=2)`` (mesmo contrato de upsert
        de ``StageService`` / ``SessionStateService._get_or_create_stage_run``),
        de modo que gravar a escolha (ou a conclusão automática) atualiza a
        execução existente do Estágio 2 em vez de acumular linhas.
        """
        session = self._require_session()
        stmt = (
            select(StageRun)
            .where(StageRun.caso_id == caso)
            .where(StageRun.estagio == ESTAGIO_GERACAO_DAG)
            .order_by(StageRun.id.asc())
        )
        stage_run = session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(caso_id=caso, estagio=ESTAGIO_GERACAO_DAG)
            session.add(stage_run)
        return stage_run

    def _upsert_case_row(self, caso: str, rotulo: Optional[str]) -> None:
        """Garante a linha ``Case`` para satisfazer a FK ``dags.caso_id``.

        Mesmo padrão de upsert por ``id`` usado em ``StageService`` ao persistir
        um Caso. Só preenche ``nome`` quando a linha é nova (ou está sem nome),
        para não sobrescrever metadados já persistidos pelo Estágio 1.
        """
        session = self._require_session()
        db_case = session.get(CaseModel, caso)
        if db_case is None:
            db_case = CaseModel(id=caso, nome=rotulo or caso)
            session.add(db_case)
        elif not db_case.nome:
            db_case.nome = rotulo or caso
        session.flush()

    @staticmethod
    def _derivar_arestas(dag: DagData) -> list[tuple[str, str]]:
        """Derive the DAG's ``(from, to)`` edges from the structural relations.

        Edges come from each structural node's ``child_nodes`` (the
        authoritative edge source, exactly as
        ``stage_service._extract_relationships`` does). This is the single shared
        edge-derivation routine used by both :meth:`montar_mermaid` and
        :meth:`obter_dag`, so the ``arestas`` returned in the payload are always
        consistent with the edges drawn in the Mermaid string.

        Only nodes carrying a ``node_id`` can participate (the DAG files always
        key relations by ``node_id``), edges are de-duplicated by ``(from, to)``
        and edges to unknown node ids are skipped defensively — deriving edges
        only from these relations (never inventing any) preserves the
        acyclicity guaranteed by ``metadata.validation.is_dag``.

        Pure: deterministic, no I/O and no network.
        """
        known_ids: set[str] = {
            node.node_id for node in dag.structural_nodes if node.node_id is not None
        }
        edges: list[tuple[str, str]] = []
        seen_edges: set[tuple[str, str]] = set()
        for node in dag.structural_nodes:
            if node.node_id is None:
                continue
            for child in node.child_nodes:
                edge = (node.node_id, child)
                if edge in seen_edges or child not in known_ids:
                    continue
                seen_edges.add(edge)
                edges.append(edge)
        return edges

    @staticmethod
    def montar_mermaid(dag: DagData) -> str:
        """Build a pure, acyclic-by-construction Mermaid ``flowchart TD`` string.

        One node per ``structural_node`` labelled
        ``n_{node_id}["{technique_id}<br/>{technique_name}<br/>{tactic}"]`` and
        one edge per edge entry (``n_{from} --> n_{to}``), mirroring
        ``topologia._mermaid``. Edges are derived from the DAG's structural
        relations (``parent_nodes``/``child_nodes``) without introducing extra
        edges, preserving the acyclicity guaranteed by
        ``metadata.validation.is_dag``. No embedded scripts.

        Pure function: deterministic, no I/O and no network — it depends only on
        its ``dag`` argument. An empty DAG (no structural nodes) yields a minimal
        valid ``flowchart TD`` (header only), never raising.
        """

        def _mermaid_node_id(node_id: str) -> str:
            """Sanitize a ``node_id`` into a Mermaid-safe ``n_...`` identifier.

            Replaces every character that is not ``[A-Za-z0-9_]`` with ``_`` and
            prefixes with ``n_`` so the id is always a valid Mermaid node id.
            """
            return "n_" + _UNSAFE_NODE_ID_CHARS.sub("_", node_id)

        def _label_part(value: Optional[str]) -> str:
            """A label segment with any ``"`` stripped so it can't break syntax.

            Mermaid node labels are quoted with ``"``; a literal ``"`` inside the
            label would terminate the string early (and could be abused to inject
            markup), so quotes are removed. ``None`` becomes an empty segment.
            """
            return (value or "").replace('"', "")

        lines: list[str] = ["flowchart TD"]

        # Only nodes that carry a ``node_id`` can be addressed by an edge; the
        # DAG files always key relations by ``node_id`` (see
        # stage_service._extract_relationships), so a node without one is skipped
        # — it cannot participate in the graph.
        for node in dag.structural_nodes:
            if node.node_id is None:
                continue
            nid = _mermaid_node_id(node.node_id)
            technique_id = _label_part(node.technique_id)
            technique_name = _label_part(node.technique_name)
            tactic = _label_part(node.tactic)
            label = f"{technique_id}<br/>{technique_name}<br/>{tactic}"
            lines.append(f'  {nid}["{label}"]')

        # One edge per DAG relation, derived from the structural nodes'
        # ``child_nodes`` via the shared :meth:`_derivar_arestas` routine (the
        # authoritative edge source, exactly as
        # stage_service._extract_relationships does). Deriving edges from these
        # relations — instead of inventing any — preserves the acyclicity
        # guaranteed by ``metadata.validation.is_dag``. De-duplicated by
        # (from, to); edges to unknown node ids are skipped defensively.
        for src_id, dst_id in DagService._derivar_arestas(dag):
            src = _mermaid_node_id(src_id)
            dst = _mermaid_node_id(dst_id)
            lines.append(f"  {src} --> {dst}")

        return "\n".join(lines)

    def obter_dag(self, caso: str) -> DagView:
        """Resolve the DAG view for a case slug (curado / stub / error).

        * Curated slug (in ``CaseService.CURATED_CASES``) with a valid
          ``{slug}_dag.json``: ``curado=True``, ``pode_avancar=True``, with
          ``nos``/``arestas`` and the assembled ``mermaid``.
        * Non-curated slug: "passa-direto" (``curado=False``,
          ``pode_avancar=False``, ``indisponivel=True``, ``mensagem``) — **no AI
          service is invoked**.
        * Curated slug with missing/malformed DAG: typed error (``curado=True``,
          ``pode_avancar=False``, ``erro``) distinguishing missing from
          malformed.

        Pure read: this method only reads ``{slug}_dag.json`` through
        ``CaseService`` and performs **no network access** and **no AI call** on
        any path (there is no AI client on this path — Req. 9.1, 9.2).
        """
        curated_cases = self._case_service.list_cases()

        # --- Non-curated slug: "passa-direto" availability stub -------------
        # No AI service is invoked (and none exists on this path); just return
        # the stub (Req. 5.4, 5.5, 9.1, 9.2).
        if caso not in curated_cases:
            return DagView(
                caso_id=caso,
                nome=caso.replace("_", " ").title(),
                curado=False,
                pode_avancar=False,
                indisponivel=True,
                mensagem=(
                    "A geração automática do DAG ainda não está disponível para "
                    "esta campanha."
                ),
            )

        nome = curated_cases[caso]

        # --- Curated slug: load the case, capturing file/parse problems as a
        # typed value (never a raised exception) via try_load_case (Req. 5.3,
        # 6.4). ``load_case`` would instead fail fast.
        resultado = self._case_service.try_load_case(caso)

        if not resultado.ok or resultado.case is None:
            erro = resultado.error
            # Distinguish a missing DAG file from a malformed/invalid one in a
            # descriptive pt-BR message (Req. 5.3, 6.4).
            if erro is not None and erro.error_type is CaseLoadErrorType.MISSING_FILE:
                mensagem = (
                    f"O DAG de '{caso}' não foi encontrado (arquivo ausente)."
                )
            elif erro is not None and (
                erro.error_type is CaseLoadErrorType.MALFORMED_JSON
            ):
                mensagem = (
                    f"O DAG de '{caso}' está malformado (JSON inválido)."
                )
            elif erro is not None and (
                erro.error_type is CaseLoadErrorType.INVALID_STRUCTURE
            ):
                mensagem = (
                    f"O DAG de '{caso}' está malformado (estrutura inválida)."
                )
            else:
                mensagem = (
                    f"O DAG de '{caso}' não pôde ser lido ou está malformado."
                )
            return DagView(
                caso_id=caso,
                nome=nome,
                curado=True,
                pode_avancar=False,
                erro=mensagem,
            )

        dag = resultado.case.dag
        if dag is None:
            # The case loaded, but carries no DAG payload (null/absent): treat as
            # a missing DAG for the curated case (Req. 5.3, 6.4).
            return DagView(
                caso_id=caso,
                nome=nome,
                curado=True,
                pode_avancar=False,
                erro=f"O DAG de '{caso}' não foi encontrado (arquivo ausente).",
            )

        # --- Curated slug with a valid DAG: build nós, arestas and Mermaid.
        # Edges are derived EXACTLY like montar_mermaid (shared helper), so the
        # ``arestas`` always match the Mermaid edges (Req. 5.2, 6.1, 6.3).
        nos = [
            DagNoView(
                node_id=node.node_id,
                technique_id=node.technique_id,
                technique_name=node.technique_name,
                tactic=node.tactic,
            )
            for node in dag.structural_nodes
            if node.node_id is not None
        ]
        arestas = [
            DagArestaView(de=src, para=dst)
            for src, dst in DagService._derivar_arestas(dag)
        ]
        mermaid = DagService.montar_mermaid(dag)

        return DagView(
            caso_id=caso,
            nome=nome,
            curado=True,
            pode_avancar=True,
            nos=nos,
            arestas=arestas,
            mermaid=mermaid,
        )
