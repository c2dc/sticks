"""SQLAlchemy domain models for the Pipeline_UI backend (task 2.1).

These models implement exactly the Data Models section of the design, with the
Portuguese table names and columns. Every model inherits from the shared
``Base`` in :mod:`app.db.base`, so importing this module registers all tables
on ``Base.metadata`` (which is what Alembic autogenerate and ``create_all``
target).

Portability notes (PostgreSQL is the product database; the unit tests run
against an ephemeral SQLite file, so the mappings stay portable across both):

* Enums are mapped with ``native_enum=False`` so they persist as a portable
  ``VARCHAR`` with a ``CHECK`` constraint on both PostgreSQL and the test
  SQLite — we do not depend on PostgreSQL native ``ENUM`` types, and we store
  the enum *values* (the Portuguese strings) rather than the member names.
* JSON columns use the generic :class:`sqlalchemy.JSON` type, which maps to a
  portable representation on both backends.
* Column types are the portable generics (``String``, ``Text``, ``Integer``,
  ``DateTime``), avoiding backend-specific types.

The models use the SQLAlchemy 2.x typed ``Mapped`` / ``mapped_column`` style to
match the ``DeclarativeBase`` used by ``Base``.

_Requisitos: 9.1, 9.7, 11.1, 11.2, 6.8_
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    AbilityResultStatus,
    Language,
    OperationState,
    OrigemDag,
    PlataformaAmbiente,
    PlataformaAmbientePersistida,
    StageState,
    Theme,
    TranslationSource,
)


def _portable_enum(enum_cls: type, name: str) -> SAEnum:
    """Build a portable SQLAlchemy ``Enum`` column type.

    Using ``native_enum=False`` makes the column a ``VARCHAR`` + ``CHECK`` on
    every backend (identical behavior on PostgreSQL and the test SQLite). We persist the
    enum *values* (the Portuguese strings) via ``values_callable`` so the stored
    data matches the API/DB contract exactly.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class Case(Base):
    """Caso curado — mapeia o par de arquivos em ``data/api/`` e o DAG.

    A ``origem_traducao`` é um atributo explícito e extensível (Req. 11.1/11.2).
    """

    __tablename__ = "casos"

    # slug: "shadowray", "apt41_dust", ...
    id: Mapped[str] = mapped_column(String, primary_key=True)
    nome: Mapped[str] = mapped_column(String, nullable=False)  # "ShadowRay"
    descricao: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # caminho data/api/{caso}_dag-ability.json
    arquivo_ability: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # caminho data/api/{caso}_dag-adversary.json
    arquivo_adversary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # caminho data/dag/{caso}_dag.json
    arquivo_dag: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    origem_traducao: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )


