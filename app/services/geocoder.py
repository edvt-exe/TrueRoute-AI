import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.schemas import BoundingBox, CityName, Coordinates, GeocodeResult

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://nominatim.openstreetmap.org"
# osmf policy: absolute max 1 request per second for the whole app
POLICY_MIN_INTERVAL_S = 1.0
# 10 percent margin over the policy limit
MIN_INTERVAL_S = 1.1
FOUND_TTL_S = 24 * 3600.0
NOT_FOUND_TTL_S = 3600.0
MAX_CACHE_ENTRIES = 256
# assumption: one jsonv2 hit is about 1 KB, so 100 KB is generous
MAX_BODY_BYTES = 100_000
# assumption: bounds from the design, tune with real latency data
TOTAL_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 5.0
# assumption: pause this long after a 403 or 429 instead of retrying
REFUSAL_COOLDOWN_S = 60.0
MAX_DISPLAY_NAME = 300
MAX_USER_AGENT = 200

# stock library and browser agents do not identify the app, policy rejects them
_STOCK_AGENT_PREFIXES = (
    "python-httpx",
    "httpx",
    "python-requests",
    "requests",
    "python-urllib",
    "urllib",
    "python/",
    "aiohttp",
    "curl/",
    "wget/",
    "mozilla/",
    "go-http-client",
    "okhttp",
    "java/",
    "node-fetch",
    "axios",
)

_city_adapter: TypeAdapter[str] = TypeAdapter(CityName)


class GeocoderError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CityNotFoundError(GeocoderError):
    pass


class GeocoderUnavailableError(GeocoderError):
    pass


class _NominatimHit(BaseModel):
    # third-party payload, unknown fields are ignored on purpose
    model_config = ConfigDict(extra="ignore")

    lat: float
    lon: float
    display_name: str
    # nominatim order is south, north, west, east
    boundingbox: Annotated[list[float], Field(min_length=4, max_length=4)] | None = None


@dataclass
class _CacheEntry:
    result: GeocodeResult | None
    expires_at: float


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    error: str | None = None


