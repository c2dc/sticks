"""Threat-intel ingestion — parse an uploaded report into the Command_Base.

This module ingests a Researcher-uploaded Threat_Intel_Report into the
Command_Base (design Component 8, Req. 13.1-13.7). It mirrors the two-collaborator
shape of :mod:`app.services.atomic_red_team_ingest_service`, but the *source* of
the techniques→commands is an uploaded file rather than a GitHub checkout, and it
has two parsing paths plus one injectable LLM boundary:

* **Free text (PDF/TXT/Markdown).** The report body is decoded to text and an
  injected :class:`ChatModel` is asked to extract a list of
  ``{technique_id, command, executor, platform}`` items (Req. 13.1). The LLM is
  injectable so the test (task 15.2) can double it with no network.
* **Structured (STIX/JSON).** The report is parsed as JSON and the
  technique→command fields are read directly, with no LLM call (Req. 13.2). A
  STIX bundle (a JSON object with ``type``/``objects``) is recognized and its
  ``attack-pattern`` external ids and any carried commands are harvested.

Both paths persist the extracted technique→commands into the relational
``technique_commands`` table (the exact half of the Command_Base, Req. 13.3) and
the command↔tool affinities into the ``experience_memory`` pgvector store (the
associative half, Req. 13.4), reusing the SAME idempotency contract as the Atomic
Red Team ingest: a ``technique_commands`` row is keyed by
``UniqueConstraint(technique_id, command_hash, source)`` and inserted only after a
check-before-insert on that key, with ``source="threat_intel"`` for every row, so
re-ingesting the same report never duplicates (Req. 13.6). Affinities are stored
through :meth:`app.services.ai.memory.ExperienceMemory.store_affinity`
(``kind="command_affinity"``), which deduplicates on a deterministic
``metadata.dedup_key``.

Format detection combines the HTTP ``content_type``, the filename extension and
content sniffing (STIX = JSON with ``type``/``objects``; generic JSON; PDF by its
``%PDF`` magic bytes; text by a successful UTF-8/Latin-1 decode). When a report
cannot be interpreted in any supported format the service raises
:class:`ThreatIntelFormatError` **before any write**, so the already-persisted
Command_Base is preserved unchanged (Req. 13.7) — analogous to how
:class:`app.services.atomic_red_team_ingest_service.AtomicFetchError` preserves
the index before any write.

Importing this module performs no I/O: the ORM models, the LLM factory and the
Experience_Memory are resolved lazily inside the methods, matching the other
Session-injected services (Req. 1.7).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory

logger = logging.getLogger(__name__)

# Provenance recorded on every row/affinity ingested by this service. It is the
# ``source`` component of the idempotency key (technique_id, command_hash,
# source) and the affinity ``metadata.source`` — the SAME contract as the Atomic
# Red Team ingest, just with a different provenance value (Req. 13.3, 13.6).
_SOURCE = "threat_intel"

# ATT&CK technique id shape, e.g. ``T1059`` or the sub-technique ``T1059.001``.
_TECHNIQUE_ID_RE = re.compile(r"^T\d{4}(?:\.\d{3})?$")

# PDF files start with the ``%PDF`` magic bytes; sniffed when neither the
# content-type nor the extension disambiguates the format.
_PDF_MAGIC = b"%PDF"


@runtime_checkable
class ChatModel(Protocol):
    """Minimal structural contract for the injected LLM (free-text extraction).

    Any object exposing an ``invoke(prompt) -> response`` method structurally
    satisfies this Protocol — notably the LangChain ``ChatOpenAI`` returned by
    :func:`app.services.ai.llm.make_chat_model`, whose ``invoke`` returns a
    message with a ``content`` attribute. Being ``@runtime_checkable`` lets the
    test double it without importing LangChain (Req. 13.1).
    """

    def invoke(self, prompt: Any) -> Any:
        """Return the model response for ``prompt`` (message or raw string)."""
        ...


class ThreatIntelFormatError(ValueError):
    """Raised when an uploaded report cannot be interpreted in any format.

    Signals Requirement 13.7: the upload is rejected with a descriptive error and
    the Command_Base is preserved unchanged because the error is raised BEFORE any
    write is attempted. The attempted ``filename`` and ``content_type`` are
    carried for diagnostics; the base ``ValueError`` message stays descriptive so
    the endpoint can surface it verbatim.
    """

    def __init__(self, message: str, *, filename: str, content_type: str | None) -> None:
        super().__init__(message)
        self.filename = filename
        self.content_type = content_type


@dataclass(frozen=True)
class ThreatIntelReport:
    """An uploaded threat-intel report: filename, content-type and raw bytes.

    The service's single input. ``content_type`` is the HTTP multipart
    ``Content-Type`` (may be ``None`` / generic); ``content`` is the raw file
    body. Format detection combines all three with content sniffing.
    """

    filename: str
    content_type: str | None
    content: bytes


@dataclass(frozen=True)
class ThreatIntelIngestSummary:
    """Outcome of a :meth:`ThreatIntelIngestService.ingest` run.

    ``report_format`` is the detected format (``structured`` | ``free_text``);
    ``techniques`` counts the distinct techniques extracted; ``commands_parsed``
    the concrete commands extracted; ``commands_inserted`` the new
    ``technique_commands`` rows created (a re-ingest leaves this at 0 for the
    unchanged commands); ``affinities_inserted`` the new experience-memory
    affinity rows (Req. 13.3, 13.4, 13.6).
    """

    report_format: str = ""
    techniques: int = 0
    commands_parsed: int = 0
    commands_inserted: int = 0
    affinities_inserted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ParsedCommand:
    """One normalized command extracted from a threat-intel report."""

    technique_id: str
    command: str
    executor: str
    platform: str


class ThreatIntelIngestService:
    """Ingest an uploaded threat-intel report into the Command_Base, idempotently.

    Session-injected; the Experience_Memory is injected so the associative
    affinities share the request-scoped unit of work, and the ChatModel is
    injectable (defaulting to a lazily-built SEPINF chat model) so the free-text
    extraction can be doubled in tests. See the module docstring for the full
    contract (Req. 13.1-13.7).
    """

    def __init__(
        self,
        session: Session,
        *,
        memory: ExperienceMemory,
        llm: ChatModel | None = None,
    ) -> None:
        self._session = session
        self._memory = memory
        self._llm = llm

    # ---- public API --------------------------------------------------------

    def ingest(self, report: ThreatIntelReport) -> ThreatIntelIngestSummary:
        """Parse ``report`` and persist its technique→commands into the Command_Base.

        Free text (PDF/TXT/Markdown) is extracted via the injected LLM (Req. 13.1);
        structured (STIX/JSON) is extracted from the JSON fields with no LLM call
        (Req. 13.2). The extracted technique→commands are persisted into
        ``technique_commands`` (Req. 13.3) and the command↔tool affinities into
        ``experience_memory`` (Req. 13.4), idempotently by
        ``(technique_id, command_hash, source="threat_intel")`` (Req. 13.6). An
        undecipherable report raises :class:`ThreatIntelFormatError` BEFORE any
        write, preserving the base (Req. 13.7).
        """
        # Detect the format and extract the commands BEFORE opening any write, so
        # an unsupported/undecipherable report leaves the persisted Command_Base
        # untouched (Req. 13.7). Both detection and extraction raise
        # ThreatIntelFormatError on failure without touching the session.
        report_format, parsed = self._extract(report)

        techniques: set[str] = set()
        commands_inserted = 0
        affinities_inserted = 0
        for item in parsed:
            techniques.add(item.technique_id)
            if self._persist_command(item):
                commands_inserted += 1
            if self._persist_affinity(item):
                affinities_inserted += 1

        return ThreatIntelIngestSummary(
            report_format=report_format,
            techniques=len(techniques),
            commands_parsed=len(parsed),
            commands_inserted=commands_inserted,
            affinities_inserted=affinities_inserted,
        )

    # ---- format detection + extraction (no writes) -------------------------

    def _extract(self, report: ThreatIntelReport) -> tuple[str, list[_ParsedCommand]]:
        """Detect the report format and extract its normalized commands.

        Returns ``(report_format, commands)``. Structured reports (STIX/JSON) are
        parsed from their fields (Req. 13.2); free-text reports (PDF/TXT/Markdown)
        are extracted via the LLM (Req. 13.1). Raises
        :class:`ThreatIntelFormatError` — before any write — when the report
        cannot be interpreted in any supported format (Req. 13.7).
        """
        content = report.content
        if not content:
            raise ThreatIntelFormatError(
                f"threat-intel report {report.filename!r} is empty and carries no "
                "interpretable content.",
                filename=report.filename,
                content_type=report.content_type,
            )

        # 1) Structured JSON/STIX: try to parse the body as JSON first when the
        #    content-type/extension hint at it OR the body sniffs as JSON.
        structured = self._try_parse_json(report)
        if structured is not None:
            return "structured", self._extract_structured(structured)

        # 2) PDF by magic bytes: decode to text, then extract via the LLM.
        if content.startswith(_PDF_MAGIC) or self._looks_like_pdf(report):
            text = self._decode_pdf(report)
            return "free_text", self._extract_free_text(report, text)

        # 3) Plain text / Markdown: accept only content that decodes as valid
        #    UTF-8 text so a binary blob that is neither JSON nor PDF is NOT
        #    silently treated as free text — it falls through to the format error
        #    below, preserving the base (Req. 13.7).
        text = self._try_decode_strict_text(content)
        if text is not None:
            return "free_text", self._extract_free_text(report, text)

        # Nothing matched: reject with a descriptive error, no write attempted.
        raise ThreatIntelFormatError(
            f"threat-intel report {report.filename!r} "
            f"(content-type {report.content_type!r}) could not be interpreted as "
            "a supported format (STIX/JSON, PDF, plain text or Markdown).",
            filename=report.filename,
            content_type=report.content_type,
        )

    def _try_parse_json(self, report: ThreatIntelReport) -> Any | None:
        """Return the parsed JSON document, or ``None`` when the body is not JSON.

        Attempts a JSON parse when the content-type/extension hint at JSON/STIX or
        the decoded body begins with a JSON ``{``/``[``. A decode/parse failure
        returns ``None`` so the caller can fall through to the text paths.
        """
        text = self._try_decode_text(report.content)
        if text is None:
            return None
        stripped = text.lstrip()
        hints_json = self._hints_json(report)
        if not stripped.startswith(("{", "[")) and not hints_json:
            return None
        try:
            return json.loads(text)
        except (ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _hints_json(report: ThreatIntelReport) -> bool:
        """True when the content-type or extension hints at JSON/STIX."""
        ctype = (report.content_type or "").lower()
        name = report.filename.lower()
        return "json" in ctype or "stix" in ctype or name.endswith((".json", ".stix"))

    @staticmethod
    def _looks_like_pdf(report: ThreatIntelReport) -> bool:
        """True when the content-type or extension hints at a PDF."""
        ctype = (report.content_type or "").lower()
        return "pdf" in ctype or report.filename.lower().endswith(".pdf")

    @staticmethod
    def _try_decode_text(content: bytes) -> str | None:
        """Decode ``content`` as UTF-8, then Latin-1; ``None`` if both fail.

        Used ONLY by the JSON sniff: Latin-1 maps every byte, so a non-text
        binary technically decodes, but the JSON path still requires the decoded
        body to start with ``{``/``[`` or a JSON content hint before a parse is
        attempted, so a binary blob never masquerades as JSON here.
        """
        for encoding in ("utf-8", "latin-1"):
            try:
                return content.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return None

    @staticmethod
    def _try_decode_strict_text(content: bytes) -> str | None:
        """Decode ``content`` as strict UTF-8 for the free-text path; else ``None``.

        The plain-text/Markdown path accepts only content that is valid UTF-8 so
        a binary blob that is neither JSON nor PDF is rejected (returned as
        ``None``) rather than silently fed to the LLM — the caller then raises
        :class:`ThreatIntelFormatError` and makes no write (Req. 13.7).
        """
        try:
            return content.decode("utf-8")
        except (UnicodeDecodeError, LookupError):
            return None

    def _decode_pdf(self, report: ThreatIntelReport) -> str:
        """Extract text from a PDF body, degrading to a best-effort decode.

        Uses ``pypdf`` when available to pull the page text; if the dependency is
        absent or the parse fails, falls back to a lossy Latin-1 decode so the LLM
        still receives something to extract from. Imported lazily to keep the
        module's import side-effect-free (Req. 1.7).
        """
        try:
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(report.content))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n".join(pages).strip()
            if text:
                return text
        except Exception as exc:  # noqa: BLE001 -- degrade to a lossy decode
            logger.warning(
                "PDF text extraction for %s failed (%s: %s); falling back to a lossy decode.",
                report.filename,
                type(exc).__name__,
                exc,
            )
        return report.content.decode("latin-1", errors="ignore")

    # ---- structured (STIX/JSON) extraction (Req. 13.2) ---------------------

    def _extract_structured(self, document: Any) -> list[_ParsedCommand]:
        """Extract technique→commands from a parsed STIX/JSON document.

        Recognizes a STIX bundle (a mapping with ``type`` and an ``objects``
        list) and harvests ``attack-pattern`` external ATT&CK ids plus any
        commands carried on the objects; otherwise reads a generic JSON shape
        where each entry names a ``technique_id`` and one or more ``commands``
        (Req. 13.2). Entries without a usable technique id or command are skipped.
        """
        if isinstance(document, dict) and self._is_stix_bundle(document):
            return self._extract_stix_bundle(document)
        return self._extract_generic_json(document)

    @staticmethod
    def _is_stix_bundle(document: dict[str, Any]) -> bool:
        """True when the mapping is a STIX bundle (``type`` + ``objects`` list)."""
        return document.get("type") == "bundle" and isinstance(document.get("objects"), list)

    def _extract_stix_bundle(self, document: dict[str, Any]) -> list[_ParsedCommand]:
        """Harvest technique ids + commands from a STIX bundle's objects.

        Each ``attack-pattern`` object carries its ATT&CK id in an
        ``external_references`` entry (``source_name == "mitre-attack"``). Any
        commands the object carries (a ``x_commands`` list of
        ``{command, executor, platform}`` or plain strings) are mapped to that
        technique. This keeps the STIX path field-driven with no LLM call.
        """
        commands: list[_ParsedCommand] = []
        for obj in document.get("objects", []):
            if not isinstance(obj, dict) or obj.get("type") != "attack-pattern":
                continue
            technique_id = self._stix_attack_id(obj)
            if technique_id is None:
                continue
            commands.extend(self._commands_from_entry(technique_id, obj))
        return commands

    @staticmethod
    def _stix_attack_id(obj: dict[str, Any]) -> str | None:
        """Return the ATT&CK technique id from a STIX attack-pattern's refs."""
        refs = obj.get("external_references")
        if not isinstance(refs, list):
            return None
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            if ref.get("source_name") == "mitre-attack":
                external_id = ref.get("external_id")
                if isinstance(external_id, str) and _TECHNIQUE_ID_RE.match(external_id.strip()):
                    return external_id.strip()
        return None

    def _extract_generic_json(self, document: Any) -> list[_ParsedCommand]:
        """Extract technique→commands from a generic JSON document.

        Accepts either a list of ``{technique_id, command(s), executor,
        platform}`` entries or a single mapping (wrapped in a list or carrying a
        ``techniques``/``items`` list). Each entry's commands are normalized and
        mapped to its technique id (Req. 13.2).
        """
        entries = self._normalize_entries(document)
        commands: list[_ParsedCommand] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            technique_id = self._resolve_technique_id(entry)
            if technique_id is None:
                continue
            commands.extend(self._commands_from_entry(technique_id, entry))
        return commands

    @staticmethod
    def _normalize_entries(document: Any) -> list[Any]:
        """Flatten a JSON document into the list of technique entries to read."""
        if isinstance(document, list):
            return document
        if isinstance(document, dict):
            for key in ("techniques", "items", "mappings"):
                value = document.get(key)
                if isinstance(value, list):
                    return value
            return [document]
        return []

    def _commands_from_entry(
        self, technique_id: str, entry: dict[str, Any]
    ) -> list[_ParsedCommand]:
        """Build normalized commands for a technique from a structured entry.

        Reads an entry's ``commands`` list (each a ``{command, executor,
        platform}`` mapping or a bare command string) plus a single top-level
        ``command`` field, applying the entry-level executor/platform as the
        default for bare strings. A STIX attack-pattern carries its commands under
        the ``x_commands`` custom property, read with the same shape. Empty
        commands are skipped.
        """
        default_executor = self._normalize_token(entry.get("executor")) or "unknown"
        default_platform = self._normalize_token(entry.get("platform")) or "unknown"

        raw_commands: list[Any] = []
        for list_key in ("commands", "x_commands"):
            listed = entry.get(list_key)
            if isinstance(listed, list):
                raw_commands.extend(listed)
        single = entry.get("command")
        if isinstance(single, str):
            raw_commands.append(single)

        parsed: list[_ParsedCommand] = []
        for raw in raw_commands:
            if isinstance(raw, str):
                command = self._normalize_command(raw)
                if not command:
                    continue
                parsed.append(
                    _ParsedCommand(
                        technique_id=technique_id,
                        command=command,
                        executor=default_executor,
                        platform=default_platform,
                    )
                )
            elif isinstance(raw, dict):
                command = self._normalize_command(raw.get("command", ""))
                if not command:
                    continue
                parsed.append(
                    _ParsedCommand(
                        technique_id=technique_id,
                        command=command,
                        executor=self._normalize_token(raw.get("executor")) or default_executor,
                        platform=self._normalize_token(raw.get("platform")) or default_platform,
                    )
                )
        return parsed

    @staticmethod
    def _resolve_technique_id(entry: dict[str, Any]) -> str | None:
        """Return a valid ATT&CK technique id from a structured entry, if present."""
        for key in ("technique_id", "technique", "attack_technique", "id"):
            value = entry.get(key)
            if isinstance(value, str) and _TECHNIQUE_ID_RE.match(value.strip()):
                return value.strip()
        return None

    # ---- free text (PDF/TXT/Markdown) extraction via the LLM (Req. 13.1) ---

    def _extract_free_text(self, report: ThreatIntelReport, text: str) -> list[_ParsedCommand]:
        """Extract technique→commands from free text via the injected LLM.

        Prompts the LLM to return a JSON array of
        ``{technique_id, command, executor, platform}`` items and parses its
        response, tolerating a fenced code block or surrounding prose. The LLM is
        resolved lazily (defaulting to the SEPINF chat model) and is injectable so
        the test can double it (Req. 13.1). A response that yields no parseable
        items returns an empty command list rather than raising — the report was a
        supported format, it simply carried nothing extractable.
        """
        llm = self._resolve_llm()
        prompt = self._build_extraction_prompt(text)
        response = llm.invoke(prompt)
        payload = self._response_text(response)
        items = self._parse_llm_items(payload)

        commands: list[_ParsedCommand] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            technique_id = self._resolve_technique_id(item)
            if technique_id is None:
                continue
            command = self._normalize_command(item.get("command", ""))
            if not command:
                continue
            commands.append(
                _ParsedCommand(
                    technique_id=technique_id,
                    command=command,
                    executor=self._normalize_token(item.get("executor")) or "unknown",
                    platform=self._normalize_token(item.get("platform")) or "unknown",
                )
            )
        return commands

    def _resolve_llm(self) -> ChatModel:
        """Return the injected LLM, lazily building the SEPINF chat model if absent.

        Deferred so constructing the service (and importing this module) never
        requires the LangChain dependency or any network; the model is only built
        when free text must actually be extracted (Req. 1.7, 13.1).
        """
        if self._llm is None:
            from app.services.ai.llm import make_chat_model

            self._llm = make_chat_model()
        return self._llm

    @staticmethod
    def _build_extraction_prompt(text: str) -> str:
        """Build the free-text extraction prompt sent to the LLM (Req. 13.1)."""
        return (
            "You are a threat-intelligence analyst. Read the report below and "
            "extract the ATT&CK techniques and the concrete commands used. "
            "Respond ONLY with a JSON array where each element is an object with "
            "the keys: technique_id (an ATT&CK id like T1059 or T1059.001), "
            "command (the concrete command string), executor (e.g. sh, "
            "powershell, cmd) and platform (e.g. linux, windows). If no commands "
            "are present, respond with an empty array [].\n\n"
            "--- REPORT START ---\n"
            f"{text}\n"
            "--- REPORT END ---"
        )

    @staticmethod
    def _response_text(response: Any) -> str:
        """Extract the text payload from an LLM response (message or string).

        A LangChain chat model returns a message with a ``content`` attribute; a
        plain stub may return a string directly. Both are reduced to the text to
        parse.
        """
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        return str(content)

    @staticmethod
    def _parse_llm_items(payload: str) -> list[Any]:
        """Parse the LLM response into a list of extracted item mappings.

        Tolerates a bare JSON array, a fenced ```json code block, or an array
        embedded in surrounding prose by locating the first ``[`` and the last
        ``]``. Returns an empty list when nothing parseable is present.
        """
        if not payload or not payload.strip():
            return []

        candidate = payload.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
        if fence:
            candidate = fence.group(1).strip()

        try:
            parsed = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            start = candidate.find("[")
            end = candidate.rfind("]")
            if start == -1 or end == -1 or end <= start:
                return []
            try:
                parsed = json.loads(candidate[start : end + 1])
            except (ValueError, json.JSONDecodeError):
                return []

        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("techniques", "items", "mappings"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
            return [parsed]
        return []

    # ---- normalization (shared with the Atomic Red Team contract) ----------

    @staticmethod
    def _normalize_command(command: Any) -> str:
        """Normalize a command string for a stable idempotency hash.

        Strips surrounding whitespace and normalizes line endings so trivially
        different encodings of the same command collapse to one row — the SAME
        normalization the Atomic Red Team ingest applies so a command shared
        across sources hashes identically.
        """
        if not isinstance(command, str):
            return ""
        return command.replace("\r\n", "\n").replace("\r", "\n").strip()

    @staticmethod
    def _normalize_token(value: Any) -> str:
        """Lowercase/strip a platform or executor token for stable storage."""
        if not isinstance(value, str):
            return ""
        return value.strip().lower()

    @staticmethod
    def _command_hash(command: str) -> str:
        """Deterministic SHA-256 of the normalized command (idempotency key part).

        The SAME deterministic hash the Atomic Red Team ingest uses, so the
        ``(technique_id, command_hash, source)`` key is consistent across both
        Command_Base sources (Req. 13.6).
        """
        return hashlib.sha256(command.encode("utf-8")).hexdigest()

    # ---- relational persistence (exact half, Req. 13.3) --------------------

    def _persist_command(self, item: _ParsedCommand) -> bool:
        """Insert a ``technique_commands`` row if absent; return True if inserted.

        Idempotent via a check-before-insert on the
        ``(technique_id, command_hash, source="threat_intel")`` key — the SAME
        contract as :meth:`AtomicRedTeamIngestService._persist_command`, so a
        re-ingest reuses the existing row rather than duplicating it (Req. 13.6).
        """
        from app.models.domain import TechniqueCommand

        command_hash = self._command_hash(item.command)
        existing = self._session.execute(
            select(TechniqueCommand.id).where(
                TechniqueCommand.technique_id == item.technique_id,
                TechniqueCommand.command_hash == command_hash,
                TechniqueCommand.source == _SOURCE,
            )
        ).first()
        if existing is not None:
            return False

        self._session.add(
            TechniqueCommand(
                technique_id=item.technique_id,
                command=item.command,
                executor=item.executor,
                platform=item.platform,
                source=_SOURCE,
                command_hash=command_hash,
            )
        )
        self._session.flush()
        return True

    # ---- vector persistence (associative half, Req. 13.4) ------------------

    def _persist_affinity(self, item: _ParsedCommand) -> bool:
        """Store a command↔tool affinity in ``experience_memory`` if absent.

        Delegates to :meth:`ExperienceMemory.store_affinity`
        (``kind="command_affinity"``, task 12.1), which anchors the sentinel
        campaign, embeds the content and deduplicates on a deterministic
        ``metadata.dedup_key`` so re-ingesting the same affinity is a no-op
        (Req. 13.4). Returns True if a new affinity row was inserted.
        """
        content = (
            f"technique {item.technique_id} executor {item.executor} "
            f"platform {item.platform}: {item.command}"
        )
        dedup_key = hashlib.sha256(
            f"{_SOURCE}|{item.technique_id}|{item.executor}|{item.platform}|{item.command}".encode()
        ).hexdigest()

        return self._memory.store_affinity(
            kind="command_affinity",
            content=content,
            dedup_key=dedup_key,
            metadata={
                "source": _SOURCE,
                "technique_id": item.technique_id,
                "command": item.command,
                "executor": item.executor,
                "platform": item.platform,
            },
        )
