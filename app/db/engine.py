from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

DRIVER = "postgresql+asyncpg"
# assumption: sized for one small server, raise with the postgres max_connections
POOL_SIZE = 5
MAX_OVERFLOW = 5
POOL_TIMEOUT_S = 10.0


def check_database_url(value: str) -> str:
    # messages never repeat the url, it holds the database password
    try:
        url = make_url(value.strip())
    except ArgumentError:
        raise ValueError("must be a database url") from None
    if url.drivername != DRIVER:
        raise ValueError(f"must start with {DRIVER}://")
    if not url.host or not url.database:
        raise ValueError("must name a host and a database")
    return value.strip()


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        check_database_url(url),
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT_S,
        # drops connections the server closed while they sat idle
        pool_pre_ping=True,
        # sql errors would otherwise quote emails and hashes into the logs
        hide_parameters=True,
    )


def make_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # objects stay readable after commit, routes return them after the transaction
    return async_sessionmaker(engine, expire_on_commit=False)