class _Spacer:
    def __init__(
        self,
        interval_s: float,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._interval_s = interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        # lock is held across the sleep so callers queue in order
        async with self._lock:
            now = self._clock()
            if self._last is not None:
                delay = self._last + self._interval_s - now
                if delay > 0:
                    await self._sleep(delay)
                    now = self._clock()
            self._last = now


def _check_user_agent(user_agent: str) -> str:
    value = user_agent.strip()
    if not value or len(value) > MAX_USER_AGENT or not value.isprintable():
        raise ValueError("user_agent must be a short printable string")
    if value.lower().startswith(_STOCK_AGENT_PREFIXES):
        raise ValueError("user_agent must identify the application, not the http library")
    return value


def _check_base_url(base_url: str) -> str:
    try:
        url = httpx.URL(base_url.strip())
    except httpx.InvalidURL:
        raise ValueError("base_url is not a valid url") from None
    if url.scheme != "https" or not url.host:
        raise ValueError("base_url must be an https url")
    if url.query or url.fragment or url.userinfo:
        raise ValueError("base_url must not contain a query, fragment or credentials")
    return str(url).rstrip("/")


def _cache_key(city: str) -> str:
    return " ".join(city.split()).casefold()


def _clean_display_name(raw: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in raw)
    return " ".join(text.split())[:MAX_DISPLAY_NAME].rstrip()


def _parse(city: str, body: bytes) -> GeocodeResult:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise GeocoderUnavailableError("geocoder returned invalid json") from None
    if not isinstance(data, list):
        raise GeocoderUnavailableError("unexpected geocoder response")
    if not data:
        raise CityNotFoundError("city not found")
    try:
        hit = _NominatimHit.model_validate(data[0])
        bbox: BoundingBox | None = None
        if hit.boundingbox is not None:
            south, north, west, east = hit.boundingbox
            bbox = BoundingBox(south=south, north=north, west=west, east=east)
        return GeocodeResult(
            query=city,
            display_name=_clean_display_name(hit.display_name),
            center=Coordinates(lat=hit.lat, lon=hit.lon),
            bbox=bbox,
        )
    except ValidationError:
        # from None so field values from the body are not kept in the chain
        raise GeocoderUnavailableError("unexpected geocoder response") from None


class Geocoder:
    def __init__(
        self,
        client: httpx.AsyncClient,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        min_interval_s: float = MIN_INTERVAL_S,
        found_ttl_s: float = FOUND_TTL_S,
        not_found_ttl_s: float = NOT_FOUND_TTL_S,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
        total_timeout_s: float = TOTAL_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if min_interval_s < POLICY_MIN_INTERVAL_S:
            raise ValueError("min_interval_s must be at least 1 second per nominatim policy")
        if found_ttl_s <= 0 or not_found_ttl_s <= 0:
            raise ValueError("cache ttls must be positive")
        if max_cache_entries < 1:
            raise ValueError("max_cache_entries must be at least 1")
        if total_timeout_s <= 0:
            raise ValueError("total_timeout_s must be positive")
        self._client = client
        self._user_agent = _check_user_agent(user_agent)
        self._search_url = _check_base_url(base_url) + "/search"
        self._found_ttl_s = found_ttl_s
        self._not_found_ttl_s = not_found_ttl_s
        self._max_cache_entries = max_cache_entries
        self._total_timeout_s = total_timeout_s
        self._http_timeout = httpx.Timeout(
            total_timeout_s, connect=min(CONNECT_TIMEOUT_S, total_timeout_s)
        )
        self._clock = clock
        self._spacer = _Spacer(min_interval_s, clock, sleep)
        # single event loop only, no awaits inside cache operations
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._slots: dict[str, _Slot] = {}
        self._blocked_until = 0.0

    async def geocode(self, city: str) -> GeocodeResult:
        try:
            clean = _city_adapter.validate_python(city)
        except ValidationError:
            raise ValueError("invalid city name") from None
        key = _cache_key(clean)
        found, cached = self._cache_get(key)
        if found:
            return _answer(clean, cached)
        slot = self._slots.get(key)
        if slot is None:
            slot = _Slot()
            self._slots[key] = slot
        slot.users += 1
        try:
            async with slot.lock:
                # concurrent callers share the outcome of the first fetch
                if slot.error is not None:
                    raise GeocoderUnavailableError(slot.error)
                found, cached = self._cache_get(key)
                if found:
                    return _answer(clean, cached)
                try:
                    result = await self._fetch(clean)
                except CityNotFoundError:
                    self._cache_put(key, None, self._not_found_ttl_s)
                    raise
                except GeocoderUnavailableError as exc:
                    slot.error = exc.message
                    raise
                self._cache_put(key, result, self._found_ttl_s)
                return result
        finally:
            slot.users -= 1
            if slot.users == 0:
                del self._slots[key]

    def _cache_get(self, key: str) -> tuple[bool, GeocodeResult | None]:
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        if self._clock() >= entry.expires_at:
            del self._cache[key]
            return False, None
        self._cache.move_to_end(key)
        return True, entry.result

    def _cache_put(self, key: str, result: GeocodeResult | None, ttl_s: float) -> None:
        self._cache[key] = _CacheEntry(result=result, expires_at=self._clock() + ttl_s)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)

    async def _fetch(self, city: str) -> GeocodeResult:
        if self._clock() < self._blocked_until:
            raise GeocoderUnavailableError("geocoder is cooling down after a refusal")
        await self._spacer.wait()
        try:
            async with asyncio.timeout(self._total_timeout_s):
                body = await self._get(city)
        except (TimeoutError, httpx.TimeoutException) as exc:
            logger.warning("geocoder timed out")
            raise GeocoderUnavailableError("geocoder timed out") from exc
        except httpx.HTTPError as exc:
            logger.warning("geocoder request failed: %s", type(exc).__name__)
            raise GeocoderUnavailableError("geocoder request failed") from exc
        return _parse(city, body)

    async def _get(self, city: str) -> bytes:
        params = {
            "q": city,
            "format": "jsonv2",
            "limit": "1",
            "featureType": "settlement",
            "accept-language": "en",
        }
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        async with self._client.stream(
            "GET",
            self._search_url,
            params=params,
            headers=headers,
            timeout=self._http_timeout,
            follow_redirects=False,
        ) as response:
            status = response.status_code
            if status in (403, 429):
                self._blocked_until = self._clock() + REFUSAL_COOLDOWN_S
                logger.warning("geocoder refused the request, http %s", status)
                raise GeocoderUnavailableError("geocoder refused the request, try again later")
            if status != 200:
                logger.warning("geocoder returned http %s", status)
                raise GeocoderUnavailableError(f"geocoder returned http {status}")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                raise GeocoderUnavailableError("geocoder response too large")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    raise GeocoderUnavailableError("geocoder response too large")
            return bytes(body)


def _answer(city: str, result: GeocodeResult | None) -> GeocodeResult:
    if result is None:
        raise CityNotFoundError("city not found")
    # cache is shared across spellings, report the caller's own spelling
    if result.query != city:
        return result.model_copy(update={"query": city})
    return result