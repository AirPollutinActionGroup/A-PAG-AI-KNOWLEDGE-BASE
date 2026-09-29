"""Alembic environment configuration."""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.core.config import settings
from src.db.models import Base

# Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Database URL: whatever the caller already set wins, otherwise fall back to app settings.
#
# The caller matters. Running `alembic` from the shell sets nothing, so settings.DATABASE_URL is
# used as before. But the integration test suite drives Alembic programmatically against a
# throwaway container, and an unconditional override here would point those migrations at the
# developer's real database — which the suite goes to some length to avoid touching.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata

# Tables that belong to extensions, not to this application.
#
# The ParadeDB image ships PostGIS and its own internals, so a *fresh* container already contains
# `spatial_ref_sys` and `_typmod_cache` before a single migration runs. `alembic check` compares
# the live schema against the ORM and reported them as tables it should drop, failing CI on a
# green migration chain. It passed locally only because the developer volume predates that image.
#
# Filtered by name rather than by "anything not in target_metadata": the latter would also
# silence the genuine drift this check exists to catch — a table created by a migration and never
# added to the ORM. If a future extension adds more tables, the failure names them and they get
# added here deliberately.
_EXTENSION_TABLES = {
    "spatial_ref_sys",   # PostGIS
    "_typmod_cache",     # ParadeDB internal
}


def include_object(obj, name, type_, reflected, compare_to):
    """Excludes extension-owned tables from autogenerate and `alembic check`."""
    return not (type_ == "table" and name in _EXTENSION_TABLES)



def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
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
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
