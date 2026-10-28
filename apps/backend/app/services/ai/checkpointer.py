"""LangGraph PostgreSQL checkpointer factory (short-term memory).

Hosts the lazy ``get_checkpointer()`` factory that builds a LangGraph
``PostgresSaver`` over the project's existing PostgreSQL (``database_url``).

Design constraints honoured here:

* **Same database, no new service.** The checkpointer connects via the project's
  existing ``Settings.database_url`` — the single PostgreSQL that already holds
  the source of truth — and establishes no connection to any other database
  (Requirements 6.1, 6.3).
* **No connection at import.** Building the factory opens no socket. The
  underlying connection pool is created with ``open=False`` so the first actual
  connection is deferred until the first checkpoint read or write
  (Requirements 1.7, 4.4).
* **Idempotent setup.** ``setup_checkpointer()`` runs ``PostgresSaver.setup()``
  once to create the checkpoint tables; it is safe to call repeatedly and is
  intended to be invoked from the Alembic migration / application startup.

Note on the connection string: LangGraph's ``PostgresSaver`` talks to psycopg 3
directly, which expects a plain libpq DSN (``postgresql://...``). SQLAlchemy's
``database_url`` may carry a driver marker (``postgresql+psycopg://...``); the
``+psycopg`` suffix is stripped here so the same configuration value drives both
the ORM and the checkpointer without duplicating connection config.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings


def _to_libpq_dsn(database_url: str) -> str:
    """Normalise a SQLAlchemy URL to a plain libpq DSN for psycopg.

    SQLAlchemy encodes the driver in the scheme (e.g.
    ``postgresql+psycopg://``). psycopg / libpq expect the bare
    ``postgresql://`` (or ``postgres://``) scheme, so the ``+<driver>`` marker is
    removed. Any URL without such a marker is returned unchanged.
    """
    scheme, sep, rest = database_url.partition("://")
    if not sep:
        return database_url
    base_scheme = scheme.split("+", 1)[0]
    return f"{base_scheme}://{rest}"


@lru_cache(maxsize=1)
def get_checkpointer():
    """Return the LangGraph checkpointer backed by the existing PostgreSQL.

    The checkpointer uses the SAME database as the source of truth (host:9090
    in the deployed environment), resolved from
    ``get_settings().database_url``. It is built over a psycopg connection pool
    created with ``open=False`` so **no connection is opened at import time or at
    factory-build time**; the pool lazily opens a connection on the first
    checkpoint read or write (Requirement 4.4).

    The instance is cached (one per process) so the whole application shares a
    single pool and a single checkpoint thread space. Call
    :func:`setup_checkpointer` once (from the migration / startup) to create the
    checkpoint tables before the first run.

    Returns:
        A ``langgraph.checkpoint.postgres.PostgresSaver`` bound to the project's
        existing PostgreSQL connection pool.
    """
    # Imported lazily so merely importing this module never pulls in the
    # database driver / LangGraph checkpoint stack or touches the network.
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    dsn = _to_libpq_dsn(get_settings().database_url)

    # ``open=False`` defers establishing any connection until the pool is first
    # used (first checkpoint read/write). ``autocommit=True`` is required for
    # ``PostgresSaver.setup()`` to persist the checkpoint tables and
    # ``row_factory=dict_row`` is required because the saver accesses rows by
    # column name.
    pool = ConnectionPool(
        conninfo=dsn,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    return PostgresSaver(pool)


def setup_checkpointer() -> None:
    """Create the checkpoint tables once (idempotent).

    Runs ``PostgresSaver.setup()`` on the cached checkpointer, which creates the
    LangGraph checkpoint tables if they do not already exist and succeeds
    without error when they do. Intended to be called from the AI persistence
    migration / application startup — the one place where opening a connection
    at a controlled time is appropriate — rather than at import.
    """
    get_checkpointer().setup()
