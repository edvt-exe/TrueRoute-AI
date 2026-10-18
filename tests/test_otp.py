import asyncio
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.email import ConsoleEmailSender, EmailMessage, code_email
from app.auth.otp import MAX_ATTEMPTS, MAX_SENDS_PER_HOUR, RESEND_COOLDOWN, TTL, OtpThrottledError, code_hmac, generate_code, issue, verify
from app.db.engine import make_engine, make_sessions
from app.db.models import OtpChallenge
from app.db.users import create_user
from tests.test_db import HASH, TEST_URL, needs_db, run, schema  # noqa: F401  schema builds the tables

KEY = b"k" * 32
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


async def new_user(session: AsyncSession) -> uuid.UUID:
    async with session.begin():
        user = await create_user(session, username="eduard", email="eduard@example.com", password_hash=HASH)
    return user.id


async def issue_at(session: AsyncSession, user_id: uuid.UUID, when: datetime, purpose: str = "verify_email") -> tuple[str, str]:
    async with session.begin():
        return await issue(session, user_id=user_id, purpose=purpose, key=KEY, now=when)


async def verify_at(session: AsyncSession, challenge_id: str, code: str, when: datetime, purpose: str = "verify_email") -> uuid.UUID | None:
    # committed like the route will, so failed attempts are counted for real
    async with session.begin():
        return await verify(session, challenge_id=challenge_id, code=code, purpose=purpose, key=KEY, now=when)


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


# codes and email, no database needed


def test_codes_are_six_digits_and_bound_to_their_challenge() -> None:
    # 200 draws: about 1 in 10 codes is below 100000 and needs its leading zero
    codes = [generate_code() for _ in range(200)]
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert code_hmac(KEY, "challenge-a", codes[0]) != code_hmac(KEY, "challenge-b", codes[0])


def test_console_sender_prints_the_email() -> None:
    stream = io.StringIO()
    asyncio.run(ConsoleEmailSender(stream).send(code_email("eduard@example.com", "signin_2fa", "042817")))
    printed = stream.getvalue()
    assert "To: eduard@example.com" in printed and "042817" in printed
    assert "development only" in printed


def test_email_headers_reject_line_breaks() -> None:
    # header injection: a newline would let text add a bcc line
    with pytest.raises(ValueError):
        EmailMessage(to="a@example.com\r\nBcc: victim@example.com", subject="hi", text="x")


# issue and verify


@needs_db
def test_code_works_once_and_is_stored_only_as_hmac() -> None:
    async def scenario(session: AsyncSession) -> None:
        user_id = await new_user(session)
        challenge_id, code = await issue_at(session, user_id, NOW)
        async with session.begin():
            stored = (await session.execute(select(OtpChallenge).where(OtpChallenge.id == challenge_id))).scalar_one()
        assert stored.code_hmac != code.encode() and len(stored.code_hmac) == 32
        assert await verify_at(session, challenge_id, code, NOW + timedelta(minutes=1)) == user_id
        # single use, the same code a second time fails
        assert await verify_at(session, challenge_id, code, NOW + timedelta(minutes=2)) is None

    run(scenario)


@needs_db
def test_failed_attempts_are_counted_and_lock_the_code() -> None:
    async def scenario(session: AsyncSession) -> None:
        user_id = await new_user(session)
        challenge_id, code = await issue_at(session, user_id, NOW)
        for _ in range(MAX_ATTEMPTS):
            assert await verify_at(session, challenge_id, wrong(code), NOW) is None
        # the right code is refused once the attempts ran out
        assert await verify_at(session, challenge_id, code, NOW) is None

    run(scenario)


@needs_db
def test_expired_or_wrong_purpose_codes_fail() -> None:
    async def scenario(session: AsyncSession) -> None:
        user_id = await new_user(session)
        challenge_id, code = await issue_at(session, user_id, NOW)
        # a sign-up code cannot finish a 2fa sign-in
        assert await verify_at(session, challenge_id, code, NOW, purpose="signin_2fa") is None
        assert await verify_at(session, challenge_id, code, NOW + TTL) is None

    run(scenario)


@needs_db
def test_resend_waits_and_retires_the_old_code() -> None:
    async def scenario(session: AsyncSession) -> None:
        user_id = await new_user(session)
        first_id, first_code = await issue_at(session, user_id, NOW)
        with pytest.raises(OtpThrottledError) as exc:
            await issue_at(session, user_id, NOW + timedelta(seconds=10))
        assert exc.value.retry_after_s == 50
        second_id, second_code = await issue_at(session, user_id, NOW + RESEND_COOLDOWN)
        assert await verify_at(session, first_id, first_code, NOW + RESEND_COOLDOWN) is None
        assert await verify_at(session, second_id, second_code, NOW + RESEND_COOLDOWN) == user_id

    run(scenario)


@needs_db
def test_hourly_email_cap() -> None:
    async def scenario(session: AsyncSession) -> None:
        user_id = await new_user(session)
        step = RESEND_COOLDOWN + timedelta(seconds=1)
        for i in range(MAX_SENDS_PER_HOUR):
            await issue_at(session, user_id, NOW + step * i)
        # the cap counts every purpose, switching purpose does not get around it
        with pytest.raises(OtpThrottledError):
            await issue_at(session, user_id, NOW + step * MAX_SENDS_PER_HOUR, purpose="signin_2fa")
        await issue_at(session, user_id, NOW + timedelta(hours=1, seconds=1))

    run(scenario)


@needs_db
def test_parallel_guesses_are_all_counted() -> None:
    async def scenario(session: AsyncSession) -> int:
        user_id = await new_user(session)
        challenge_id, code = await issue_at(session, user_id, NOW)
        engine = make_engine(TEST_URL)
        sessions = make_sessions(engine)

        async def guess() -> None:
            async with sessions() as own:
                await verify_at(own, challenge_id, wrong(code), NOW)

        try:
            # without the row lock, parallel guesses overwrite each other's count
            await asyncio.gather(*(guess() for _ in range(8)))
        finally:
            await engine.dispose()
        async with session.begin():
            row = (await session.execute(select(OtpChallenge).where(OtpChallenge.id == challenge_id))).scalar_one()
            return row.attempts

    assert run(scenario) == 8