import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import dotenv_values
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.engine import check_database_url
from app.db.models import Base

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

config = context.config
# tests call alembic from code and keep their own logging
if config.config_file_name is not None and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name)


def database_url() -> str:
    # tests pass the url directly, the cli reads the environment first, then .env
    url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL") or dotenv_values(ENV_FILE).get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    try:
        return check_database_url(url)
    except ValueError as exc:
        raise SystemExit(f"DATABASE_URL {exc}") from None


def run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    # one short lived connection, no pool
    engine = create_async_engine(database_url(), poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline sql scripts are not supported, run against a database")
asyncio.run(run_online())