"""Property test da origem persistida extensível — ``gerado_ia`` sem migração (task 10.4).

# Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível nos
# Estágios 3, 4 e 5. Parte **persistência** (Estágio 4 / Operacionalização): a
# linha ``parametros_ambiente`` aceita ``origem = gerado_ia`` e
# ``plataforma = maquina_virtual`` sem qualquer alteração de esquema — o esquema
# de cada exemplo é construído pelo ORM (``Base.metadata.create_all``), e os
# ``CHECK`` portáveis (``native_enum=False``) já incluem ambos os valores
# (``TranslationSource.GERADO_IA`` e ``PlataformaAmbientePersistida.VM``).

Validates: Requirements 16.4, 17.2

Esta é uma property test de *feature* (``pipeline-seis-estagios`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a definição atual do
modelo :class:`~app.models.domain.ParametrosAmbiente` + os enums de persistência
:class:`~app.models.enums.PlataformaAmbientePersistida` e
:class:`~app.models.enums.TranslationSource`.

O que a propriedade garante
---------------------------
* **Origem extensível sem migração (Req. 16.4):** persistir uma linha de
  ``parametros_ambiente`` com ``origem = "gerado_ia"`` é aceito pelo esquema
  gerado pelo ORM, sem alteração de esquema nem migração. O ``CHECK`` portável
  da coluna ``origem`` (``translation_source``, ``native_enum=False``) já admite
  o valor; o round-trip devolve a string pt-BR exata gravada no disco.
* **Plataforma de VM admitida sem migração (Req. 17.2):** persistir
  ``plataforma = "maquina_virtual"`` é aceito pelo mesmo esquema. A coluna
  ``plataforma`` é construída a partir de
  :class:`~app.models.enums.PlataformaAmbientePersistida` (que já declara
  ``maquina_virtual``), de modo que o ``CHECK`` portável o aceite desde a
  primeira migração — exatamente o precedente de ``OrigemDag``/``gerado_ia``.
* **Round-trip do conteúdo (Req. 8.3):** ``caso_id``, ``conteudo`` (JSON) e
  ``criado_em`` voltam idênticos ao persistido, e a FK ``caso_id → casos.id`` é
  respeitada.

Como a propriedade é dirigida
------------------------------
Hypothesis gera, para cada exemplo, um ``conteudo`` JSON-serializável no estilo
de ``ParametrosDeAmbiente`` (hosts/serviços/network_context) e um ``criado_em``
arbitrário. A ``origem`` é fixada em ``"gerado_ia"`` e a ``plataforma`` em
``"maquina_virtual"`` — os valores *futuros* que a Property 13 (lado
persistência) exige que o esquema aceite sem migração. Para cada exemplo um
banco SQLite efêmero em arquivo temporário é construído pelo ORM (mesmo padrão
de ``test_dag_entity_property.py``), inserimos o ``Case`` e a
``ParametrosAmbiente``, lemos de volta e comparamos cada campo contra o valor
gerado. As asserções nunca confiam na implementação para decidir o resultado
esperado. Cada exemplo constrói e descarta o próprio banco (sem vazamento de
estado), offline e sem rede/Docker.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Case, ParametrosAmbiente


# ---------------------------------------------------------------------------
# Valores "futuros" exigidos pela Property 13 (lado persistência). São strings
# cruas de propósito: o teste prova que o esquema gerado pelo ORM os aceita SEM
# migração, independentemente de o enum de aplicação ainda não os expor.
# ---------------------------------------------------------------------------

_ORIGEM_FUTURA = "gerado_ia"  # TranslationSource futura (Req. 16.4)
_PLATAFORMA_FUTURA = "maquina_virtual"  # PlataformaAmbiente futura (Req. 17.2)


# ---------------------------------------------------------------------------
# Generators para um ParametrosAmbiente arbitrário e válido.
# ---------------------------------------------------------------------------

_rotulo_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=12
)

# Um host-alvo mínimo, JSON-serializável, no estilo de ParametrosDeAmbiente.
_host_strategy = st.fixed_dictionaries(
    {
        "rotulo": _rotulo_strategy,
        "endereco": st.text(alphabet="0123456789.", min_size=7, max_size=15),
        "container": _rotulo_strategy,
        "host_type": st.sampled_from(["linux", "windows", "network"]),
        "vulnerabilidades": st.lists(st.text(max_size=20), max_size=4),
    }
)

_servico_strategy = st.fixed_dictionaries(
    {
        "descricao": st.text(max_size=40),
        "tecnica_origem": st.text(alphabet="T0123456789", min_size=1, max_size=6),
    }
)

# conteudo JSON (ParametrosDeAmbiente serializado) — Req. 8.3.
_conteudo_strategy = st.fixed_dictionaries(
    {
        "hosts": st.lists(_host_strategy, max_size=5),
        "servicos": st.lists(_servico_strategy, max_size=5),
        "network_context": st.one_of(st.none(), st.text(max_size=60)),
    }
)

# criado_em arbitrário. Microssegundos zerados: SQLite guarda DateTime como
# texto ISO e alguns formatos truncam frações, então mantemos o round-trip
# determinístico (ainda cobre data/hora arbitrárias), igual a
# test_dag_entity_property.py.
_criado_em_strategy = st.datetimes(
    min_value=dt.datetime(2000, 1, 1),
    max_value=dt.datetime(2100, 12, 31, 23, 59, 59),
).map(lambda d: d.replace(microsecond=0))

_params_strategy = st.fixed_dictionaries(
    {
        "conteudo": _conteudo_strategy,
        "criado_em": _criado_em_strategy,
    }
)


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
#
# Mirrors test_dag_entity_property.py: the schema is built with the ORM
# (``Base.metadata.create_all``), NOT alembic — so ``gerado_ia`` e
# ``maquina_virtual`` persistem SEM migração nova, porque os ``CHECK`` portáveis
# (``native_enum=False``) já incluem esses valores.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward (no ``.db`` state leaks between examples).
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_origem_persist_prop_")
    db_path = Path(tmp_dir) / "origem_persist_prop.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


# ---------------------------------------------------------------------------
# Property 13 (lado persistência) — para qualquer conteudo/criado_em, uma linha
# de parametros_ambiente com origem=gerado_ia e plataforma=maquina_virtual:
#   (1) é aceita pelo esquema gerado pelo ORM (sem migração);
#   (2) faz round-trip byte-a-byte de caso_id/conteudo/criado_em;
#   (3) grava no disco exatamente as strings pt-BR "gerado_ia"/"maquina_virtual".
# ---------------------------------------------------------------------------


# ``deadline=None``: cada exemplo cria e descarta um banco SQLite em arquivo
# temporário, cujo I/O pode exceder o deadline padrão do Hypothesis. O banco por
# exemplo é intencional (sem vazamento de estado), então desabilitamos o
# deadline em vez de enfraquecer o isolamento.
# Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível nos
# Estágios 3, 4 e 5 (parte persistência — gerado_ia e maquina_virtual sem migração).
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(spec=_params_strategy)
def test_property13_origem_persistida_extensivel_sem_migracao(spec: dict) -> None:
    """Persistir origem=gerado_ia e plataforma=maquina_virtual não exige migração.

    # Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível
    # nos Estágios 3, 4 e 5 (parte persistência).
    Validates: Requirements 16.4, 17.2
    """
    conteudo: dict = spec["conteudo"]
    criado_em: dt.datetime = spec["criado_em"]

    session, db_path, engine = _build_ephemeral_session()
    try:
        # Insere o Caso para que a FK caso_id → casos.id seja respeitada.
        caso_id = "caso-prop13-persist"
        session.add(Case(id=caso_id, nome="Caso Prop13 persist"))
        session.commit()

        # Insere a linha com os valores FUTUROS crus — provando que o esquema
        # gerado pelo ORM (CHECK portável) os aceita SEM migração nova.
        params = ParametrosAmbiente(
            caso_id=caso_id,
            plataforma=_PLATAFORMA_FUTURA,
            conteudo=conteudo,
            origem=_ORIGEM_FUTURA,
            criado_em=criado_em,
        )
        session.add(params)
        session.commit()
        params_id = params.id

        # Lê de volta via ORM a partir de um cache limpo (round-trip real).
        session.expunge_all()
        loaded = session.get(ParametrosAmbiente, params_id)
        assert loaded is not None

        # FK respeitada: a linha permanece associada ao Caso inserido.
        assert loaded.caso_id == caso_id

        # Round-trip byte-a-byte do conteúdo e do instante de criação (Req. 8.3).
        assert loaded.conteudo == conteudo
        assert loaded.criado_em == criado_em

        # O valor cru gravado no disco é a string pt-BR FUTURA — provando que o
        # CHECK portável de ``origem``/``plataforma`` aceitou os valores sem
        # qualquer alteração de esquema (Req. 16.4, 17.2).
        with engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT origem, plataforma FROM parametros_ambiente "
                    "WHERE id = :id"
                ),
                {"id": params_id},
            ).one()
        stored_origem, stored_plataforma = stored
        assert stored_origem == _ORIGEM_FUTURA
        assert stored_plataforma == _PLATAFORMA_FUTURA
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
