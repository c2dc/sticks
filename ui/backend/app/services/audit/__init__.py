"""Audit-trail services.

``AuditLogger`` (task 7.1) persists exactly one ``AuditLogEntry`` per executed
command (Req. 6.8) and surfaces persistence failures as a typed
``AuditPersistenceError`` (foundation for the abort logic in task 7.5 / Req. 6.9).
"""

from app.services.audit.audit_logger import AuditLogger, AuditPersistenceError

__all__ = ["AuditLogger", "AuditPersistenceError"]
