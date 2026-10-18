import hmac
import ipaddress
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import SecretStr

from app.utils.throttle import Clock, SlidingWindow, TTLCache

logger = logging.getLogger(__name__)

ACCESS_HEADER = "X-Access-Code"
HOUR_S = 3600.0
DAY_S = 86_400.0
DEFAULT_PER_IP_HOUR = 5
DEFAULT_DAILY_CAP = 100
# bounded memory, an address cycling attacker can only evict, the daily cap still holds
MAX_TRACKED_CLIENTS = 10_000
MIN_ACCESS_CODE = 8
MAX_ACCESS_CODE = 200
MAX_CLIENT_KEY = 64
# one ipv6 subscriber usually gets a whole /64
IPV6_PREFIX = 64

# api responses carry itineraries, no shared caches
API_CACHE_CONTROL = "no-store"
# osm tile policy needs a referer, so no no-referrer
REFERRER_POLICY = "strict-origin-when-cross-origin"
CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: https://tile.openstreetmap.org",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": REFERRER_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
# pages with the turnstile widget load its script and iframe from cloudflare, nothing else changes
CAPTCHA_PAGES = ("/auth.html",)
TURNSTILE_ORIGIN = "https://challenges.cloudflare.com"
CAPTCHA_CSP = CSP.replace("script-src 'self'", f"script-src 'self' {TURNSTILE_ORIGIN}").replace(
    "frame-ancestors 'none'", f"frame-src {TURNSTILE_ORIGIN}; frame-ancestors 'none'"
)
# swagger ui loads its own scripts and inline code, the strict csp would blank it
DOCS_PATHS = ("/docs", "/redoc")


@dataclass(frozen=True)
class Refusal:
    status: int
    code: str
    message: str
    retry_after_s: int | None = None


DENIED = Refusal(401, "access_denied", "a valid access code is required")


# methods that change state, another site can make the browser send these with our cookies
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# fetch metadata values that mean the request came from this site or the user typed the address
SAME_SITE_FETCH = frozenset({"same-origin", "none"})


def is_cross_site(method: str, headers: Mapping[str, str]) -> bool:
    # csrf defence, header names must be lowercase as starlette provides them
    if method.upper() not in UNSAFE_METHODS:
        return False
    # every current browser sends this and a page cannot forge it
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site not in SAME_SITE_FETCH
    # older browsers: the origin must name this host
    origin = headers.get("origin")
    if origin is None:
        # browsers send one of the two on cross-site posts, scripts and curl carry no victim cookies
        return False
    return origin == "null" or urlsplit(origin).netloc.lower() != headers.get("host", "").lower()


def check_access_code(value: str) -> str:
    if not MIN_ACCESS_CODE <= len(value) <= MAX_ACCESS_CODE:
        raise ValueError(f"must be {MIN_ACCESS_CODE} to {MAX_ACCESS_CODE} characters")
    if not value.isascii() or not value.isprintable() or any(ch.isspace() for ch in value):
        raise ValueError("must be printable ascii without spaces")
    return value


def client_key(host: str | None) -> str:
    # uvicorn already resolved trusted proxies into request.client, see FORWARDED_ALLOW_IPS
    if not host:
        return "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host[:MAX_CLIENT_KEY]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{ip}/{IPV6_PREFIX}", strict=False))
    return str(ip)


def _seconds(value: float) -> int:
    return max(1, math.ceil(value))


class DemoGuard:
    def __init__(
        self,
        access_code: SecretStr | None = None,
        per_ip_hour: int = DEFAULT_PER_IP_HOUR,
        daily_cap: int = DEFAULT_DAILY_CAP,
        *,
        max_clients: int = MAX_TRACKED_CLIENTS,
        clock: Clock = time.monotonic,
    ) -> None:
        # the daily window checks its own limit, per ip windows are built lazily
        if per_ip_hour < 1:
            raise ValueError("per_ip_hour must be at least 1")
        code = access_code.get_secret_value() if access_code is not None else ""
        self._code = check_access_code(code).encode() if code else None
        self._per_ip_hour = per_ip_hour
        self._clock = clock
        self._clients: TTLCache[str, SlidingWindow] = TTLCache(max_clients, clock)
        # sliding 24 h, no dependence on the wall clock or a timezone
        self._daily = SlidingWindow(daily_cap, DAY_S, clock)

    @property
    def requires_code(self) -> bool:
        return self._code is not None

    def _window(self, key: str) -> SlidingWindow:
        found, window = self._clients.get(key)
        if not found or window is None:
            window = SlidingWindow(self._per_ip_hour, HOUR_S, self._clock)
        # every touch keeps an active client for another hour
        self._clients.put(key, window, HOUR_S)
        return window

    def admit(self, host: str | None, supplied_code: str | None) -> Refusal | None:
        window = self._window(client_key(host))
        wait = window.retry_after()
        if wait > 0:
            return self._refuse(Refusal(429, "rate_limited", "too many requests, try again later", _seconds(wait)))
        if self._code is not None:
            given = (supplied_code or "").encode("utf-8", "replace")
            if not hmac.compare_digest(given, self._code):
                # a wrong guess spends a slot, so guessing is as slow as planning
                window.try_acquire()
                return self._refuse(DENIED)
        daily_wait = self._daily.retry_after()
        if daily_wait > 0:
            return self._refuse(
                Refusal(429, "daily_cap_reached", "the demo reached its daily limit, try again later", _seconds(daily_wait))
            )
        # both checked first, so a refusal never spends the other quota
        window.try_acquire()
        self._daily.try_acquire()
        return None

    def _refuse(self, refusal: Refusal) -> Refusal:
        # no address or code in the log
        logger.warning("demo guard refused: %s", refusal.code)
        return refusal


def security_headers(path: str) -> dict[str, str]:
    headers = dict(SECURITY_HEADERS)
    if path == "/api" or path.startswith("/api/"):
        headers["Cache-Control"] = API_CACHE_CONTROL
    if path in CAPTCHA_PAGES:
        headers["Content-Security-Policy"] = CAPTCHA_CSP
    if any(path == d or path.startswith(d + "/") for d in DOCS_PATHS):
        del headers["Content-Security-Policy"]
    return headers