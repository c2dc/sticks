"""Domain enums for the Pipeline_UI backend (task 2.1).

These enums implement exactly the value sets defined in the design's Data
Models section. They subclass ``str`` so their members serialize to their
Portuguese string values (the values persisted in the database and exchanged
over the REST/JSON API), while still being proper ``enum.Enum`` members in
Python code.

The Portuguese string values are the source of truth: they match the API/DB
contracts described in the design (endpoints and WebSocket channels use these
exact strings, e.g. ``"nao_iniciado"``, ``"em_execucao"``, ``"pendente"``).

_Requisitos: 11.1, 11.2, 6.8, 7.6, 8.6_
"""

from __future__ import annotations

import enum


class TranslationSource(str, enum.Enum):
    """Origem da tradução do Estágio 2 — atributo explícito e extensível.

    Hoje, todas as 8 traduções curadas usam ``HUMAN_CURATION``. Valores futuros
    (por exemplo agentes de IA) podem ser adicionados aqui **sem** alterar o
    modelo persistido de ``Ability``/``Adversary``/``Case`` — o atributo
    ``origem_traducao`` já existe e apenas passa a aceitar novos valores.

    _Requisitos: 11.1, 11.2_
    """

    HUMAN_CURATION = "curadoria_humana"  # valor das 8 traduções existentes
    # Valores futuros (fora de escopo de implementação) podem ser adicionados
    # sem alterar o modelo de Ability/Adversary, por exemplo:
    # AI_AGENT = "agente_ia"


class StageState(str, enum.Enum):
    """Estado de um Estágio (Req. 1.4, 5.3)."""

    NOT_STARTED = "nao_iniciado"
    IN_PROGRESS = "em_andamento"
    COMPLETED = "concluido"
    ERROR = "erro"


class OperationState(str, enum.Enum):
    """Estado de uma Operação na Caldera."""

    NOT_STARTED = "nao_iniciada"
    RUNNING = "em_execucao"
    FINISHED = "finalizada"
    ABORTED = "abortada"


class AbilityResultStatus(str, enum.Enum):
    """Status de execução de uma Ability durante uma Operação (Req. 4.3)."""

    PENDING = "pendente"
    RUNNING = "em_execucao"
    SUCCESS = "sucesso"
    FAILURE = "falha"


class Theme(str, enum.Enum):
    """Tema visual da interface (Req. 7)."""

    LIGHT = "claro"
    DARK = "escuro"


class Language(str, enum.Enum):
    """Idioma da interface (Req. 8)."""

    PT_BR = "pt-BR"
    EN = "en"
