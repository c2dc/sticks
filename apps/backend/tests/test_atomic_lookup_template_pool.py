"""The agent command pool carries the stored template and its arguments (Req. 12.1-12.4)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import TechniqueCommand
from app.services.ai.tools import tool_atomic_lookup
from app.services.command_base import CommandBase

_ARGS = {"host": {"default": "10.0.0.5", "description": "target"}}
_TEMPLATE = "curl http://#{host}/#{missing} -o $PathToAtomicsFolder/x"


def _session() -> tuple[Session, Path, Any]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="atomic_lookup_pool_"))
    settings_obj = Settings(database_url=f"sqlite:///{(tmp_dir / 'pool.db').as_posix()}")
    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True, class_=Session)
    return factory(), tmp_dir, engine


def test_exact_pool_keeps_template_and_exposes_id_and_arguments() -> None:
    session, tmp_dir, engine = _session()
    try:
        row = TechniqueCommand(
            technique_id="T1105",
            command=_TEMPLATE,
            executor="sh",
            platform="linux",
            source="atomic_red_team",
            command_hash="h1",
            input_arguments=_ARGS,
        )
        session.add(row)
        session.commit()

        suggestions = CommandBase(session).commands_for_technique("T1105")
        assert suggestions[0].id == row.id
        assert suggestions[0].input_arguments == _ARGS

        pool = tool_atomic_lookup("T1105", session=session)
        assert len(pool) == 1
        entry = pool[0]
        assert entry["command"] == _TEMPLATE  # not substituted
        assert entry["command_ref"] == row.id
        assert entry["input_arguments"]["host"] == _ARGS["host"]
        assert entry["input_arguments"]["PathToAtomicsFolder"]["default"] is None
        # Stored metadata is not mutated by the synthetic argument.
        assert "PathToAtomicsFolder" not in _ARGS
        assert entry["technique_id"] == "T1105" and entry["name"] == "sh"
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_similarity_pool_has_no_ref_and_only_detected_names() -> None:
    class _Memory:
        def retrieve(self, *, campaign_id: str, query: str, k: int = 5) -> list[dict]:
            return [
                {
                    "content": _TEMPLATE,
                    "metadata": {"command": _TEMPLATE, "executor": "sh", "platform": "linux"},
                }
            ]

    session, tmp_dir, engine = _session()
    try:
        suggestions = CommandBase(session, memory=_Memory()).commands_for_technique("T1105")  # type: ignore[arg-type] -- test double stands in for the real collaborator
        assert suggestions[0].exact is False
        assert suggestions[0].id is None and suggestions[0].input_arguments is None

        from app.services.atomic_arguments import detect_argument_names

        assert detect_argument_names(_TEMPLATE) == ["host", "missing", "PathToAtomicsFolder"]
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)
