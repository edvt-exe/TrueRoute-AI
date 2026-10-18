import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.breach import BreachStatus
from app.auth.captcha import FailedSignIns
from app.auth.email import EmailMessage
from app.auth.otp import RESEND_COOLDOWN
from app.auth.passwords import Passwords
from app.auth.routes import AuthServices
from app.auth.sessions import COOKIE_NAME
from app.config import ConfigError, load_settings
from app.db.engine import make_engine, make_sessions
from app.main import create_app
from tests.test_config import minimal
from tests.test_db import TEST_URL, needs_db, run, schema  # noqa: F401  schema builds the tables
from tests.test_main import fakes

PW = "Walk-the-Seine-42"
START = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class FakeCaptcha:
    # "ok-<action>" passes for that action only, like a real token bound to its form
    async def verify(self, token: str | None, *, action: str, remote_ip: str | None) -> bool:
        return token == f"ok-{action}"


class FakeBreach:
    def __init__(self) -> None:
        self.status = BreachStatus.CLEAN

    async def check(self, password: str) -> BreachStatus:
        return self.status


class Outbox:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def code(self) -> str:
        match = re.search(r"code is (\d{6})", self.sent[-1].text)
        assert match, self.sent[-1].text
        return match.group(1)


class World:
    def __init__(self) -> None:
        self.now = START
        self.outbox = Outbox()
        self.breach = FakeBreach()


async def _empty(session: AsyncSession) -> None:
    return None


@pytest.fixture
def world() -> Iterator[tuple[TestClient, World]]:
    run(_empty)  # wipes the tables
    w = World()

    @asynccontextmanager
    async def factory() -> AsyncIterator[AuthServices]:
        engine = make_engine(TEST_URL)
        try:
            yield AuthServices(
                db=make_sessions(engine),
                passwords=Passwords(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)),
                breach=w.breach,
                captcha=FakeCaptcha(),
                email=w.outbox,
                otp_key=b"k" * 32,
                failed=FailedSignIns(),
                clock=lambda: w.now,
            )
        finally:
            await engine.dispose()

    # https, so the client keeps the Secure session cookie like a browser does
    with TestClient(create_app(services=fakes(), auth_factory=factory), base_url="https://testserver") as client:
        yield client, w


def signup(client: TestClient, username: str = "eduard", email: str = "eduard@example.com", token: str = "ok-signup") -> dict:
    res = client.post(
        "/api/auth/signup",
        json={"username": username, "email": email, "password": PW, "password_confirm": PW, "captcha_token": token},
    )
    return {"http": res.status_code, **res.json()}


def verify(client: TestClient, challenge_id: str, code: str) -> dict:
    res = client.post("/api/auth/otp/verify", json={"challenge_id": challenge_id, "code": code})
    return {"http": res.status_code, **res.json()}


def signin(client: TestClient, identifier: str = "eduard", password: str = PW, captcha: str | None = None) -> dict:
    body = {"identifier": identifier, "password": password} | ({"captcha_token": captcha} if captcha else {})
    res = client.post("/api/auth/signin", json=body)
    return {"http": res.status_code, **res.json()}


def code_of(result: dict) -> str:
    return result["error"]["code"]


def registered(client: TestClient, w: World) -> None:
    started = signup(client)
    assert verify(client, started["challenge_id"], w.outbox.code())["status"] == "signed_in"


# sign-up and email confirmation


@needs_db
def test_sign_up_confirm_and_use_the_account(world: tuple[TestClient, World]) -> None:
    client, w = world
    started = signup(client)
    assert started["http"] == 202 and started["status"] == "otp_required"
    assert w.outbox.sent[-1].to == "eduard@example.com"
    # nothing is signed in before the code is entered
    assert client.get("/api/auth/me").status_code == 401
    done = verify(client, started["challenge_id"], w.outbox.code())
    assert done["http"] == 200 and done == {"http": 200, "status": "signed_in", "username": "eduard"}
    me = client.get("/api/auth/me").json()
    assert me == {"username": "eduard", "email": "eduard@example.com", "two_factor_enabled": False}
    # the code worked once and is now spent
    assert code_of(verify(client, started["challenge_id"], w.outbox.code())) == "invalid_code"


@needs_db
def test_taken_email_looks_like_success_but_only_warns_the_owner(world: tuple[TestClient, World]) -> None:
    client, w = world
    registered(client, w)
    again = signup(client, username="someone_else", email="EDUARD@example.com")
    # same status and shape as a fresh sign-up, so it does not reveal the account exists
    assert again["http"] == 202 and again["status"] == "otp_required" and len(again["challenge_id"]) == 43
    assert w.outbox.sent[-1].subject == "Sign-up attempt with your email"
    assert "code is" not in w.outbox.sent[-1].text
    assert code_of(verify(client, again["challenge_id"], "123456")) == "invalid_code"
    # a taken username cannot be hidden, it is part of the public profile
    assert code_of(signup(client, email="other@example.com")) == "username_taken"


@needs_db
def test_sign_up_needs_the_human_check_and_a_clean_password(world: tuple[TestClient, World]) -> None:
    client, w = world
    # a token solved on the sign-in form is refused here
    assert code_of(signup(client, token="ok-signin")) == "captcha_failed"
    w.breach.status = BreachStatus.BREACHED
    assert code_of(signup(client)) == "password_breached"
    # the breach service being down does not block sign-ups
    w.breach.status = BreachStatus.UNKNOWN
    assert signup(client)["http"] == 202


# sign-in


