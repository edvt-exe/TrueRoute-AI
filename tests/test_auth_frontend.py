import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import password_policy
from app.auth.errors import AUTH_ERRORS
from app.auth.schemas import OTP_PATTERN, USERNAME_PATTERN
from app.main import FRONTEND_DIR, create_app
from tests.test_frontend import Page
from tests.test_main import fakes

AUTH_HTML = (FRONTEND_DIR / "auth.html").read_text(encoding="utf-8")
AUTH_JS = (FRONTEND_DIR / "auth.js").read_text(encoding="utf-8")
INDEX = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
TURNSTILE_SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js"


def const(js: str, name: str) -> str:
    match = re.search(rf"^const {name} = (.+?);$", js, re.MULTILINE)
    assert match, f"{name} not found"
    return match.group(1)


def messages(js: str) -> set[str]:
    start = js.index("const MESSAGES = {")
    return set(re.findall(r"^  (\w+): ", js[start : js.index("};", start)], re.MULTILINE))


def ids(html: str) -> set[str]:
    page = Page()
    page.feed(html)
    return {attrs["id"] for _, attrs in page.tags if attrs.get("id")}


@pytest.fixture(scope="module")
def api_paths() -> Any:
    with TestClient(create_app(services=fakes())) as client:
        return set(client.get("/openapi.json").json()["paths"])


def test_every_account_error_has_a_message() -> None:
    assert set(AUTH_ERRORS) <= messages(AUTH_JS), set(AUTH_ERRORS) - messages(AUTH_JS)
    # the planner's account menu can hit these two
    assert {"invalid_credentials", "not_signed_in"} <= messages(APP_JS)


def test_password_rules_match_the_server() -> None:
    for name in ("MIN_LENGTH", "MAX_LENGTH", "MIN_PERSONAL"):
        assert int(const(AUTH_JS, name)) == getattr(password_policy, name), name
    assert int(const(AUTH_JS, "MIN_CLASSES")) == len(password_policy.CHARACTER_TYPES)
    assert const(AUTH_JS, "USERNAME_PATTERN") == f"/^{USERNAME_PATTERN.pattern}$/"
    assert const(AUTH_JS, "OTP_PATTERN") == f"/{OTP_PATTERN}/"


def test_every_called_endpoint_exists(api_paths: set[str]) -> None:
    called = set(re.findall(r'"(/api/auth/[a-z0-9/]+)"', AUTH_JS + APP_JS))
    assert len(called) >= 8
    assert called <= api_paths, called - api_paths


def test_scripts_only_touch_elements_that_exist() -> None:
    for js, html in ((AUTH_JS, AUTH_HTML), (APP_JS, INDEX)):
        used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js))
        assert used <= ids(html), used - ids(html)


def test_auth_page_stays_self_contained() -> None:
    page = Page()
    page.feed(AUTH_HTML)
    assert page.inline_scripts == 0
    for _, attrs in page.tags:
        assert "style" not in attrs and not any(k.startswith("on") for k in attrs)
        for key in ("src", "href"):
            assert not (attrs.get(key) or "").startswith(("http:", "https:", "//"))
    # turnstile is the one outside script, loaded by auth.js from cloudflare's exact url
    assert TURNSTILE_SCRIPT in AUTH_JS
    for banned in ("localStorage", "sessionStorage", "document.cookie", "innerHTML"):
        assert banned not in AUTH_JS