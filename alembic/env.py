"""
Alembic environment: uses app.db.base.Base and DATABASE_URL from environment.
Run from project root so `app` is importable.
"""
from logging.config import fileConfig

import os
import sys
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context

# Project root (parent of alembic/)
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# Load .env so DATABASE_URL is set
from dotenv import load_dotenv
load_dotenv(project_root / ".env")

# App's Base and all models (so metadata includes every table)
from app.db.base import Base
import app.models  # noqa: F401 — register all models with Base

config = context.config
target_metadata = Base.metadata

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

def get_url():
    url = os.getenv("DATABASE_URL")
    if not url:
        return config.get_main_option("sqlalchemy.url")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[10:]
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
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
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
