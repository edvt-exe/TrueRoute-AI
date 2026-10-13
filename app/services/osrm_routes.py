import asyncio
import json
import logging
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas import LatLon
from app.services.walking_routes import (
    PROVIDER_OSRM,
    ROUTE_DECIMALS,
    SNAP_TOLERANCE_M,
    DayRoute,
    NonNeg,
    Position,
    RouteError,
    StopKey,
    aerial_m,
    downsample,
)
from app.utils.http_guard import BodyTooLargeError, check_base_url, check_user_agent, read_capped
from app.utils.throttle import Clock, Cooldown, Sleep, Spacer

logger = logging.getLogger(__name__)

# fossgis public osrm, worldwide foot profile, no key
DEFAULT_BASE_URL = "https://routing.openstreetmap.de"
# live probe: only routed-foot picks the profile, the name after v1 is ignored
ROUTE_PATH = "routed-foot/route/v1/foot"
# assumption: no wording is required by the fossgis page, credit the router and the data
OSRM_ATTRIBUTION = "Walking routes by OSRM on the FOSSGIS routing server, map data © OpenStreetMap contributors"
# fossgis policy: one request per second at most, 10 percent margin
MIN_INTERVAL_S = 1.1
CALL_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 5.0
# live probe: 3 paris stops were 4.4 KB, a 1772 km answer was 1.2 MB and is refused here
MAX_BODY_BYTES = 1_000_000
# live probe: a point in the atlantic came back ok, snapped 1757 km away to portugal
# assumption: 350 m like the ors default search radius, farther means the wrong place
MAX_SNAP_M = 350.0
RATE_COOLDOWN_S = 60.0
OUTAGE_COOLDOWN_S = 120.0
COORD_DECIMALS = 6


class _Osrm(BaseModel):
    # third-party payload, unknown fields such as hint are ignored on purpose
    model_config = ConfigDict(extra="ignore")


class _Leg(_Osrm):
    distance: NonNeg
    duration: NonNeg


class _Geometry(_Osrm):
    type: Literal["LineString"]
    coordinates: list[Position] = Field(min_length=2)


class _Route(_Osrm):
    legs: list[_Leg]
    geometry: _Geometry


class _Waypoint(_Osrm):
    location: Position
    distance: NonNeg


class _Response(_Osrm):
    code: Literal["Ok"]
    routes: list[_Route] = Field(min_length=1)
    waypoints: list[_Waypoint]


def _stop_indices(coords: list[Position], waypoints: list[_Waypoint]) -> list[int]:
    # osrm sends no way_points, the closest line point after the previous stop stands in
    keep: list[int] = []
    start = 0
    for wp in waypoints:
        lon, lat = wp.location[0], wp.location[1]
        best = min(range(start, len(coords)), key=lambda i: (coords[i][0] - lon) ** 2 + (coords[i][1] - lat) ** 2)
        keep.append(best)
        start = best
    return keep


def parse_osrm(body: bytes, stop_key: StopKey) -> DayRoute:
    try:
        data = _Response.model_validate(json.loads(body))
    except (ValueError, RecursionError, ValidationError):
        # from None so values from the body are not kept in the chain
        raise RouteError("osrm_invalid_response") from None
    route = data.routes[0]
    coords = route.geometry.coordinates
    if len(route.legs) != len(stop_key) - 1 or len(data.waypoints) != len(stop_key):
        raise RouteError("osrm_leg_count_mismatch")
    if any(wp.distance > MAX_SNAP_M for wp in data.waypoints):
        raise RouteError("osrm_snap_too_far")
    for (a, b), leg in zip(zip(stop_key, stop_key[1:]), route.legs, strict=True):
        # a walk clearly shorter than the straight line means the points were misread
        if leg.distance + SNAP_TOLERANCE_M < aerial_m(a, b):
            raise RouteError("osrm_implausible_distance")
    latlon: list[LatLon] = [(round(p[1], ROUTE_DECIMALS), round(p[0], ROUTE_DECIMALS)) for p in coords]
    return DayRoute(
        legs=tuple((leg.distance, leg.duration) for leg in route.legs),
        route_latlon=tuple(downsample(latlon, _stop_indices(coords, data.waypoints))),
        provider=PROVIDER_OSRM,
    )


class OsrmRouter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        call_timeout_s: float = CALL_TIMEOUT_S,
        min_interval_s: float = MIN_INTERVAL_S,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if call_timeout_s <= 0:
            raise ValueError("call_timeout_s must be positive")
        if min_interval_s < 1.0:
            raise ValueError("min_interval_s must be at least 1 second per fossgis policy")
        self._client = client
        # policy: a user agent that identifies the app
        self._user_agent = check_user_agent(user_agent)
        self._base = f"{check_base_url(base_url)}/{ROUTE_PATH}"
        self._call_timeout_s = call_timeout_s
        self._http_timeout = httpx.Timeout(call_timeout_s, connect=min(CONNECT_TIMEOUT_S, call_timeout_s))
        self._clock = clock
        self._spacer = Spacer(min_interval_s, clock, sleep)
        self._cooldown = Cooldown(clock)

    async def route(self, key: StopKey, deadline: float) -> DayRoute:
        if self._cooldown.active:
            raise RouteError("osrm_cooldown")
        if deadline - self._clock() <= 0:
            raise RouteError("osrm_budget")
        await self._spacer.wait()
        # the spacing wait may have used the rest of the budget
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RouteError("osrm_budget")
        timeout_s = min(self._call_timeout_s, remaining)
        try:
            body = await self._get(key, timeout_s)
        except RouteError as exc:
            if exc.reason == "osrm_timeout" and timeout_s < self._call_timeout_s:
                raise RouteError("osrm_budget_timeout") from None
            if exc.reason in ("osrm_timeout", "osrm_network", "osrm_server_error"):
                self._cooldown.trip(OUTAGE_COOLDOWN_S)
                logger.warning("osrm unhealthy, fallback routes paused %.0f s", OUTAGE_COOLDOWN_S)
            raise
        return parse_osrm(body, key)

    async def _get(self, key: StopKey, timeout_s: float) -> bytes:
        # only rounded numbers go into the path, never user text
        coords = ";".join(f"{lon:.{COORD_DECIMALS}f},{lat:.{COORD_DECIMALS}f}" for lat, lon in key)
        params = {"overview": "full", "geometries": "geojson", "steps": "false"}
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        try:
            async with asyncio.timeout(timeout_s):
                async with self._client.stream(
                    "GET",
                    f"{self._base}/{coords}",
                    params=params,
                    headers=headers,
                    timeout=self._http_timeout,
                    follow_redirects=False,
                ) as response:
                    status = response.status_code
                    if status == 429:
                        self._cooldown.trip(RATE_COOLDOWN_S)
                        logger.warning("osrm rate limited, fallback routes paused %.0f s", RATE_COOLDOWN_S)
                        raise RouteError("osrm_http_429")
                    if status >= 500:
                        logger.warning("osrm returned http %s", status)
                        raise RouteError("osrm_server_error")
                    if status != 200:
                        # 400 such as NoRoute only affects this day
                        raise RouteError(f"osrm_http_{status}")
                    return await read_capped(response, MAX_BODY_BYTES)
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("osrm timed out")
            raise RouteError("osrm_timeout") from None
        except BodyTooLargeError:
            raise RouteError("osrm_too_large") from None
        except httpx.HTTPError as exc:
            logger.warning("osrm request failed: %s", type(exc).__name__)
            raise RouteError("osrm_network") from None