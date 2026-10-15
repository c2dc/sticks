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
    """Origem de um artefato — atributo explícito e extensível.

    Hoje, todas as 8 traduções curadas usam ``HUMAN_CURATION``. O valor futuro
    ``GERADO_IA`` já é **declarado** aqui, exatamente como o precedente de
    :class:`OrigemDag`: as colunas que usam este enum
    (``Ability``/``Adversary``/``Case.origem_traducao`` e
    ``ParametrosAmbiente.origem``) persistem com ``native_enum=False`` (VARCHAR +
    CHECK), de modo que o ``CHECK`` gerado já inclua ``gerado_ia`` desde a
    primeira migração. Assim, persistir um artefato de origem ``gerado_ia`` no
    futuro **não** exige qualquer alteração de esquema nem migração (Req. 16.2,
    16.4). A implementação do agente de IA que produziria tais artefatos é
    não-objetivo desta spec (Req. 18.1).

    _Requisitos: 11.1, 11.2, 16.2, 16.4_
    """

    HUMAN_CURATION = "curadoria_humana"  # valor das 8 traduções curadas existentes
    GERADO_IA = "gerado_ia"  # produzido por agente de IA (encaixável depois, Req. 16)


class OrigemDag(str, enum.Enum):
    """Origem de um DAG — atributo explícito e extensível (Req. 4.3, 4.6, 9.4).

    Segue **exatamente** o precedente de ``TranslationSource``: ``str`` Enum com
    valores em pt-BR, que serializam para as strings persistidas no banco e
    trocadas pela API REST/JSON.

    A entidade ``Dag`` persiste ``origem`` com ``native_enum=False`` (VARCHAR +
    CHECK), de modo que o ``CHECK`` gerado já inclui **todos** os valores aqui
    definidos. Incluir ``GERADO_IA`` desde já faz o ``CHECK`` aceitar
    ``"gerado_ia"`` desde a primeira migração: inserir um DAG ``gerado_ia`` no
    futuro **não** exige qualquer alteração de esquema nem migração (Req. 4.6,
    9.4).

    _Requisitos: 4.3, 4.6, 9.4_
    """

    CURADORIA_HUMANA = "curadoria_humana"  # DAGs produzidos por curadoria humana
    GERADO_IA = "gerado_ia"  # produzido por agente de IA (encaixável depois, Req. 9)


class PlataformaAmbiente(str, enum.Enum):
    """Plataforma do ambiente — contêiner hoje; VM admitida sem redesenho (Req. 17).

    Vive na camada de modelos (dependency-free) para ser referenciada tanto pela
    persistência (``parametros_ambiente.plataforma``) quanto pela camada de
    serviço (``app.services.environment.operacionalizacao`` reexporta este enum),
    evitando um ciclo de import entre modelos e serviços.

    Subclasse de ``str`` para serializar ao seu valor em pt-BR na API/DB, seguindo
    o precedente de :class:`TranslationSource`/:class:`OrigemDag`.

    Nesta spec apenas ``CONTAINER`` é implementado (Req. 17.1). ``maquina_virtual``
    **não** é um membro do enum (Req. 17.2) — mas o CHECK da coluna persistida já
    o admite via ``_portable_enum(..., extra_values=("maquina_virtual",))``,
    exatamente como ``OrigemDag``/``gerado_ia``: incluí-lo depois não exige
    migração de esquema.

    _Requisitos: 17.1, 17.2_
    """

    CONTAINER = "conteiner"
    # VM = "maquina_virtual"  # futuro (Req. 17.2) — não implementado nesta spec


class PlataformaAmbientePersistida(str, enum.Enum):
    """Enum **de persistência** da plataforma — fonte do CHECK da coluna.

    Existe apenas para montar o tipo de coluna de ``parametros_ambiente.plataforma``
    via ``_portable_enum`` (VARCHAR + CHECK). Diferentemente de
    :class:`PlataformaAmbiente` (usado pela camada de aplicação, com apenas
    ``CONTAINER`` implementado — Req. 17.1), este enum já **declara**
    ``maquina_virtual`` para que o ``CHECK`` gerado o aceite desde a primeira
    migração — exatamente o precedente de ``OrigemDag``/``gerado_ia``: persistir
    uma plataforma de máquina virtual no futuro **não** exige migração de esquema
    (Req. 17.2). ``CONTAINER`` tem o mesmo valor (``"conteiner"``) do enum público,
    então gravar ``PlataformaAmbiente.CONTAINER`` continua compatível.

    _Requisitos: 17.1, 17.2_
    """

    CONTAINER = "conteiner"
    VM = "maquina_virtual"  # admitido no CHECK; não implementado nesta spec (Req. 17.2)


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
