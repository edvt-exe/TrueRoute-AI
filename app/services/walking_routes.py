import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, SecretStr, ValidationError

from app.schemas import (
    MAX_ROUTE_POINTS,
    DayPlan,
    DayStop,
    DistanceSource,
    Itinerary,
    LatLon,
    Lat,
    Lon,
    disclaimer_for,
)
from app.utils.http_guard import BodyTooLargeError, check_base_url, check_user_agent, read_capped
from app.utils.throttle import Clock, Cooldown, SlidingWindow, TTLCache

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openrouteservice.org"
PROFILE = "foot-walking"
# ors terms: show this wherever ors results are displayed
ORS_ATTRIBUTION = "© openrouteservice.org by HeiGIT | Map data © OpenStreetMap contributors"
# ors free plan: 40 directions per minute, sliding
PER_MINUTE_LIMIT = 40
CALL_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 5.0
# all days of one request together, later days fall back to aerial
BUDGET_S = 20.0
MINUTE_COOLDOWN_S = 60.0
DAY_S = 86_400.0
CACHE_TTL_S = 24 * 3600.0
MAX_CACHE_ENTRIES = 256
# assumption: 5 walking stops with instructions are well under 200 KB
MAX_BODY_BYTES = 1_000_000
COORD_DECIMALS = 6
ROUTE_DECIMALS = 5
# assumption: ors snaps each end up to 350 m, so a walk may look shorter than the aerial line
SNAP_TOLERANCE_M = 700.0

NonNeg = Annotated[FiniteFloat, Field(ge=0)]
# geojson order is lon, lat, elevation is optional
_Position = tuple[Lon, Lat] | tuple[Lon, Lat, float]

StopKey = tuple[tuple[float, float], ...]
WallClock = Callable[[], float]


class RouteError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Ors(BaseModel):
    # third-party payload, unknown fields are ignored on purpose
    model_config = ConfigDict(extra="ignore")


class _Segment(_Ors):
    distance: NonNeg
    duration: NonNeg


class _Properties(_Ors):
    segments: list[_Segment]
    way_points: list[Annotated[int, Field(ge=0)]]


class _Geometry(_Ors):
    type: Literal["LineString"]
    coordinates: list[_Position] = Field(min_length=2)


class _Feature(_Ors):
    geometry: _Geometry
    properties: _Properties


class _Collection(_Ors):
    type: Literal["FeatureCollection"]
    features: list[_Feature] = Field(min_length=1)


@dataclass(frozen=True)
class DayRoute:
    # one (metres, seconds) pair per leg
    legs: tuple[tuple[float, float], ...]
    route_latlon: tuple[LatLon, ...]


def _stop_key(plan: DayPlan) -> StopKey:
    return tuple(
        (round(s.place.coords.lat, COORD_DECIMALS), round(s.place.coords.lon, COORD_DECIMALS))
        for s in plan.stops
    )


