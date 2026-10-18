import hashlib
import hmac
import math
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OTP_PURPOSES, OtpChallenge, User

CODE_DIGITS = 6
TTL = timedelta(minutes=10)
# wrong guesses per code, 5 of a million possible codes is a 1 in 200 000 chance
MAX_ATTEMPTS = 5
# a new code for the same account waits this long
RESEND_COOLDOWN = timedelta(seconds=60)
# emails per account per hour, all purposes together, so nobody can flood an inbox
MAX_SENDS_PER_HOUR = 5
MIN_KEY_BYTES = 32
# 32 random bytes, url-safe, 43 characters, fits the request schema pattern
CHALLENGE_BYTES = 32


class OtpThrottledError(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__("otp throttled")
        self.retry_after_s = retry_after_s


def check_otp_key(key: bytes) -> bytes:
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(f"the otp key must be at least {MIN_KEY_BYTES} bytes")
    return key


def generate_code() -> str:
    # secrets, never random, the code is a credential
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def code_hmac(key: bytes, challenge_id: str, code: str) -> bytes:
    # bound to the challenge, so a code is worthless for any other challenge
    return hmac.new(key, f"{challenge_id}:{code}".encode(), hashlib.sha256).digest()


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    return now


def _seconds_until(moment: datetime, now: datetime) -> int:
    return max(1, math.ceil((moment - now).total_seconds()))


async def issue(session: AsyncSession, *, user_id: uuid.UUID, purpose: str, key: bytes, now: datetime) -> tuple[str, str]:
    # returns (challenge id for the browser, code for the email), call inside a transaction
    if purpose not in OTP_PURPOSES:
        raise ValueError("unknown otp purpose")
    now = _aware(now)
    # the account row lock makes concurrent requests for one account take turns
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())
    sent = select(OtpChallenge.created_at).where(OtpChallenge.user_id == user_id)
    last = (await session.execute(sent.order_by(OtpChallenge.created_at.desc()).limit(1))).scalar_one_or_none()
    if last is not None and now - last < RESEND_COOLDOWN:
        raise OtpThrottledError(_seconds_until(last + RESEND_COOLDOWN, now))
    hour_ago = now - timedelta(hours=1)
    recent = select(func.count(), func.min(OtpChallenge.created_at)).where(
        OtpChallenge.user_id == user_id, OtpChallenge.created_at > hour_ago
    )
    count, oldest = (await session.execute(recent)).one()
    if count >= MAX_SENDS_PER_HOUR and oldest is not None:
        raise OtpThrottledError(_seconds_until(oldest + timedelta(hours=1), now))
    # a new code retires every older open code for the same purpose
    await session.execute(
        update(OtpChallenge)
        .where(OtpChallenge.user_id == user_id, OtpChallenge.purpose == purpose, OtpChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    challenge_id = secrets.token_urlsafe(CHALLENGE_BYTES)
    code = generate_code()
    session.add(
        OtpChallenge(
            id=challenge_id,
            user_id=user_id,
            purpose=purpose,
            code_hmac=code_hmac(check_otp_key(key), challenge_id, code),
            expires_at=now + TTL,
            created_at=now,
        )
    )
    await session.flush()
    return challenge_id, code


async def verify(session: AsyncSession, *, challenge_id: str, code: str, purpose: str, key: bytes, now: datetime) -> uuid.UUID | None:
    # returns the account on success, otherwise None for every reason alike
    # never raises on a wrong code: the caller must commit, or the attempt count rolls back
    now = _aware(now)
    query = select(OtpChallenge).where(OtpChallenge.id == challenge_id).with_for_update()
    challenge = (await session.execute(query)).scalar_one_or_none()
    if challenge is None or challenge.consumed_at is not None or challenge.expires_at <= now:
        return None
    challenge.attempts += 1
    if challenge.attempts > MAX_ATTEMPTS or challenge.purpose != purpose:
        await session.flush()
        return None
    if not hmac.compare_digest(challenge.code_hmac, code_hmac(check_otp_key(key), challenge.id, code)):
        await session.flush()
        return None
    challenge.consumed_at = now
    await session.flush()
    return challenge.user_id