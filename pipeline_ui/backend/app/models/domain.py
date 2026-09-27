"""SQLAlchemy domain models for the Pipeline_UI backend (task 2.1).

These models implement exactly the Data Models section of the design, with the
Portuguese table names and columns. Every model inherits from the shared
``Base`` in :mod:`app.db.base`, so importing this module registers all tables
on ``Base.metadata`` (which is what Alembic autogenerate and ``create_all``
target).

Portability notes (SQLite dev default, PostgreSQL production target):

* Enums are mapped with ``native_enum=False`` so they persist as a portable
  ``VARCHAR`` with a ``CHECK`` constraint on both SQLite and PostgreSQL — we do
  not depend on PostgreSQL native ``ENUM`` types, and we store the enum
  *values* (the Portuguese strings) rather than the member names.
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
    StageState,
    Theme,
    TranslationSource,
)


def _portable_enum(enum_cls: type, name: str) -> SAEnum:
    """Build a portable SQLAlchemy ``Enum`` column type.

    Using ``native_enum=False`` makes the column a ``VARCHAR`` + ``CHECK`` on
    every backend (identical behavior on SQLite and PostgreSQL). We persist the
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


class StageRun(Base):
    """Execução de um Estágio de um Caso (estados, progresso, falha)."""

    __tablename__ = "execucoes_estagio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("casos.id"), nullable=True
    )
    estagio: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2 ou 3
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
    # ex "nginx (172.21.0.20)"
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
