import hashlib
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.errors import AuthError
from app.db.models import AuthSession, User

# __Host- makes the browser refuse it unless Secure, Path=/ and no Domain, so no subdomain can plant one
COOKIE_NAME = "__Host-tr_session"
TOKEN_BYTES = 32
# token_urlsafe(32) is always 43 characters
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
# signed in for at most 30 days, and out after 7 days without a visit
LIFETIME = timedelta(days=30)
IDLE_TIMEOUT = timedelta(days=7)
# last_seen_at is written at most this often, not on every request
TOUCH_INTERVAL = timedelta(hours=1)


def token_digest(token: str) -> bytes:
    # the database keeps only this, a leaked table cannot be replayed as cookies
    return hashlib.sha256(token.encode()).digest()


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    return now


async def create_session(session: AsyncSession, *, user_id: uuid.UUID, now: datetime) -> str:
    # call inside a transaction, returns the raw token for the cookie
    now = _aware(now)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    session.add(
        AuthSession(user_id=user_id, token_hash=token_digest(token), created_at=now, last_seen_at=now, expires_at=now + LIFETIME)
    )
    await session.flush()
    return token


async def resolve_session(session: AsyncSession, token: str | None, *, now: datetime) -> User | None:
    # call inside a transaction, it may record the visit
    now = _aware(now)
    # a malformed cookie never reaches the database
    if token is None or not TOKEN_PATTERN.fullmatch(token):
        return None
    query = (
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(AuthSession.token_hash == token_digest(token))
    )
    row = (await session.execute(query)).one_or_none()
    if row is None:
        return None
    record, user = row
    if record.expires_at <= now or record.last_seen_at + IDLE_TIMEOUT <= now:
        await session.delete(record)
        return None
    if now - record.last_seen_at >= TOUCH_INTERVAL:
        record.last_seen_at = now
    await session.flush()
    return user


async def revoke_session(session: AsyncSession, token: str | None) -> None:
    if token is not None and TOKEN_PATTERN.fullmatch(token):
        await session.execute(delete(AuthSession).where(AuthSession.token_hash == token_digest(token)))


async def revoke_all(session: AsyncSession, *, user_id: uuid.UUID, keep_token: str | None = None) -> None:
    # after a password change or reset: every other device is signed out
    query = delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep_token is not None:
        query = query.where(AuthSession.token_hash != token_digest(keep_token))
    await session.execute(query)


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(LIFETIME.total_seconds()),
        path="/",
        # no script can read it, so an xss bug cannot steal the session
        httponly=True,
        secure=True,
        # sent on links from other sites, not on their form posts or fetches
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=True, samesite="lax")


def utc_now() -> datetime:
    return datetime.now(UTC)


async def current_user(request: Request) -> User:
    # dependency for routes that need a signed-in user, 401 otherwise
    sessions: async_sessionmaker[AsyncSession] | None = getattr(request.app.state, "db_sessions", None)
    if sessions is None:
        raise AuthError("accounts_unavailable")
    # tests set app.state.clock to control time
    clock: Callable[[], datetime] = getattr(request.app.state, "clock", utc_now)
    async with sessions() as session, session.begin():
        user = await resolve_session(session, request.cookies.get(COOKIE_NAME), now=clock())
    if user is None:
        raise AuthError("not_signed_in")
    return user