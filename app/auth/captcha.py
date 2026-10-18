import asyncio
import logging
import re
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.utils.http_guard import BodyTooLargeError, read_capped
from app.utils.throttle import Clock, TTLCache

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
CALL_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 16_384
# turnstile docs: tokens are at most 2048 characters
MAX_TOKEN = 2048
# turnstile docs: actions are up to 32 letters, digits, _ or -
ACTION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{10,100}")
# cloudflare's published test secrets, they accept only the dummy token and report no action
TEST_SECRETS = frozenset(
    {"1x0000000000000000000000000000000AA", "2x0000000000000000000000000000000AA", "3x0000000000000000000000000000000AA"}
)
# failed sign-ins before the human check is required, per account and per client, in a rolling window
FAILURES_BEFORE_CAPTCHA = 3
FAILURE_WINDOW_S = 900.0
MAX_TRACKED_KEYS = 10_000


def check_turnstile_key(value: str) -> str:
    if not KEY_PATTERN.fullmatch(value):
        raise ValueError("must be 10 to 100 letters, digits, _ or -")
    return value


class _Answer(BaseModel):
    # third-party payload, only the fields we rely on are read
    model_config = ConfigDict(extra="ignore", strict=True)

    success: bool
    action: str | None = None
    error_codes: list[str] = Field(default_factory=list, alias="error-codes")


class CaptchaVerifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        secret: SecretStr,
        *,
        url: str = SITEVERIFY_URL,
        call_timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        if call_timeout_s <= 0:
            raise ValueError("call_timeout_s must be positive")
        self._client = client
        self._secret = SecretStr(check_turnstile_key(secret.get_secret_value()))
        self._url = url
        self._call_timeout_s = call_timeout_s

    @property
    def is_test_key(self) -> bool:
        return self._secret.get_secret_value() in TEST_SECRETS

    async def verify(self, token: str | None, *, action: str, remote_ip: str | None) -> bool:
        # fails closed: any doubt, outage or odd answer counts as not human
        if not ACTION_PATTERN.fullmatch(action):
            raise ValueError("invalid captcha action")
        if not token or len(token) > MAX_TOKEN:
            return False
        form = {"secret": self._secret.get_secret_value(), "response": token}
        if remote_ip:
            form["remoteip"] = remote_ip
        try:
            async with asyncio.timeout(self._call_timeout_s):
                async with self._client.stream(
                    "POST", self._url, data=form, timeout=self._call_timeout_s, follow_redirects=False
                ) as response:
                    if response.status_code != 200:
                        logger.warning("turnstile returned http %s", response.status_code)
                        return False
                    body = await read_capped(response, MAX_BODY_BYTES)
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("turnstile timed out")
            return False
        except BodyTooLargeError:
            logger.warning("turnstile answer too large")
            return False
        except httpx.HTTPError as exc:
            logger.warning("turnstile request failed: %s", type(exc).__name__)
            return False
        try:
            answer = _Answer.model_validate_json(body)
        except ValidationError:
            logger.warning("turnstile answer was not the expected json")
            return False
        if not answer.success:
            # cloudflare's own codes such as timeout-or-duplicate, never the token
            logger.info("turnstile refused: %s", ",".join(c for c in answer.error_codes if c.isprintable())[:200])
            return False
        # a token solved for sign-in must not unlock sign-up; test keys report no action at all
        if answer.action != action and not self.is_test_key:
            logger.warning("turnstile action mismatch")
            return False
        return True


class FailedSignIns:
    # in memory, per process: fine for one worker, move to the database before running several
    def __init__(
        self,
        threshold: int = FAILURES_BEFORE_CAPTCHA,
        window_s: float = FAILURE_WINDOW_S,
        *,
        max_keys: int = MAX_TRACKED_KEYS,
        clock: Clock = time.monotonic,
    ) -> None:
        if threshold < 1 or window_s <= 0:
            raise ValueError("threshold and window_s must be positive")
        self._threshold = threshold
        self._window_s = window_s
        self._clock = clock
        self._seen: TTLCache[str, list[float]] = TTLCache(max_keys, clock)

    @staticmethod
    def _keys(account: str, client: str) -> tuple[str, str]:
        # the identifier as typed, casefolded, so it counts whether or not the account exists
        return f"account:{account.strip().casefold()}", f"client:{client}"

    def _recent(self, key: str) -> list[float]:
        now = self._clock()
        _, times = self._seen.get(key)
        return [t for t in times or [] if now - t < self._window_s]

    def needs_captcha(self, *, account: str, client: str) -> bool:
        return any(len(self._recent(key)) >= self._threshold for key in self._keys(account, client))

    def record_failure(self, *, account: str, client: str) -> None:
        for key in self._keys(account, client):
            # only the newest few matter, so memory per key stays bounded
            times = [*self._recent(key), self._clock()][-self._threshold :]
            self._seen.put(key, times, self._window_s)

    def record_success(self, *, account: str) -> None:
        # the client count stays: one success must not reset an attacker spraying many accounts
        account_key, _ = self._keys(account, "")
        self._seen.put(account_key, [], self._window_s)