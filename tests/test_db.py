import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ConfigError, load_settings
from app.db.engine import make_engine, make_sessions
from app.db.models import AuthSession, Base, OtpChallenge
from app.db.users import EmailTakenError, UsernameTakenError, create_user, find_user
from tests.test_config import minimal

# a throwaway database, every run drops and recreates the tables
TEST_URL = os.environ.get("TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parent.parent
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaGhhc2g"
T = TypeVar("T")

needs_db = pytest.mark.skipif(not TEST_URL, reason="set TEST_DATABASE_URL to run the database tests")


def alembic(action: str, target: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = TEST_URL
    cfg.attributes["skip_logging"] = True
    getattr(command, action)(cfg, target)


def run(test: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def go() -> T:
        engine = make_engine(TEST_URL)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("TRUNCATE users CASCADE"))
            async with make_sessions(engine)() as session:
                return await test(session)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture(scope="module", autouse=True)
def schema() -> None:
    if TEST_URL:
        # down and up again, so the downgrade path runs on every test run
        alembic("upgrade", "head")
        alembic("downgrade", "base")
        alembic("upgrade", "head")


# settings, no database needed


def test_database_url_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    good = "postgresql+asyncpg://trueroute:s3cret-pw@localhost:5432/trueroute"
    settings = load_settings(minimal(tmp_path, f"DATABASE_URL={good}\n"))
    assert settings.database_url is not None
    assert "s3cret-pw" not in repr(settings)
    with pytest.raises(ConfigError) as exc:
        load_settings(minimal(tmp_path, "DATABASE_URL=postgresql://trueroute:s3cret-pw@localhost/trueroute\n"))
    assert "must start with postgresql+asyncpg://" in exc.value.message
    assert "s3cret-pw" not in exc.value.message


# schema


@needs_db
def test_migration_matches_models() -> None:
    async def check(session: AsyncSession) -> list[object]:
        conn = await session.connection()
        return await conn.run_sync(lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))

    # an empty diff means the hand written migration builds exactly what the models describe
    assert run(check) == []


# users


@needs_db
def test_create_and_find_user() -> None:
    async def scenario(session: AsyncSession) -> None:
        async with session.begin():
            created = await create_user(session, username="Eduard_V", email="Eduard@Example.com", password_hash=HASH)
        by_name = await find_user(session, " EDUARD_v ")
        by_email = await find_user(session, "eduard@EXAMPLE.com")
        assert by_name is not None and by_name.id == created.id
        assert by_email is not None and by_email.id == created.id
        assert (by_name.username, by_name.email, by_name.two_factor_enabled) == ("Eduard_V", "eduard@example.com", False)
        assert by_name.email_verified_at is None
        assert await find_user(session, "nobody") is None

    run(scenario)


@needs_db
def test_duplicates_are_named_and_leave_the_transaction_usable() -> None:
    async def scenario(session: AsyncSession) -> None:
        async with session.begin():
            await create_user(session, username="ana", email="ana@example.com", password_hash=HASH)
            with pytest.raises(UsernameTakenError):
                await create_user(session, username="ANA", email="other@example.com", password_hash=HASH)
            with pytest.raises(EmailTakenError):
                await create_user(session, username="ana2", email="ANA@example.com", password_hash=HASH)
            # the savepoint rolled back only the duplicate
            await create_user(session, username="bob", email="bob@example.com", password_hash=HASH)
        total = (await session.execute(select(func.count()).select_from(text("users")))).scalar_one()
        assert total == 2

    run(scenario)


@needs_db
def test_deleting_a_user_removes_sessions_and_codes() -> None:
    async def scenario(session: AsyncSession) -> tuple[int, int]:
        later = datetime.now(UTC) + timedelta(hours=1)
        async with session.begin():
            user = await create_user(session, username="carol", email="carol@example.com", password_hash=HASH)
            session.add(AuthSession(user_id=user.id, token_hash=b"t" * 32, expires_at=later))
            session.add(OtpChallenge(id="c" * 43, user_id=user.id, purpose="verify_email", code_hmac=b"h" * 32, expires_at=later))
        async with session.begin():
            await session.delete(user)
        sessions = (await session.execute(select(func.count()).select_from(AuthSession))).scalar_one()
        codes = (await session.execute(select(func.count()).select_from(OtpChallenge))).scalar_one()
        return sessions, codes

    assert run(scenario) == (0, 0)


@needs_db
def test_database_rejects_bad_rows() -> None:
    async def scenario(session: AsyncSession) -> None:
        later = datetime.now(UTC) + timedelta(hours=1)
        async with session.begin():
            user = await create_user(session, username="dan", email="dan@example.com", password_hash=HASH)
        # a rollback expires loaded objects, so keep the id as a plain value
        user_id = user.id
        # the check constraints hold even if application code has a bug
        for bad in (
            AuthSession(user_id=user_id, token_hash=b"short", expires_at=later),
            OtpChallenge(id="d" * 43, user_id=user_id, purpose="anything", code_hmac=b"h" * 32, expires_at=later),
        ):
            with pytest.raises(IntegrityError) as exc:
                async with session.begin():
                    session.add(bad)
            # hide_parameters keeps row values out of error text and logs
            assert "parameters hidden" in str(exc.value)
            assert "anything" not in str(exc.value) and str(user_id) not in str(exc.value)

    run(scenario)