class Dag(Base):
    """DAG persistido de primeira classe, multi-origem (Req. 4).

    Uma Campanha (``Case``) pode ter N DAGs: cada linha referencia um ``Case``
    por ``caso_id`` (FK → ``casos.id``), estabelecendo a relação 1 Caso → N Dags
    (Req. 4.1, 4.2) — no mesmo estilo de FK já usado por ``Ability``/
    ``Adversary``/``StageRun``.

    ``origem`` é um atributo explícito e extensível (``OrigemDag``), persistido
    com ``native_enum=False`` (VARCHAR + CHECK) via ``_portable_enum``: o CHECK
    gerado já inclui ``gerado_ia``, logo inserir um DAG ``gerado_ia`` no futuro
    não requer migração de esquema (Req. 4.3, 4.6).

    O conteúdo gráfico (``structural_nodes`` + ``arestas``) é guardado em uma
    única coluna JSON portável, equivalente ao conteúdo de
    ``data/dag/{slug}_dag.json`` (Req. 4.4).
    """

    __tablename__ = "dags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_id: Mapped[str] = mapped_column(
        String, ForeignKey("casos.id"), nullable=False
    )
    origem: Mapped[OrigemDag] = mapped_column(
        _portable_enum(OrigemDag, "origem_dag"),
        default=OrigemDag.CURADORIA_HUMANA,
        nullable=False,
    )
    # identificador/versão legível do DAG (Req. 4.4); para curadoria,
    # derivado do arquivo (ex.: "curadoria-v1")
    identificador: Mapped[str] = mapped_column(String, nullable=False)
    rotulo: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # conteúdo gráfico: {"structural_nodes": [...], "arestas": [{"de","para"}]}
    conteudo: Mapped[Any] = mapped_column(JSON, nullable=False)
    criado_em: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class StageRun(Base):
    """Execução de um Estágio de um Caso (estados, progresso, falha)."""

    __tablename__ = "execucoes_estagio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("casos.id"), nullable=True
    )
    # Estágio do pipeline no range 1..6 (coluna Integer pura, sem CHECK de
    # intervalo: o intervalo é acomodado pela própria coluna — Req. 1.2).
    estagio: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..6
    estado: Mapped[StageState] = mapped_column(
        _portable_enum(StageState, "stage_state"),
        default=StageState.NOT_STARTED,
        nullable=False,
    )
    progresso: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0..100
    # causa da falha, quando estado == erro
    mensagem_erro: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # etapa da modelagem em que falhou (Estágio 1)
    etapa_falha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # DAG escolhido para esta execução (preenchido no Estágio 1 — Req. 4.7, 6.3).
    # Nullable: Estágios 2/3/4 e execuções antigas não têm valor.
    dag_escolhido_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("dags.id"), nullable=True
    )
    atualizado_em: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class Ability(Base):
    """Mapeia ``data/api/{caso}_dag-ability.json`` (lista de abilities).

    A ``origem_traducao`` é um atributo explícito e extensível (Req. 11.1/11.2).
    """

    __tablename__ = "abilities"

    # uuid do arquivo
    ability_id: Mapped[str] = mapped_column(String, primary_key=True)
    caso_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("casos.id"), nullable=True
    )
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # "T1102 - Web Service"
    tactic: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    technique_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    technique_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # lista de {name, platform, command}
    executors: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    origem_traducao: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )


class Adversary(Base):
    """Mapeia ``data/api/{caso}_dag-adversary.json`` (objeto único).

    A ``origem_traducao`` é um atributo explícito e extensível (Req. 11.1/11.2).
    """

    __tablename__ = "adversaries"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # id do arquivo
    caso_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("casos.id"), nullable=True
    )
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # lista ordenada de ability_ids
    atomic_ordering: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    origem_traducao: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )


class Operation(Base):
    """Execução de um Adversary na Caldera contra o Ambiente_Docker."""

    __tablename__ = "operacoes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("casos.id"), nullable=True
    )
    caldera_operation_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    estado: Mapped[OperationState] = mapped_column(
        _portable_enum(OperationState, "operation_state"),
        default=OperationState.NOT_STARTED,
        nullable=False,
    )
    # agregado (Req. 4.5)
    total_sucesso: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_falha: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    iniciada_em: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    finalizada_em: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class AbilityResult(Base):
    """Resultado por Ability de uma Operação (status + saída do comando)."""

    __tablename__ = "resultados_ability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operacao_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("operacoes.id"), nullable=True
    )
    ability_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("abilities.ability_id"), nullable=True
    )
    status: Mapped[AbilityResultStatus] = mapped_column(
        _portable_enum(AbilityResultStatus, "ability_result_status"),
        default=AbilityResultStatus.PENDING,
        nullable=False,
    )
    # stdout/stderr do comando
    saida_comando: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AuditLogEntry(Base):
    """Trilha de auditoria persistida — Req. 6.8.

    ``comando``, ``container_destino`` e ``registrado_em`` são NOT NULL: cada
    comando executado gera exatamente um registro com o comando concreto, o
    container de destino e o instante do registro.
    """

    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operacao_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("operacoes.id"), nullable=True
    )
    ability_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # comando concreto executado
    comando: Mapped[str] = mapped_column(Text, nullable=False)
    # ex "nginx (192.168.20.30)"
    container_destino: Mapped[str] = mapped_column(String, nullable=False)
    resultado: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # sucesso/falha + saída
    registrado_em: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class SessionState(Base):
    """Estado_de_Sessão — continuidade entre máquinas (Req. 9.1, 9.7).

    Os estágios concluídos por caso e os resultados são derivados de
    ``StageRun``/``Operation`` associados ao caso correspondente.
    """

    __tablename__ = "estado_sessao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_atual: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("casos.id"), nullable=True
    )
    atualizado_em: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class UserPreferences(Base):
    """Preferências de tema e idioma do Pesquisador (Req. 7.6, 8.6)."""

    __tablename__ = "preferencias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tema: Mapped[Theme] = mapped_column(
        _portable_enum(Theme, "theme"),
        default=Theme.LIGHT,  # padrão Modo_Claro (Req. 7.6)
        nullable=False,
    )
    idioma: Mapped[Language] = mapped_column(
        _portable_enum(Language, "language"),
        default=Language.PT_BR,  # padrão pt-BR (Req. 8.6)
        nullable=False,
    )


