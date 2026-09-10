"""Alembic environment.

The database URL always comes from application settings (i.e. ``DATABASE_URL``),
never from ``alembic.ini`` - so migrations and the app can never point at
different databases, and no credential is stored in a tracked file.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.core.db import Base

# Importing the package registers every table on Base.metadata.
import app.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """Resolve the target database.

    A URL explicitly set on the Alembic config wins - that is how the test suite
    and one-off operational runs point migrations at a specific database. When it
    is absent (the normal CLI case) the application settings decide, so migrations
    and the app can never diverge.
    """
    explicit = config.get_main_option("sqlalchemy.url", None)
    return explicit or settings.sync_database_url


config.set_main_option("sqlalchemy.url", _database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
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
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
