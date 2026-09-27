"""AuditLogger — 1:1 audit-trail persistence per executed command (task 7.1).

Every command executed during an Operation must produce **exactly one**
``AuditLogEntry`` row (Req. 6.8): the concrete command, the target container
(``container_destino``, e.g. ``"nginx (172.21.0.20)"``), the result
(``resultado``), plus ``operacao_id`` / ``ability_id`` and a ``registrado_em``
timestamp. This 1:1 invariant is the foundation for Property 5 (task 7.2:
exactly K rows for K executed commands).

Design alignment
----------------
* Maps directly onto the ``AuditLogEntry`` ORM model in
  :mod:`app.models.domain` (table ``auditoria``). The NOT NULL columns
  (``comando``, ``container_destino``, ``registrado_em``) are always populated
  by :meth:`AuditLogger.log_command`.
* The logger accepts an injected SQLAlchemy :class:`~sqlalchemy.orm.Session`, so
  the same code works with the app's ``SessionLocal`` and with an ephemeral
  test SQLite session — no global state.
* Timestamps are **naive UTC** (``datetime.utcnow``) applied consistently, which
  round-trips cleanly on both SQLite (dev) and PostgreSQL (production) with the
  portable ``DateTime`` column used by the model. A caller may still pass an
  explicit ``registrado_em`` when it needs a fixed instant.

Failure surface (foundation for Req. 6.9 / task 7.5)
----------------------------------------------------
If persisting an entry fails, :meth:`AuditLogger.log_command` rolls back the
session and raises a typed :class:`AuditPersistenceError` (wrapping the
underlying :class:`~sqlalchemy.exc.SQLAlchemyError`). This makes the failure
*observable* so the ``OperationRunner`` can abort the Operation from that point
on. The abort logic itself is **not** implemented here (that is task 7.5) — this
module only makes the failure clean and typed.

This module never runs any operation logic (task 7.3) and never aborts an
Operation (task 7.5). It only persists and reads back audit rows.

_Requisitos: 6.8_
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import AuditLogEntry


class AuditPersistenceError(RuntimeError):
    """Raised when persisting an ``AuditLogEntry`` fails.

    Wraps the underlying database error so the ``OperationRunner`` (task 7.5)
    can detect an audit-write failure and abort the Operation (Req. 6.9) without
    coupling to SQLAlchemy exception types. The offending command / container
    are attached for a descriptive UI message.
    """

    def __init__(
        self,
        message: str,
        *,
        comando: str,
        container_destino: str,
        original: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.comando = comando
        self.container_destino = container_destino
        self.original = original


def _utcnow() -> dt.datetime:
    """Return the current naive UTC timestamp.

    Naive UTC is used consistently across the audit trail so timestamps
    round-trip identically on SQLite (dev) and PostgreSQL (production) via the
    portable ``DateTime`` column on ``AuditLogEntry``.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class AuditLogger:
    """Persist and read back the audit trail, one row per executed command.

    Parameters
    ----------
    session:
        An open SQLAlchemy :class:`~sqlalchemy.orm.Session`. Injected by the
        caller so the logger works with both the app's ``SessionLocal`` and an
        ephemeral test session. The logger does not open or close the session;
        it only uses it (commit/rollback around each write).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def log_command(
        self,
        *,
        comando: str,
        container_destino: str,
        resultado: Optional[str] = None,
        operacao_id: Optional[int] = None,
        ability_id: Optional[str] = None,
        registrado_em: Optional[dt.datetime] = None,
    ) -> AuditLogEntry:
        """Persist exactly one ``AuditLogEntry`` for one executed command.

        This is the 1:1 seam: a single call inserts a single row (Req. 6.8).

        Parameters
        ----------
        comando:
            The concrete command that was executed (NOT NULL).
        container_destino:
            The target container, e.g. ``"nginx (172.21.0.20)"`` (NOT NULL).
        resultado:
            The command result (success/failure + output), optional per the
            model.
        operacao_id / ability_id:
            Links back to the Operation and the Ability that produced the
            command (optional per the model).
        registrado_em:
            The record instant. Defaults to the current naive UTC time when not
            provided.

        Returns
        -------
        AuditLogEntry
            The persisted, refreshed row (with its generated ``id``).

        Raises
        ------
        AuditPersistenceError
            If the insert/commit fails. The session is rolled back first so it
            stays usable, and the error carries the offending command/container
            so task 7.5 can abort the Operation and surface a UI message.
        """
        entry = AuditLogEntry(
            operacao_id=operacao_id,
            ability_id=ability_id,
            comando=comando,
            container_destino=container_destino,
            resultado=resultado,
            registrado_em=registrado_em if registrado_em is not None else _utcnow(),
        )

        try:
            self._session.add(entry)
            self._session.commit()
        except SQLAlchemyError as exc:  # persistence failure — make it observable
            self._session.rollback()
            raise AuditPersistenceError(
                "Falha ao persistir o registro de auditoria "
                f"(comando={comando!r}, container={container_destino!r}).",
                comando=comando,
                container_destino=container_destino,
                original=exc,
            ) from exc

        self._session.refresh(entry)
        return entry

    def read_trail(self, operacao_id: int) -> list[AuditLogEntry]:
        """Return the audit trail for an Operation, oldest first.

        Helper for the audit endpoint added later (task 11.1,
        ``GET /api/auditoria/{operacao}``). Ordered by ``registrado_em`` then
        ``id`` so entries with identical timestamps keep insertion order.
        """
        stmt = (
            select(AuditLogEntry)
            .where(AuditLogEntry.operacao_id == operacao_id)
            .order_by(AuditLogEntry.registrado_em.asc(), AuditLogEntry.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())


__all__ = ["AuditLogger", "AuditPersistenceError"]
