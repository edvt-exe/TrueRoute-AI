import asyncio
import logging
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth.captcha import SITEVERIFY_URL, CaptchaVerifier, FailedSignIns
from app.config import ConfigError, load_settings
from app.main import create_app
from app.security import CSP
from tests.test_config import minimal
from tests.test_main import fakes

SECRET = "0x4AAAAAAAreal-secret-for-tests"
TEST_SECRET = "1x0000000000000000000000000000000AA"
TOKEN = "XXXX.DUMMY.TOKEN.XXXX"


def verifier(answer: httpx.Response | Exception, secret: str = SECRET, seen: list[httpx.Request] | None = None) -> CaptchaVerifier:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return CaptchaVerifier(httpx.AsyncClient(transport=httpx.MockTransport(handler)), SecretStr(secret), call_timeout_s=0.5)


def check(v: CaptchaVerifier, token: str | None = TOKEN, action: str = "signup") -> bool:
    return asyncio.run(v.verify(token, action=action, remote_ip="203.0.113.7"))


def ok(**extra: object) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "error-codes": [], "action": "signup", **extra})


# siteverify


def test_valid_token_passes_and_sends_what_cloudflare_expects() -> None:
    seen: list[httpx.Request] = []
    assert check(verifier(ok(), seen=seen)) is True
    sent = parse_qs(seen[0].content.decode())
    assert str(seen[0].url) == SITEVERIFY_URL and seen[0].method == "POST"
    assert sent == {"secret": [SECRET], "response": [TOKEN], "remoteip": ["203.0.113.7"]}


def test_refused_or_mismatched_tokens_fail() -> None:
    # action matches, so only the success flag can fail it
    refused = httpx.Response(200, json={"success": False, "error-codes": ["timeout-or-duplicate"], "action": "signup"})
    assert check(verifier(refused)) is False
    # a token solved on the sign-in form cannot open sign-up
    assert check(verifier(ok(action="signin"))) is False


def test_missing_or_oversized_token_never_calls_cloudflare() -> None:
    seen: list[httpx.Request] = []
    v = verifier(ok(), seen=seen)
    assert check(v, None) is False
    assert check(v, "") is False
    assert check(v, "x" * 2049) is False
    assert seen == []


def test_upstream_trouble_fails_closed() -> None:
    # a human check that waves people through when cloudflare is down protects nothing
    for answer in (
        httpx.Response(500),
        httpx.Response(200, content=b"<html>blocked</html>"),
        # strict parsing: "yes" is not true
        httpx.Response(200, json={"success": "yes", "action": "signup"}),
        httpx.ConnectError("down"),
        httpx.ReadTimeout("slow"),
    ):
        assert check(verifier(answer)) is False, answer


def test_redirects_are_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example":
            return ok()
        return httpx.Response(302, headers={"Location": "https://evil.example/siteverify"})

    v = CaptchaVerifier(httpx.AsyncClient(transport=httpx.MockTransport(handler)), SecretStr(SECRET))
    assert check(v) is False


def test_test_keys_skip_only_the_action_check() -> None:
    # cloudflare's test secrets report no action, everything else is still enforced
    v = verifier(httpx.Response(200, json={"success": True, "error-codes": []}), secret=TEST_SECRET)
    assert v.is_test_key and check(v) is True
    assert not verifier(ok()).is_test_key


def test_secret_never_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        check(verifier(httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]})))
        check(verifier(httpx.ConnectError("down")))
    assert caplog.records
    assert SECRET not in caplog.text and TOKEN not in caplog.text


# failed sign-ins


def test_captcha_after_repeated_failures() -> None:
    now = [1000.0]
    tracker = FailedSignIns(threshold=3, window_s=900, clock=lambda: now[0])
    for _ in range(2):
        tracker.record_failure(account="Eduard", client="203.0.113.7")
    assert not tracker.needs_captcha(account="eduard", client="198.51.100.1")
    tracker.record_failure(account=" EDUARD ", client="203.0.113.7")
    # the account is flagged from any address, the address is flagged for any account
    assert tracker.needs_captcha(account="eduard", client="198.51.100.1")
    assert tracker.needs_captcha(account="someone-else", client="203.0.113.7")
    # a success clears the account but not the address that sprayed failures
    tracker.record_success(account="eduard")
    assert not tracker.needs_captcha(account="eduard", client="198.51.100.1")
    assert tracker.needs_captcha(account="someone-else", client="203.0.113.7")
    # the window rolls: failures older than 15 minutes stop counting
    now[0] += 901
    assert not tracker.needs_captcha(account="someone-else", client="203.0.113.7")
    for gap in (0, 600, 400):
        now[0] += gap
        tracker.record_failure(account="ana", client="192.0.2.1")
    # the first of the three is now 1000 s old, so only two count
    assert not tracker.needs_captcha(account="ana", client="192.0.2.1")


# page wiring and settings


def test_only_the_auth_page_allows_turnstile() -> None:
    app = create_app(services=fakes())
    app.state.captcha_site_key = "1x00000000000000000000AA"
    with TestClient(app) as client:
        auth_csp = client.get("/auth.html").headers["Content-Security-Policy"]
        assert "script-src 'self' https://challenges.cloudflare.com" in auth_csp
        assert "frame-src https://challenges.cloudflare.com" in auth_csp
        assert client.get("/").headers["Content-Security-Policy"] == CSP
        assert client.get("/api/auth/captcha").json() == {"site_key": "1x00000000000000000000AA"}
    with TestClient(create_app(services=fakes())) as client:
        assert client.get("/api/auth/captcha").json() == {"site_key": None}


def test_turnstile_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    others = f"DATABASE_URL=postgresql+asyncpg://u:p@localhost/db\nOTP_HMAC_KEY={'k' * 40}\n"
    pair = f"TURNSTILE_SITE_KEY=1x00000000000000000000AA\nTURNSTILE_SECRET_KEY={SECRET}\n{others}"
    settings = load_settings(minimal(tmp_path, pair))
    assert settings.turnstile_site_key == "1x00000000000000000000AA"
    assert SECRET not in repr(settings)
    with pytest.raises(ConfigError) as exc:
        load_settings(minimal(tmp_path, f"TURNSTILE_SECRET_KEY={SECRET}\n{others}"))
    assert "must be set together" in exc.value.message and SECRET not in exc.value.message