def downsample(points: list[LatLon], keep: list[int], cap: int = MAX_ROUTE_POINTS) -> list[LatLon]:
    if len(points) <= cap:
        return points
    # stops and both ends always survive so the line passes through every marker
    fixed = {0, len(points) - 1, *(i for i in keep if 0 <= i < len(points))}
    if len(fixed) > cap:
        raise ValueError("cap is smaller than the number of stops")
    room = cap - len(fixed)
    # centred picks among the other points, distinct so the result is exactly cap
    rest = [i for i in range(len(points)) if i not in fixed]
    even = {rest[((2 * k + 1) * len(rest)) // (2 * room)] for k in range(room)}
    return [points[i] for i in sorted(fixed | even)]


def parse_route(body: bytes, stop_key: StopKey) -> DayRoute:
    try:
        data = json.loads(body)
        fc = _Collection.model_validate(data)
    except (ValueError, RecursionError, ValidationError):
        # from None so values from the body are not kept in the chain
        raise RouteError("invalid_response") from None
    feature = fc.features[0]
    segments = feature.properties.segments
    way_points = feature.properties.way_points
    coords = feature.geometry.coordinates
    if len(segments) != len(stop_key) - 1 or len(way_points) != len(stop_key):
        raise RouteError("leg_count_mismatch")
    if way_points != sorted(way_points) or way_points[-1] >= len(coords):
        raise RouteError("invalid_response")
    for (a, b), seg in zip(zip(stop_key, stop_key[1:]), segments, strict=True):
        # a walk clearly shorter than the straight line means the points were misread
        if seg.distance + SNAP_TOLERANCE_M < _aerial_m(a, b):
            raise RouteError("implausible_distance")
    latlon: list[LatLon] = [
        (round(p[1], ROUTE_DECIMALS), round(p[0], ROUTE_DECIMALS)) for p in coords
    ]
    return DayRoute(
        legs=tuple((s.distance, s.duration) for s in segments),
        route_latlon=tuple(downsample(latlon, way_points)),
    )


def _aerial_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    phi1, phi2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(
        math.radians(b[1] - a[1]) / 2
    ) ** 2
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(h)))


def _minutes(seconds: float) -> int:
    return int(seconds / 60 + 0.5)


def apply_route(plan: DayPlan, route: DayRoute) -> DayPlan:
    stops = [plan.stops[0].model_copy(update={"leg_distance_km": None, "leg_walk_minutes": None})]
    total_km = 0.0
    total_min = 0
    for stop, (metres, seconds) in zip(plan.stops[1:], route.legs, strict=True):
        km = round(metres / 1000, 2)
        minutes = _minutes(seconds)
        total_km = round(total_km + km, 2)
        total_min += minutes
        stops.append(stop.model_copy(update={"leg_distance_km": km, "leg_walk_minutes": minutes}))
    # rebuilt through the constructor so the day validators run
    return DayPlan(
        day=plan.day,
        theme=plan.theme,
        stops=[DayStop.model_validate(s.model_dump()) for s in stops],
        total_distance_km=total_km,
        distance_source=DistanceSource.WALKING,
        total_walk_minutes=total_min,
        route_latlon=list(route.route_latlon),
    )


def _seconds_to_utc_midnight(wall: float) -> float:
    # assumption: the ors daily quota resets at 00:00 utc
    return DAY_S - (wall % DAY_S)


