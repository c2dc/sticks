"""Property-based test for Property 5 of the pipeline-ui design (task 7.2).

# Feature: pipeline-ui, Property 5: Toda execução gera exatamente um registro de
# auditoria. Para qualquer Operação em que K comandos são executados, a trilha de
# auditoria persistida contém exatamente K registros, cada um com o comando, o
# container de destino e o resultado correspondentes.

Validates: Requisitos 6.8

This is a normal ``feature`` property test (the pipeline-ui spec is a feature
spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.audit.audit_logger.AuditLogger` implementation. It
complements the example-based nullability tests in ``test_models.py`` by
exercising the 1:1 audit-trail invariant across many generated Operations whose
size ``K`` varies from 0 to 30 executed commands.

Strategy design
---------------
- Each example generates a list of ``K`` command-execution records, each a tuple
  of ``(comando, container_destino, resultado)`` drawn from curated-style
  commands, lab container labels, and success/failure results (``resultado`` may
  also be ``None``, which the model allows).
- Every record is persisted through :meth:`AuditLogger.log_command` — the same
  1:1 seam the ``OperationRunner`` uses — against an **ephemeral temp-file
  SQLite** database created fresh *per example*. Execution is mocked: no real
  Caldera/Docker is involved; only the logger and the DB participate.
- We pin each record's ``registrado_em`` to strictly increasing instants so the
  order-preserving read-back (``read_trail`` orders by ``registrado_em`` then
  ``id``) is deterministic and can be matched positionally against the inputs.

Isolation / no state leak
-------------------------
A brand-new temp-file engine + schema is built and torn down inside the test
body for every Hypothesis example (Hypothesis re-runs the body per example), and
a unique ``operacao_id`` is used per example. The temp file is always removed in
a ``finally`` block, so counts stay exact and no ``.db`` state leaks between
examples. Everything is pure in-memory/SQLite with no network access.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AuditLogEntry
from app.services.audit.audit_logger import AuditLogger

# ---------------------------------------------------------------------------
# Generators for a single executed-command record.
# ---------------------------------------------------------------------------

# Curated-style concrete commands (targets are lab-internal here; Property 5 is
# about the 1:1 audit invariant, not containment — containment is Property 1).
_COMMAND_TEMPLATES: list[str] = [
    "curl -X POST -F 'cmd=whoami' http://172.21.0.20:5055/exec",
    "wget http://172.21.0.20/payload.sh",
    "sshpass -p Passw0rd ssh attacker@172.20.0.20 'whoami'",
    "ssh attacker@172.22.0.20 'id'",
    "cat /etc/passwd",
    "uname -a",
    "mysql -h 172.22.0.20 -u root -e 'show databases;'",
    "nmap -sT 172.21.0.0/24",
]

# Lab container labels in the "name (ip)" style used by the design.
_CONTAINER_LABELS: list[str] = [
    "kali (172.20.0.20)",
    "nginx (172.21.0.20)",
    "db (172.22.0.20)",
    "caldera (172.20.0.10)",
]

comando_strategy = st.sampled_from(_COMMAND_TEMPLATES)
container_strategy = st.sampled_from(_CONTAINER_LABELS)
# resultado may be a success/failure string OR None (the model allows NULL).
resultado_strategy = st.one_of(
    st.none(),
    st.sampled_from(["sucesso", "falha"]),
    st.text(alphabet="abcdefghijklmnop 0123456789", min_size=0, max_size=40),
)

command_record_strategy = st.tuples(
    comando_strategy, container_strategy, resultado_strategy
)


def _build_ephemeral_session() -> tuple[Session, "os.PathLike[str] | Path", object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_models.py``. Returns ``(session, db_path, engine)`` so the caller can
    dispose the engine and remove the file afterward. A temp file (not
    ``:memory:``) is used so ``create_all`` and NOT NULL behavior match the dev
    SQLite default exactly.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_audit_")
    db_path = Path(tmp_dir) / "audit.db"
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
# The property.
# ---------------------------------------------------------------------------


# ``deadline=None``: each example builds and tears down a fresh temp-file SQLite
# database (see ``_build_ephemeral_session``), whose file I/O timing varies on
# Windows and can occasionally exceed Hypothesis's 200ms default deadline. The
# per-example DB is intentional (exact counts, no state leak), so we disable the
# per-example deadline rather than weaken the isolation.
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    records=st.lists(command_record_strategy, min_size=0, max_size=30),
    operacao_id=st.integers(min_value=1, max_value=1_000_000),
)
def test_property5_one_audit_entry_per_executed_command(
    records: list[tuple[str, str, str | None]],
    operacao_id: int,
) -> None:
    """Property 5: K executed commands -> exactly K matching audit records.

    # Feature: pipeline-ui, Property 5: Toda execução gera exatamente um registro
    # de auditoria.
    Validates: Requisitos 6.8

    For any Operation in which ``K`` commands are executed, the persisted audit
    trail contains exactly ``K`` records, each with the corresponding command,
    target container and result — in the same order they were logged.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        logger = AuditLogger(session)

        # Base instant; each record gets a strictly increasing timestamp so the
        # order-preserving read-back is deterministic (read_trail orders by
        # registrado_em then id).
        base = dt.datetime(2024, 1, 1, 0, 0, 0)
        for i, (comando, container_destino, resultado) in enumerate(records):
            entry = logger.log_command(
                comando=comando,
                container_destino=container_destino,
                resultado=resultado,
                operacao_id=operacao_id,
                ability_id=f"ability-{i}",
                registrado_em=base + dt.timedelta(seconds=i),
            )
            # Each call persists exactly one row (the 1:1 seam) with a real id.
            assert entry.id is not None

        k = len(records)

        # (a) Exactly K rows exist for this Operation via a COUNT query.
        counted = session.execute(
            select(func.count())
            .select_from(AuditLogEntry)
            .where(AuditLogEntry.operacao_id == operacao_id)
        ).scalar_one()
        assert counted == k

        # (b) No rows leaked to any other Operation id (fresh DB per example).
        total = session.execute(
            select(func.count()).select_from(AuditLogEntry)
        ).scalar_one()
        assert total == k

        # (c) read_trail returns exactly K rows, order-preserving, each matching
        #     the corresponding generated command/container/result.
        trail = logger.read_trail(operacao_id)
        assert len(trail) == k
        for (comando, container_destino, resultado), row in zip(records, trail):
            assert row.operacao_id == operacao_id
            assert row.comando == comando
            assert row.container_destino == container_destino
            assert row.resultado == resultado
    finally:
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
