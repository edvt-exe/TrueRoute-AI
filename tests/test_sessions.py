import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import (
    COOKIE_NAME,
    IDLE_TIMEOUT,
    LIFETIME,
    TOUCH_INTERVAL,
    clear_session_cookie,
    create_session,
    current_user,
    resolve_session,
    revoke_all,
    revoke_session,
    set_session_cookie,
)
from app.db.engine import make_engine, make_sessions
from app.db.models import AuthSession, User
from app.db.users import create_user
from app.main import create_app
from app.security import is_cross_site
from tests.test_db import HASH, TEST_URL, needs_db, run, schema  # noqa: F401  schema builds the tables
from tests.test_main import BODY, fakes

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


async def new_user(session: AsyncSession, name: str = "eduard") -> uuid.UUID:
    async with session.begin():
        user = await create_user(session, username=name, email=f"{name}@example.com", password_hash=HASH)
    return user.id


async def login(session: AsyncSession, user_id: uuid.UUID, when: datetime = NOW) -> str:
    async with session.begin():
        return await create_session(session, user_id=user_id, now=when)


async def who(session: AsyncSession, token: str | None, when: datetime) -> User | None:
    async with session.begin():
        return await resolve_session(session, token, now=when)


async def count(session: AsyncSession) -> int:
    async with session.begin():
        return (await session.execute(select(func.count()).select_from(AuthSession))).scalar_one()


# cookie and csrf, no database needed


def test_cookie_is_host_only_httponly_secure_lax() -> None:
    response = Response()
    set_session_cookie(response, "t" * 43)
    cookie = response.headers["set-cookie"]
    # the prefix makes browsers enforce Secure, Path=/ and no Domain
    assert COOKIE_NAME.startswith("__Host-")
    assert cookie.startswith(f"{COOKIE_NAME}={'t' * 43};")
    for part in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", f"Max-Age={int(LIFETIME.total_seconds())}"):
        assert part in cookie
    # a domain attribute would break the __Host- prefix and widen the cookie to subdomains
    assert "Domain" not in cookie
    cleared = Response()
    clear_session_cookie(cleared)
    assert "Max-Age=0" in cleared.headers["set-cookie"]


def test_cross_site_rules() -> None:
    host = {"host": "trueroute.example"}
    # fetch metadata decides when present
    assert is_cross_site("POST", {**host, "sec-fetch-site": "cross-site"})
    assert is_cross_site("POST", {**host, "sec-fetch-site": "same-site"})
    assert not is_cross_site("POST", {**host, "sec-fetch-site": "same-origin"})
    # older browsers: the origin must match the host
    assert is_cross_site("DELETE", {**host, "origin": "https://evil.example"})
    assert is_cross_site("POST", {**host, "origin": "null"})
    assert not is_cross_site("POST", {**host, "origin": "https://trueroute.example"})
    # reads are never refused, and clients without either header carry no browser cookies
    for safe in ("GET", "HEAD", "OPTIONS"):
        assert not is_cross_site(safe, {**host, "sec-fetch-site": "cross-site"})
    assert not is_cross_site("POST", host)


def test_app_refuses_cross_site_posts() -> None:
    with TestClient(create_app(services=fakes())) as client:
        refused = client.post("/api/itinerary", json=BODY, headers={"Sec-Fetch-Site": "cross-site"})
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "cross_site_request"
        assert client.post("/api/itinerary", json=BODY, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


# sessions


@needs_db
def test_session_round_trip_stores_only_a_digest() -> None:
    async def scenario(session: AsyncSession) -> None:
        user_id = await new_user(session)
        token = await login(session, user_id)
        user = await who(session, token, NOW + timedelta(minutes=5))
        assert user is not None and user.id == user_id
        async with session.begin():
            stored = (await session.execute(select(AuthSession.token_hash))).scalar_one()
        assert stored == hashlib.sha256(token.encode()).digest()
        # a malformed or unknown cookie is simply not signed in
        assert await who(session, "not-a-token", NOW) is None
        assert await who(session, "A" * 43, NOW) is None

    run(scenario)


@needs_db
def test_idle_and_absolute_expiry() -> None:
    async def scenario(session: AsyncSession) -> None:
        user_id = await new_user(session)
        idle = await login(session, user_id)
        assert await who(session, idle, NOW + IDLE_TIMEOUT) is None
        # an expired session is deleted, not just ignored
        assert await count(session) == 0
        active = await login(session, user_id)
        # visiting every 6 days keeps it alive, but never past the 30 day lifetime
        visit = NOW
        while visit + timedelta(days=6) < NOW + LIFETIME:
            visit += timedelta(days=6)
            assert await who(session, active, visit) is not None
        assert await who(session, active, NOW + LIFETIME) is None

    run(scenario)


@needs_db
def test_visits_are_recorded_at_most_hourly() -> None:
    async def scenario(session: AsyncSession) -> list[datetime]:
        user_id = await new_user(session)
        token = await login(session, user_id)
        seen = []
        for later in (timedelta(minutes=30), TOUCH_INTERVAL):
            await who(session, token, NOW + later)
            async with session.begin():
                seen.append((await session.execute(select(AuthSession.last_seen_at))).scalar_one())
        return seen

    assert run(scenario) == [NOW, NOW + TOUCH_INTERVAL]


@needs_db
def test_sign_out_and_sign_out_everywhere_else() -> None:
    async def scenario(session: AsyncSession) -> None:
        user_id = await new_user(session)
        other_id = await new_user(session, "ana")
        phone = await login(session, user_id)
        laptop = await login(session, user_id)
        tablet = await login(session, user_id)
        others = await login(session, other_id)
        async with session.begin():
            await revoke_session(session, phone)
        assert await who(session, phone, NOW) is None
        async with session.begin():
            await revoke_all(session, user_id=user_id, keep_token=laptop)
        assert await who(session, laptop, NOW) is not None
        assert await who(session, tablet, NOW) is None
        # another account is never touched
        assert await who(session, others, NOW) is not None

    run(scenario)


@needs_db
def test_current_user_dependency() -> None:
    token = run(lambda s: _signed_in(s))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(TEST_URL)
        app.state.db_sessions = make_sessions(engine)
        app.state.clock = lambda: NOW + timedelta(minutes=1)
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)

    @app.get("/me")
    async def me(user: User = Depends(current_user)) -> dict[str, str]:
        return {"username": user.username}

    with TestClient(app) as client:
        assert client.get("/me").status_code == 401
        assert client.get("/me", headers={"Cookie": f"{COOKIE_NAME}={'x' * 43}"}).status_code == 401
        signed_in = client.get("/me", headers={"Cookie": f"{COOKIE_NAME}={token}"})
        assert signed_in.status_code == 200 and signed_in.json() == {"username": "eduard"}


async def _signed_in(session: AsyncSession) -> str:
    return await login(session, await new_user(session))