"""Migration ``a9b0c1d2e3f4`` (``technique_commands.input_arguments``), task 44.6.

Drives the shipped ``upgrade()`` / ``downgrade()`` in-process against a
disposable temp-file SQLite database (never the user's database): the column is
added nullable, existing rows survive untouched with ``NULL``, and the downgrade
removes only that column. Also checks the chain position (``f8a9b0c1d2e3``).

**Validates: Requirements 11.1, 11.2, 11.11**
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_FILE = "20261105_0100-a9b0c1d2e3f4_add_technique_commands_input_arguments.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_migration_a9b0c1d2e3f4", _VERSIONS_DIR / _FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: sa.engine.Engine, module: ModuleType, step: str) -> None:
    with engine.connect() as conn, conn.begin():
        original = module.op
        module.op = Operations(MigrationContext.configure(connection=conn))
        try:
            getattr(module, step)()
        finally:
            module.op = original


def _columns(engine: sa.engine.Engine) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns("technique_commands")}


def test_chains_onto_previous_head() -> None:
    module = _load()
    assert module.revision == "a9b0c1d2e3f4"
    assert module.down_revision == "f8a9b0c1d2e3"


def test_upgrade_adds_nullable_column_keeping_rows_and_downgrade_removes_it() -> None:
    module = _load()
    tmp_dir = Path(tempfile.mkdtemp(prefix="input_args_mig_"))
    db_path = tmp_dir / "mig.db"
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE technique_commands ("
                    "id INTEGER PRIMARY KEY, technique_id VARCHAR, command TEXT)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO technique_commands (technique_id, command) "
                    "VALUES ('T1033', 'whoami')"
                )
            )
        before = set(_columns(engine))
        assert "input_arguments" not in before

        _run(engine, module, "upgrade")

        columns = _columns(engine)
        assert set(columns) - before == {"input_arguments"}
        assert columns["input_arguments"]["nullable"] is True
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT technique_id, command, input_arguments FROM technique_commands")
            ).all()
        assert rows == [("T1033", "whoami", None)]  # data kept, column empty

        _run(engine, module, "downgrade")

        assert set(_columns(engine)) == before
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT technique_id, command FROM technique_commands")
            ).all()
        assert rows == [("T1033", "whoami")]
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            tmp_dir.rmdir()
        except OSError:
            pass
