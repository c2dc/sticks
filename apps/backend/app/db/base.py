"""SQLAlchemy declarative base.

All domain models (arriving in task 2.1) must inherit from ``Base`` so that
``Base.metadata`` collects every table. Alembic autogenerate targets this same
``Base.metadata`` (see ``alembic/env.py``), which is why the concrete models can
land later without touching the migration wiring.

To make sure the metadata is fully populated whenever the migration env or the
create-all bootstrap imports this module, import the models package here. The
models package is intentionally light today (populated in task 2.x); the import
is a no-op until then and avoids circular imports because models only depend on
``Base`` from this module.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model and by Alembic metadata."""


def import_models() -> None:
    """Import the models package so every table registers on ``Base.metadata``.

    Called by the Alembic env and the local ``create_all`` helper. Kept as a
    function (rather than a top-level import) to avoid import-time cycles and to
    make the intent explicit. Safe to call before concrete models exist.
    """
    # Local import on purpose: keeps this module import-cycle free.
    import app.models  # noqa: F401  (import for side effects / metadata registration)