@needs_db
def test_wrong_passwords_then_the_human_check(world: tuple[TestClient, World]) -> None:
    client, w = world
    registered(client, w)
    client.post("/api/auth/signout")
    # wrong password and unknown account answer the same
    assert code_of(signin(client, password="Wrong-Password-1")) == "invalid_credentials"
    assert code_of(signin(client, identifier="nobody")) == "invalid_credentials"
    assert code_of(signin(client, password="Wrong-Password-1")) == "invalid_credentials"
    # three failures from this client, now even the right password needs the check
    assert code_of(signin(client)) == "captcha_required"
    assert code_of(signin(client, captcha="ok-signup")) == "captcha_failed"
    assert signin(client, captcha="ok-signin")["status"] == "signed_in"
    assert client.get("/api/auth/me").status_code == 200


@needs_db
def test_unconfirmed_email_must_be_confirmed_at_sign_in(world: tuple[TestClient, World]) -> None:
    client, w = world
    signup(client)
    w.now += RESEND_COOLDOWN
    pending = signin(client, identifier="eduard@example.com")
    assert pending["http"] == 200 and pending["status"] == "otp_required"
    assert verify(client, pending["challenge_id"], w.outbox.code())["status"] == "signed_in"


@needs_db
def test_two_factor_sign_in(world: tuple[TestClient, World]) -> None:
    client, w = world
    registered(client, w)
    # turning 2fa on needs the password again, a session alone is not enough
    assert client.post("/api/auth/2fa", json={"enabled": True, "password": "Wrong-Password-1"}).status_code == 401
    assert client.post("/api/auth/2fa", json={"enabled": True, "password": PW}).json()["two_factor_enabled"] is True
    first_cookie = client.cookies.get(COOKIE_NAME)
    w.now += RESEND_COOLDOWN
    pending = signin(client)
    assert pending["status"] == "otp_required"
    assert w.outbox.sent[-1].subject == "Your TrueRoute sign-in code"
    assert verify(client, pending["challenge_id"], w.outbox.code())["status"] == "signed_in"
    assert client.get("/api/auth/me").status_code == 200
    # signing in again replaced the session, the earlier cookie is dead
    assert client.cookies.get(COOKIE_NAME) != first_cookie
    assert client.get("/api/auth/me", headers={"Cookie": f"{COOKIE_NAME}={first_cookie}"}).status_code == 401
    assert client.post("/api/auth/2fa", json={"enabled": False, "password": PW}).json()["two_factor_enabled"] is False


# sessions and codes


@needs_db
def test_sign_out_ends_the_session_for_good(world: tuple[TestClient, World]) -> None:
    client, w = world
    registered(client, w)
    old_cookie = client.cookies.get(COOKIE_NAME)
    assert client.post("/api/auth/signout").json() == {"status": "signed_out"}
    assert client.get("/api/auth/me").status_code == 401
    # replaying the old cookie does not work either, the session is gone from the database
    assert client.get("/api/auth/me", headers={"Cookie": f"{COOKIE_NAME}={old_cookie}"}).status_code == 401


@needs_db
def test_resend_replaces_the_code_and_is_throttled(world: tuple[TestClient, World]) -> None:
    client, w = world
    first = signup(client)
    first_code = w.outbox.code()
    early = client.post("/api/auth/otp/resend", json={"challenge_id": first["challenge_id"]})
    assert early.status_code == 429 and early.headers["Retry-After"] == "60"
    w.now += RESEND_COOLDOWN
    second = client.post("/api/auth/otp/resend", json={"challenge_id": first["challenge_id"]}).json()
    assert second["challenge_id"] != first["challenge_id"]
    assert code_of(verify(client, first["challenge_id"], first_code)) == "invalid_code"
    assert verify(client, second["challenge_id"], w.outbox.code())["status"] == "signed_in"


# configuration


@needs_db
def test_production_startup_wires_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY", "OTP_HMAC_KEY"):
        monkeypatch.delenv(name, raising=False)
    accounts = (
        f"DATABASE_URL={TEST_URL}\nTURNSTILE_SITE_KEY=1x00000000000000000000AA\n"
        f"TURNSTILE_SECRET_KEY=1x0000000000000000000000000000000AA\nOTP_HMAC_KEY={'k' * 40}\n"
    )
    settings = load_settings(minimal(tmp_path, accounts))
    # the real lifespan builds the account services from settings, nothing injected
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get("/api/auth/captcha").json() == {"site_key": "1x00000000000000000000AA"}
        res = client.post("/api/auth/signin", json={"identifier": "nobody", "password": PW})
        assert res.status_code == 401 and res.json()["error"]["code"] == "invalid_credentials"


def test_accounts_off_without_configuration() -> None:
    with TestClient(create_app(services=fakes()), base_url="https://testserver") as client:
        res = client.post("/api/auth/signin", json={"identifier": "eduard", "password": PW})
        assert res.status_code == 503 and res.json()["error"]["code"] == "accounts_unavailable"


def test_account_settings_are_all_or_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY", "OTP_HMAC_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as exc:
        load_settings(minimal(tmp_path, "DATABASE_URL=postgresql+asyncpg://u:p@localhost/db\n"))
    assert "accounts also need TURNSTILE_SECRET_KEY, OTP_HMAC_KEY" in exc.value.message
    with pytest.raises(ConfigError) as exc:
        load_settings(minimal(tmp_path, "OTP_HMAC_KEY=too-short\n"))
    assert "at least 32" in exc.value.message and "too-short" not in exc.value.message