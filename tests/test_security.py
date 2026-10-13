import logging

import pytest
from pydantic import SecretStr

from app.security import (
    API_CACHE_CONTROL,
    CSP,
    DAY_S,
    DENIED,
    HOUR_S,
    REFERRER_POLICY,
    DemoGuard,
    check_access_code,
    client_key,
    security_headers,
)

MARKER = "SECRET-MARKER"
CODE = f"demo-{MARKER}"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def guard(code: str | None = None, per_ip: int = 5, daily: int = 100, **kwargs: object) -> tuple[DemoGuard, FakeClock]:
    clock = FakeClock()
    g = DemoGuard(SecretStr(code) if code is not None else None, per_ip, daily, clock=clock, **kwargs)  # type: ignore[arg-type]
    return g, clock


def codes(g: DemoGuard, n: int, host: str = "203.0.113.7", code: str | None = None) -> list[str]:
    out = []
    for _ in range(n):
        r = g.admit(host, code)
        out.append("ok" if r is None else r.code)
    return out


# client key


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("testclient", "testclient"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_client_key(host: str | None, key: str) -> None:
    assert client_key(host) == key


def test_client_key_bounded() -> None:
    assert len(client_key("x" * 500)) == 64


# per ip limit


def test_per_ip_limit() -> None:
    g, _ = guard(per_ip=3)
    assert codes(g, 4) == ["ok", "ok", "ok", "rate_limited"]


def test_per_ip_retry_after_and_recovery() -> None:
    g, clock = guard(per_ip=2)
    codes(g, 1)
    clock.now += 600
    codes(g, 1)
    refusal = g.admit("203.0.113.7", None)
    assert refusal is not None
    assert (refusal.status, refusal.code) == (429, "rate_limited")
    assert refusal.retry_after_s == 3000
    clock.now += 3000
    assert codes(g, 2) == ["ok", "rate_limited"]


def test_retry_after_rounds_up_and_is_at_least_one() -> None:
    g, clock = guard(per_ip=1)
    codes(g, 1)
    clock.now += HOUR_S - 0.2
    refusal = g.admit("203.0.113.7", None)
    assert refusal is not None and refusal.retry_after_s == 1


def test_addresses_are_independent() -> None:
    g, _ = guard(per_ip=1)
    assert codes(g, 2, "203.0.113.7") == ["ok", "rate_limited"]
    assert codes(g, 1, "203.0.113.8") == ["ok"]


def test_ipv6_neighbours_share_a_limit() -> None:
    g, _ = guard(per_ip=1)
    assert codes(g, 1, "2001:db8:1:2::1") == ["ok"]
    assert codes(g, 1, "2001:db8:1:2::99") == ["rate_limited"]
    assert codes(g, 1, "2001:db8:1:3::1") == ["ok"]


def test_refusals_do_not_extend_the_wait() -> None:
    g, clock = guard(per_ip=1)
    codes(g, 1)
    codes(g, 20)
    clock.now += HOUR_S
    assert codes(g, 1) == ["ok"]


def test_client_memory_is_bounded() -> None:
    g, _ = guard(per_ip=1, max_clients=3)
    for n in range(10):
        codes(g, 1, f"203.0.113.{n}")
    assert len(g._clients) == 3


# daily cap


def test_daily_cap_across_addresses() -> None:
    g, _ = guard(daily=3)
    seen = [codes(g, 1, f"203.0.113.{n}")[0] for n in range(5)]
    assert seen == ["ok", "ok", "ok", "daily_cap_reached", "daily_cap_reached"]


def test_daily_cap_is_rolling() -> None:
    g, clock = guard(daily=1)
    codes(g, 1, "203.0.113.1")
    clock.now += DAY_S - 10
    refusal = g.admit("203.0.113.2", None)
    assert refusal is not None
    assert (refusal.status, refusal.code, refusal.retry_after_s) == (429, "daily_cap_reached", 10)
    clock.now += 10
    assert codes(g, 1, "203.0.113.2") == ["ok"]


def test_daily_refusal_does_not_spend_the_ip_slot() -> None:
    g, clock = guard(per_ip=1, daily=1)
    codes(g, 1, "203.0.113.1")
    assert codes(g, 3, "203.0.113.2") == ["daily_cap_reached"] * 3
    clock.now += DAY_S
    assert codes(g, 1, "203.0.113.2") == ["ok"]


def test_ip_refusal_does_not_spend_the_daily_cap() -> None:
    g, _ = guard(per_ip=1, daily=2)
    codes(g, 5, "203.0.113.1")
    assert codes(g, 1, "203.0.113.2") == ["ok"]


# access code


def test_code_required_when_set() -> None:
    g, _ = guard(CODE)
    assert g.requires_code
    assert g.admit("203.0.113.7", None) == DENIED
    assert g.admit("203.0.113.7", "") == DENIED
    assert g.admit("203.0.113.7", CODE + "x") == DENIED
    assert g.admit("203.0.113.7", CODE.upper()) == DENIED
    assert g.admit("203.0.113.7", CODE) is None
    assert DENIED.status == 401 and DENIED.code == "access_denied"


def test_no_code_configured_ignores_header() -> None:
    g, _ = guard()
    assert not g.requires_code
    assert g.admit("203.0.113.7", "anything") is None


def test_wrong_codes_spend_the_ip_limit() -> None:
    g, _ = guard(CODE, per_ip=3)
    assert codes(g, 3, code="wrong-guess") == ["access_denied"] * 3
    # even the right code waits once the guesses used the hour up
    assert codes(g, 1, code=CODE) == ["rate_limited"]


def test_wrong_codes_do_not_spend_the_daily_cap() -> None:
    g, _ = guard(CODE, per_ip=1, daily=1)
    for n in range(5):
        codes(g, 1, f"203.0.113.{n}", code="wrong-guess")
    assert codes(g, 1, "203.0.113.99", code=CODE) == ["ok"]


def test_non_ascii_code_header_is_just_wrong() -> None:
    g, _ = guard(CODE)
    assert g.admit("203.0.113.7", "dému-\udcff-x") == DENIED


def test_compare_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.security as security

    seen: list[tuple[bytes, bytes]] = []
    real = security.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    g, _ = guard(CODE)
    g.admit("203.0.113.7", "nope-nope")
    assert seen == [(b"nope-nope", CODE.encode())]


@pytest.mark.parametrize("code", ["short", "has space1", "tab\there1", "ünicode-ok", "x" * 201])
def test_bad_codes_rejected(code: str) -> None:
    with pytest.raises(ValueError):
        check_access_code(code)
    with pytest.raises(ValueError):
        guard(code)


def test_code_length_bounds() -> None:
    assert check_access_code("x" * 8) == "x" * 8
    assert check_access_code("x" * 200) == "x" * 200


def test_blank_code_means_off() -> None:
    g, _ = guard("")
    assert not g.requires_code


@pytest.mark.parametrize(("per_ip", "daily"), [(0, 1), (1, 0)])
def test_bad_limits(per_ip: int, daily: int) -> None:
    with pytest.raises(ValueError):
        guard(per_ip=per_ip, daily=daily)


def test_refusal_logs_hold_no_address_or_code(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    g, _ = guard(CODE, per_ip=1, daily=1)
    g.admit("203.0.113.7", f"guess-{MARKER}")
    g.admit("203.0.113.7", CODE)
    g.admit("203.0.113.8", CODE)
    g.admit("203.0.113.9", CODE)
    messages = [r.getMessage() for r in caplog.records]
    assert messages
    for msg in messages:
        assert MARKER not in msg and "203.0.113" not in msg


# headers


def test_headers_for_pages() -> None:
    h = security_headers("/")
    assert h["Content-Security-Policy"] == CSP
    assert h["Referrer-Policy"] == REFERRER_POLICY == "strict-origin-when-cross-origin"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert "Cache-Control" not in h


def test_headers_for_api() -> None:
    assert security_headers("/api/itinerary")["Cache-Control"] == API_CACHE_CONTROL == "no-store"
    assert security_headers("/api")["Cache-Control"] == "no-store"
    assert "Cache-Control" not in security_headers("/apix")


@pytest.mark.parametrize("path", ["/docs", "/docs/oauth2-redirect", "/redoc"])
def test_docs_skip_csp(path: str) -> None:
    h = security_headers(path)
    assert "Content-Security-Policy" not in h
    assert h["X-Content-Type-Options"] == "nosniff"


def test_docs_prefix_is_exact() -> None:
    assert "Content-Security-Policy" in security_headers("/docsx")


def test_csp_content() -> None:
    parts = dict(p.split(" ", 1) for p in CSP.split("; "))
    assert parts["default-src"] == "'self'"
    assert parts["script-src"] == "'self'"
    # tiles only from the https osm tile server
    assert parts["img-src"] == "'self' data: https://tile.openstreetmap.org"
    assert parts["connect-src"] == "'self'"
    assert parts["frame-ancestors"] == "'none'"
    assert parts["object-src"] == "'none'"
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP


def test_headers_are_copies() -> None:
    security_headers("/docs")
    assert "Content-Security-Policy" in security_headers("/")