class VersaoStix(Base):
    """Flag de versão curada por versão STIX (Req. 5.5, 5.6).

    Fonte persistível da flag de versão curada. A constante ``VERSAO_CURADA``
    (em :mod:`app.services.stix.stix_service`) é a fonte única em código e semeia
    esta tabela na migração; aqui apenas definimos o esquema portável.

    * ``versao`` é a PK (ex.: ``"18.0"``).
    * ``curada`` marca a versão como curada (``True`` para a versão 18 — Req. 5.5).
    * ``artefatos_curados_path`` aponta os artefatos curados da versão (Req. 5.6),
      ``None`` quando a versão não tem artefatos curados associados.
    """

    __tablename__ = "versoes_stix"

    versao: Mapped[str] = mapped_column(String, primary_key=True)  # ex.: "18.0"
    curada: Mapped[bool] = mapped_column(Boolean, nullable=False)
    artefatos_curados_path: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )


class ParametrosAmbiente(Base):
    """Parâmetros de ambiente do Estágio 4 (Operacionalização) — Req. 8.3.

    A segunda Saída tipada do Estágio 4 (``ParametrosDeAmbiente``) é persistida em
    uma única coluna JSON portável (``conteudo``), como ``Dag.conteudo``, evitando
    normalização desnecessária (Req. 8.3, 17.1).

    ``plataforma`` persiste como VARCHAR + CHECK (``native_enum=False``) a partir
    de ``PlataformaAmbientePersistida``. Seguindo o precedente de
    ``OrigemDag``/``gerado_ia``, esse enum de persistência já declara
    ``maquina_virtual``, então o CHECK gerado o admite mesmo que apenas
    ``conteiner`` esteja implementado na aplicação (Req. 17.1): persistir uma
    plataforma de máquina virtual no futuro não exige migração de esquema
    (Req. 17.2).

    ``origem`` é o atributo explícito e extensível (``TranslationSource``,
    VARCHAR + CHECK que já inclui os valores do enum): uma operacionalização
    futura ``gerado_ia`` encaixa sem alterar o esquema (Req. 16.2, 16.4).
    """

    __tablename__ = "parametros_ambiente"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_id: Mapped[str] = mapped_column(
        String, ForeignKey("casos.id"), nullable=False
    )
    # O tipo de coluna é derivado de ``PlataformaAmbientePersistida`` (que já
    # declara ``maquina_virtual``), de modo que o CHECK portável aceite a VM
    # desde a primeira migração — sem migração futura (Req. 17.2). O valor
    # ``conteiner`` coincide com ``PlataformaAmbiente.CONTAINER``, logo gravar o
    # enum público continua válido.
    plataforma: Mapped[PlataformaAmbientePersistida] = mapped_column(
        _portable_enum(PlataformaAmbientePersistida, "plataforma_ambiente"),
        default=PlataformaAmbiente.CONTAINER,
        nullable=False,
    )
    # ``ParametrosDeAmbiente`` serializado (hosts/serviços/network_context).
    conteudo: Mapped[Any] = mapped_column(JSON, nullable=False)
    origem: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )
    criado_em: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class AmbienteProvisionadoRow(Base):
    """Saída do Estágio 5 (Criação do ambiente) persistida — Req. 6.3, 1.6.

    O artefato tipado :class:`~app.services.environment.provisionador.AmbienteProvisionado`
    produzido quando o provisionamento do Estágio 5 fica **pronto** (contêineres
    saudáveis + Abilities carregadas + isolamento aprovado) é persistido aqui, em
    uma única coluna JSON portável (``conteudo``) associada ao Caso, exatamente no
    precedente de ``Dag.conteudo`` / ``ParametrosAmbiente.conteudo`` /
    ``RelatorioExecucao.metricas`` — de modo que o Estágio 6 e a tela consumam a
    Saída **após a conclusão** (Req. 6.3) e o provisionamento seja **reaproveitado**
    sem subir um novo quando o ambiente já está pronto (idempotência — Req. 1.6).

    ``plataforma`` persiste como VARCHAR + CHECK (``native_enum=False``) a partir
    de ``PlataformaAmbientePersistida`` — como ``ParametrosAmbiente.plataforma`` —,
    de modo que o CHECK gerado já aceite ``maquina_virtual`` sem migração futura
    (Req. 17.2). O valor ``conteiner`` coincide com ``PlataformaAmbiente.CONTAINER``,
    logo gravar o enum público continua válido.

    ``origem`` é o atributo explícito e extensível (``TranslationSource``, VARCHAR +
    CHECK que já inclui ``gerado_ia``): um provisionamento futuro ``gerado_ia``
    encaixa sem alterar o esquema (Req. 16.2, 16.4).

    ``pronto`` guarda o veredito de homologação da Saída persistida (os três
    pré-requisitos passaram) — é o sinal que a idempotência do Estágio 5 lê para
    reaproveitar o ambiente (Req. 1.6). Uma única linha por Caso (upsert
    *latest-row-wins*): a persistência sempre atualiza a linha existente do Caso.
    """

    __tablename__ = "ambiente_provisionado"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_id: Mapped[str] = mapped_column(
        String, ForeignKey("casos.id"), nullable=False
    )
    # O tipo de coluna é derivado de ``PlataformaAmbientePersistida`` (que já
    # declara ``maquina_virtual``), de modo que o CHECK portável aceite a VM
    # desde a primeira migração — sem migração futura (Req. 17.2). O valor
    # ``conteiner`` coincide com ``PlataformaAmbiente.CONTAINER``, logo gravar o
    # enum público continua válido.
    plataforma: Mapped[PlataformaAmbientePersistida] = mapped_column(
        _portable_enum(PlataformaAmbientePersistida, "plataforma_ambiente"),
        default=PlataformaAmbiente.CONTAINER,
        nullable=False,
    )
    # ``AmbienteProvisionado`` serializado via ``model_dump(mode="json")``
    # (plataforma, contêineres-alvo, compose_file, isolamento, saude,
    # abilities_carregadas, origem) — Saída portável do Estágio 5 (Req. 6.3).
    conteudo: Mapped[Any] = mapped_column(JSON, nullable=False)
    origem: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )
    # Veredito de homologação da Saída (os três pré-requisitos passaram) — o
    # sinal "pronto+persistido" que a idempotência do Estágio 5 lê (Req. 1.6).
    pronto: Mapped[bool] = mapped_column(Boolean, nullable=False)
    criado_em: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    atualizado_em: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class RelatorioExecucao(Base):
    """Relatório/métricas do Estágio 6 (Execução, medição e relatório) — Req. 15.7.

    As ``MetricasSuficiencia`` são persistidas em uma coluna JSON portável
    (``metricas``), como ``Dag.conteudo``. ``veredito_sucesso`` guarda o veredito
    geral da execução (Req. 15.6) e ``ambiente_desprovisionado`` registra se o
    ambiente foi desprovisionado ao final do Estágio 6 (Req. 12.1).
    """

    __tablename__ = "relatorios_execucao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_id: Mapped[str] = mapped_column(
        String, ForeignKey("casos.id"), nullable=False
    )
    # liga à Operação executada; nullable (relatório pode existir sem operação).
    operacao_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("operacoes.id"), nullable=True
    )
    # ``MetricasSuficiencia`` serializada (Req. 15.8).
    metricas: Mapped[Any] = mapped_column(JSON, nullable=False)
    veredito_sucesso: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ambiente_desprovisionado: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gerado_em: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
