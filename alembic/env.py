# Version: 0.1.0-beta

from logging.config import fileConfig
import os
import sys
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Add the parent directory to the path so we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import Base
from app.core.config import settings

# Import all models so they are registered with Base.metadata
import app.models.record  # noqa: F401
import app.models.camera  # noqa: F401
import app.models.project  # noqa: F401
import app.models.collection  # noqa: F401
import app.models.user  # noqa: F401
import app.models.system_log  # noqa: F401
import app.models.project_member  # noqa: F401
import app.models.offsite_object  # noqa: F401
import app.models.fisqua  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate and `alembic check` compare the database against this metadata.
target_metadata = Base.metadata


def get_url():
    """Get database URL from environment or settings."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    
    # Build from settings (using psycopg3 driver)
    return (
        f"postgresql+psycopg://{settings.DATABASE_USER}:{settings.DATABASE_PASSWORD}"
        f"@{settings.DATABASE_HOST}:{settings.DATABASE_PORT}/{settings.DATABASE_NAME}"
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = get_url()
    
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    from app.core.db import sqlite_foreign_keys_off

    with connectable.connect() as connection:
        context.configure(
            connection=connection, 
            target_metadata=target_metadata
        )

        # On SQLite, batch rebuilds of a referenced table must not fire the
        # referencing tables' ON DELETE actions (app.core.db.sqlite_foreign_keys_off).
        with sqlite_foreign_keys_off(connection):
            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
