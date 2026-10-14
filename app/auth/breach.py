import asyncio
import hashlib
import logging
from enum import StrEnum

import httpx

from app.auth.passwords import normalize_password
from app.utils.http_guard import BodyTooLargeError, check_base_url, check_user_agent, read_capped

logger = logging.getLogger(__name__)

# pwned passwords range api, free, no key, k-anonymity: only 5 hex chars of the sha-1 leave the server
DEFAULT_BASE_URL = "https://api.pwnedpasswords.com"
PREFIX_LEN = 5
SUFFIX_LEN = 35
CALL_TIMEOUT_S = 3.0
# assumption from the api docs: a padded answer is 800 to 1000 lines of about 40 bytes
MAX_BODY_BYTES = 256_000


class BreachStatus(StrEnum):
    BREACHED = "breached"
    CLEAN = "clean"
    # the caller decides whether sign-up waits, fails or goes ahead
    UNKNOWN = "unknown"


def sha1_parts(password: str) -> tuple[str, str]:
    # sha-1 is what the api indexes, it is never used to store the password
    digest = hashlib.sha1(normalize_password(password).encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    return digest[:PREFIX_LEN], digest[PREFIX_LEN:]


def range_contains(body: bytes, suffix: str) -> bool:
    found = False
    for line in body.decode("ascii").splitlines():
        if not line.strip():
            continue
        tail, sep, count = line.strip().partition(":")
        if not sep or len(tail) != SUFFIX_LEN or not count.isdigit():
            raise ValueError("unexpected range line")
        # padding lines carry a count of 0 and are not real breaches
        if tail.upper() == suffix and int(count) > 0:
            found = True
    return found


class BreachChecker:
    def __init__(
        self,
        client: httpx.AsyncClient,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        call_timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        if call_timeout_s <= 0:
            raise ValueError("call_timeout_s must be positive")
        self._client = client
        self._user_agent = check_user_agent(user_agent)
        self._base = f"{check_base_url(base_url)}/range"
        self._call_timeout_s = call_timeout_s

    async def check(self, password: str) -> BreachStatus:
        prefix, suffix = sha1_parts(password)
        # padding hides the real answer size from anyone watching the traffic
        headers = {"User-Agent": self._user_agent, "Add-Padding": "true"}
        try:
            async with asyncio.timeout(self._call_timeout_s):
                async with self._client.stream(
                    "GET",
                    f"{self._base}/{prefix}",
                    headers=headers,
                    timeout=self._call_timeout_s,
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        logger.warning("pwned passwords returned http %s", response.status_code)
                        return BreachStatus.UNKNOWN
                    body = await read_capped(response, MAX_BODY_BYTES)
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("pwned passwords timed out")
            return BreachStatus.UNKNOWN
        except BodyTooLargeError:
            logger.warning("pwned passwords answer too large")
            return BreachStatus.UNKNOWN
        except httpx.HTTPError as exc:
            logger.warning("pwned passwords request failed: %s", type(exc).__name__)
            return BreachStatus.UNKNOWN
        try:
            found = range_contains(body, suffix)
        except (UnicodeDecodeError, ValueError):
            logger.warning("pwned passwords answer was not a range list")
            return BreachStatus.UNKNOWN
        return BreachStatus.BREACHED if found else BreachStatus.CLEAN