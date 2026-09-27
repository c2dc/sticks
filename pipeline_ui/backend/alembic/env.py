"""Alembic migration environment.

Key decisions for task 1.3:

* The database URL is resolved from the application ``Settings`` (env var
  ``PIPELINE_UI_DATABASE_URL``, defaulting to local SQLite), so PostgreSQL and
  SQLite both work with no code changes — only the environment differs.
* ``target_metadata`` points at the shared ``Base.metadata``. The concrete
  domain models arrive in task 2.1; importing the models package here (via
  ``import_models``) ensures every future table is registered so
  ``alembic revision --autogenerate`` picks them up automatically.
* ``render_as_batch`` is enabled so SQLite migrations can ALTER tables (SQLite
  has limited native ALTER support); this is a no-op for PostgreSQL.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.db.base import Base, import_models

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime database URL from application settings (single source of
# truth). This keeps the URL out of alembic.ini and honours the env var.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Make sure every model is imported so its table is on Base.metadata. Today the
# models package is a placeholder (task 2.x); autogenerate becomes meaningful
# once the concrete models land, with no change needed here.
import_models()
target_metadata = Base.metadata


def _is_sqlite() -> bool:
    url = config.get_main_option("sqlalchemy.url") or ""
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live DB)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=_is_sqlite(),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against a live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=_is_sqlite(),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