class WalkingRouter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: SecretStr | None,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        call_timeout_s: float = CALL_TIMEOUT_S,
        budget_s: float = BUDGET_S,
        per_minute: int = PER_MINUTE_LIMIT,
        cache_ttl_s: float = CACHE_TTL_S,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
        clock: Clock = time.monotonic,
        wall_clock: WallClock = time.time,
    ) -> None:
        if call_timeout_s <= 0 or budget_s <= 0 or cache_ttl_s <= 0:
            raise ValueError("timeouts and ttl must be positive")
        if per_minute < 1 or per_minute > PER_MINUTE_LIMIT:
            raise ValueError(f"per_minute must be 1..{PER_MINUTE_LIMIT}")
        key = api_key.get_secret_value() if api_key is not None else ""
        if key and (not key.isprintable() or any(ch.isspace() for ch in key)):
            raise ValueError("api_key must not contain spaces or line breaks")
        self._client = client
        self._key = api_key if key else None
        self._user_agent = check_user_agent(user_agent)
        self._url = f"{check_base_url(base_url)}/v2/directions/{PROFILE}/geojson"
        self._call_timeout_s = call_timeout_s
        self._budget_s = budget_s
        self._cache_ttl_s = cache_ttl_s
        self._http_timeout = httpx.Timeout(call_timeout_s, connect=min(CONNECT_TIMEOUT_S, call_timeout_s))
        self._clock = clock
        self._wall_clock = wall_clock
        self._window = SlidingWindow(per_minute, 60.0, clock)
        self._cooldown = Cooldown(clock)
        self._cache: TTLCache[StopKey, DayRoute] = TTLCache(max_cache_entries, clock)

    @property
    def enabled(self) -> bool:
        return self._key is not None

    async def attach(self, itinerary: Itinerary) -> Itinerary:
        deadline = self._clock() + self._budget_s
        plans: list[DayPlan] = []
        reasons: list[str] = []
        # a dead upstream should not cost a timeout per day
        give_up = False
        for plan in itinerary.day_plans:
            if len(plan.stops) < 2:
                plans.append(plan)
                continue
            if give_up:
                reasons.append("skipped")
                plans.append(plan)
                continue
            try:
                route = await self._route(plan, deadline)
                plans.append(apply_route(plan, route))
            except RouteError as exc:
                reasons.append(exc.reason)
                give_up = exc.reason in ("timeout", "network", "server_error")
                plans.append(plan)
            except Exception as exc:
                # routing must never fail the itinerary, type only, no values
                logger.error("walking route bug: %s", type(exc).__name__)
                reasons.append("internal")
                plans.append(plan)
        walking = sum(p.distance_source is DistanceSource.WALKING for p in plans)
        if reasons:
            logger.info("walking routes: %d walking, aerial fallback: %s", walking, ",".join(reasons))
        return itinerary.model_copy(update={"day_plans": plans, "disclaimer": disclaimer_for(plans)})

    async def _route(self, plan: DayPlan, deadline: float) -> DayRoute:
        key = _stop_key(plan)
        found, cached = self._cache.get(key)
        if found and cached is not None:
            return cached
        if self._key is None:
            raise RouteError("no_key")
        if self._cooldown.active:
            raise RouteError("cooldown")
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RouteError("budget")
        if not self._window.try_acquire():
            raise RouteError("rate_window")
        body = await self._post(key, min(self._call_timeout_s, remaining))
        route = parse_route(body, key)
        # only successes are cached, a failure is retried on the next request
        self._cache.put(key, route, self._cache_ttl_s)
        return route

    async def _post(self, key: StopKey, timeout_s: float) -> bytes:
        assert self._key is not None
        payload = {"coordinates": [[lon, lat] for lat, lon in key]}
        headers = {
            "Authorization": self._key.get_secret_value(),
            "User-Agent": self._user_agent,
            "Accept": "application/geo+json, application/json",
        }
        try:
            async with asyncio.timeout(timeout_s):
                async with self._client.stream(
                    "POST",
                    self._url,
                    json=payload,
                    headers=headers,
                    timeout=self._http_timeout,
                    follow_redirects=False,
                ) as response:
                    self._check_status(response)
                    body = await read_capped(response, MAX_BODY_BYTES)
                    if response.headers.get("x-ratelimit-remaining", "").strip() == "0":
                        # this answer is fine, the next call would be refused
                        self._cooldown.trip(_seconds_to_utc_midnight(self._wall_clock()))
                        logger.warning("ors daily quota used up, walking routes paused")
                    return body
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("ors timed out")
            raise RouteError("timeout") from None
        except BodyTooLargeError:
            raise RouteError("too_large") from None
        except httpx.HTTPError as exc:
            logger.warning("ors request failed: %s", type(exc).__name__)
            raise RouteError("network") from None

    def _check_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        if status == 429:
            self._cooldown.trip(MINUTE_COOLDOWN_S)
            logger.warning("ors minute limit hit, walking routes paused %.0f s", MINUTE_COOLDOWN_S)
            raise RouteError("http_429")
        if status in (401, 403):
            # quota used up or key refused, both last until the daily reset
            self._cooldown.trip(_seconds_to_utc_midnight(self._wall_clock()))
            logger.warning("ors refused the key or quota, http %s, check ORS_API_KEY", status)
            raise RouteError(f"http_{status}")
        if status >= 500:
            logger.warning("ors returned http %s", status)
            raise RouteError("server_error")
        # 4xx such as an unroutable point only affects this day
        raise RouteError(f"http_{status}")