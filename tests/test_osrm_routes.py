import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.schemas import MAX_ROUTE_POINTS
from app.services import osrm_routes
from app.services.osrm_routes import (
    MAX_SNAP_M,
    MIN_INTERVAL_S,
    OUTAGE_COOLDOWN_S,
    RATE_COOLDOWN_S,
    OsrmRouter,
    parse_osrm,
)
from app.services.walking_routes import PROVIDER_OSRM, RouteError

UA = "TrueRoute-AI-tests/0.1 (+https://example.org/trueroute)"
# notre-dame, sainte-chapelle, louvre pyramid as lat, lon like the router keys
KEY = ((48.853, 2.3499), (48.8554, 2.345), (48.8611, 2.3359))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


def aerial_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    phi1, phi2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


def osrm_body(key: tuple[tuple[float, float], ...] = KEY, *, between: int = 3, factor: float = 1.3, snap: float = 30.0) -> dict[str, Any]:
    # shape follows the live fossgis probe, values are synthetic
    line: list[list[float]] = []
    legs = []
    for i, (lat, lon) in enumerate(key):
        line.append([lon, lat])
        if i + 1 < len(key):
            nlat, nlon = key[i + 1]
            for k in range(1, between + 1):
                t = k / (between + 1)
                line.append([lon + (nlon - lon) * t, lat + (nlat - lat) * t])
            dist = round(aerial_m((lat, lon), (nlat, nlon)) * factor, 1)
            legs.append({"steps": [], "weight": 1.0, "summary": "", "duration": round(dist / 1.25, 1), "distance": dist})
    return {
        "code": "Ok",
        "routes": [
            {
                "legs": legs,
                "weight_name": "routability",
                "geometry": {"coordinates": line, "type": "LineString"},
                "weight": 150.12,
                "duration": sum(leg["duration"] for leg in legs),
                "distance": sum(leg["distance"] for leg in legs),
            }
        ],
        "waypoints": [{"hint": "Ru_Sn", "location": [lon, lat], "name": "", "distance": snap} for lat, lon in key],
    }


Responder = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def ok(**kwargs: Any) -> Responder:
    return lambda request: httpx.Response(200, json=osrm_body(**kwargs))


class Recorder:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responder(request)
        if isinstance(result, httpx.Response):
            return result
        return await result

    @property
    def calls(self) -> int:
        return len(self.requests)


Scenario = Callable[[OsrmRouter, FakeClock], Awaitable[Any]]


def run(responder: Responder, scenario: Scenario, **kwargs: Any) -> tuple[Any, Recorder, FakeClock]:
    clock = FakeClock()
    rec = Recorder(responder)

    async def main() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
            router = OsrmRouter(client, UA, clock=clock, sleep=clock.sleep, **kwargs)
            return await scenario(router, clock)

    return asyncio.run(main()), rec, clock


def route_once(key: tuple[tuple[float, float], ...] = KEY, budget: float = 20.0) -> Scenario:
    async def scenario(router: OsrmRouter, clock: FakeClock) -> Any:
        try:
            return await router.route(key, clock() + budget)
        except RouteError as exc:
            return exc.reason

    return scenario


# request shape


def test_request_shape() -> None:
    _, rec, _ = run(ok(), route_once())
    req = rec.requests[0]
    assert req.method == "GET"
    assert req.url.host == "routing.openstreetmap.de"
    # live probe: routed-foot picks the profile, coordinates are lon,lat joined by ;
    assert req.url.path == "/routed-foot/route/v1/foot/2.349900,48.853000;2.345000,48.855400;2.335900,48.861100"
    assert dict(req.url.params) == {"overview": "full", "geometries": "geojson", "steps": "false"}
    assert req.headers["user-agent"] == UA
    assert "authorization" not in req.headers


def test_custom_base_url() -> None:
    _, rec, _ = run(ok(), route_once(), base_url="https://osrm.example.org/")
    assert str(rec.requests[0].url).startswith("https://osrm.example.org/routed-foot/route/v1/foot/")


@pytest.mark.parametrize("agent", ["python-httpx/0.28", "", "Mozilla/5.0"])
def test_user_agent_must_identify_the_app(agent: str) -> None:
    with pytest.raises(ValueError):
        OsrmRouter(httpx.AsyncClient(), agent)


def test_spacing_cannot_go_below_the_policy() -> None:
    assert MIN_INTERVAL_S >= 1.0
    with pytest.raises(ValueError):
        OsrmRouter(httpx.AsyncClient(), UA, min_interval_s=0.5)


# parsing


def test_route_parsed() -> None:
    route, _, _ = run(ok(), route_once())
    assert route.provider == PROVIDER_OSRM
    assert len(route.legs) == 2
    walk = round(aerial_m(KEY[0], KEY[1]) * 1.3, 1)
    assert route.legs[0] == (walk, round(walk / 1.25, 1))
    # lat, lon for leaflet, osrm sends lon, lat
    assert route.route_latlon[0] == (48.853, 2.3499)
    assert route.route_latlon[-1] == (48.8611, 2.3359)


def test_live_probe_numbers_parse() -> None:
    # the leg values the fossgis server returned for these three stops
    body = osrm_body()
    body["routes"][0]["legs"][0].update(distance=469.2, duration=375.6)
    body["routes"][0]["legs"][1].update(distance=1331.7, duration=1065.5)
    route = parse_osrm(json.dumps(body).encode(), KEY)
    assert route.legs == ((469.2, 375.6), (1331.7, 1065.5))


def test_long_route_keeps_every_stop() -> None:
    route, _, _ = run(ok(between=600), route_once())
    assert len(route.route_latlon) == MAX_ROUTE_POINTS
    for lat, lon in KEY:
        assert (lat, lon) in route.route_latlon


def test_atlantic_answer_is_refused() -> None:
    # live probe: a sea point came back ok, snapped 1757 km to portugal
    body = osrm_body()
    body["waypoints"][1]["distance"] = 1757537.1
    with pytest.raises(RouteError) as info:
        parse_osrm(json.dumps(body).encode(), KEY)
    assert info.value.reason == "osrm_snap_too_far"


@pytest.mark.parametrize(("snap", "reason"), [(MAX_SNAP_M, None), (MAX_SNAP_M + 0.1, "osrm_snap_too_far")])
def test_snap_limit_edge(snap: float, reason: str | None) -> None:
    body = json.dumps(osrm_body(snap=snap)).encode()
    if reason is None:
        assert parse_osrm(body, KEY).provider == PROVIDER_OSRM
    else:
        with pytest.raises(RouteError) as info:
            parse_osrm(body, KEY)
        assert info.value.reason == reason


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b: b["routes"][0]["legs"].pop(), "osrm_leg_count_mismatch"),
        (lambda b: b["waypoints"].pop(), "osrm_leg_count_mismatch"),
        (lambda b: b.update(code="NoRoute"), "osrm_invalid_response"),
        (lambda b: b.update(routes=[]), "osrm_invalid_response"),
        (lambda b: b["routes"][0]["legs"][0].update(distance=-1), "osrm_invalid_response"),
        (lambda b: b["routes"][0]["legs"][0].update(duration=-1), "osrm_invalid_response"),
        (lambda b: b["routes"][0]["geometry"].update(type="Point"), "osrm_invalid_response"),
        (lambda b: b["routes"][0]["geometry"]["coordinates"][1].__setitem__(1, 95.0), "osrm_invalid_response"),
        (lambda b: b["waypoints"][0].update(distance=-3), "osrm_invalid_response"),
        (lambda b: b["waypoints"][0].pop("location"), "osrm_invalid_response"),
    ],
)
def test_invalid_answers(mutate: Callable[[dict[str, Any]], Any], reason: str) -> None:
    body = osrm_body()
    mutate(body)
    assert run(lambda r: httpx.Response(200, json=body), route_once())[0] == reason


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"{}", b"[" * 100_000])
def test_malformed_body(body: bytes) -> None:
    assert run(lambda r: httpx.Response(200, content=body), route_once())[0] == "osrm_invalid_response"


def test_nan_is_refused() -> None:
    text = json.dumps(osrm_body()).replace('"distance": ', '"distance": NaN, "x": ', 1)
    assert run(lambda r: httpx.Response(200, content=text.encode()), route_once())[0] == "osrm_invalid_response"


def test_implausibly_short_walk() -> None:
    far = ((48.853, 2.3499), (48.88, 2.3499))
    assert run(ok(key=far, factor=0.2), route_once(far))[0] == "osrm_implausible_distance"


def test_too_large_body() -> None:
    big = json.dumps(osrm_body()).encode()[:-1] + b" " * osrm_routes.MAX_BODY_BYTES + b"}"
    assert run(lambda r: httpx.Response(200, content=big), route_once())[0] == "osrm_too_large"


# status codes and pauses


def status(code: int) -> Responder:
    return lambda r: httpx.Response(code, json={"code": "NoRoute", "message": "x"})


@pytest.mark.parametrize("code", [400, 404])
def test_client_error_only_affects_this_day(code: int) -> None:
    async def scenario(router: OsrmRouter, clock: FakeClock) -> list[Any]:
        first = await route_once()(router, clock)
        second = await route_once()(router, clock)
        return [first, second]

    seen, rec, _ = run(status(code), scenario)
    assert seen == [f"osrm_http_{code}"] * 2
    assert rec.calls == 2


def test_redirect_not_followed() -> None:
    reason, rec, _ = run(lambda r: httpx.Response(302, headers={"location": "https://evil.example.org/"}), route_once())
    assert reason == "osrm_http_302" and rec.calls == 1


def test_429_pauses_for_a_minute() -> None:
    async def scenario(router: OsrmRouter, clock: FakeClock) -> list[Any]:
        out = [await route_once()(router, clock)]
        clock.now += RATE_COOLDOWN_S - 2
        out.append(await route_once()(router, clock))
        clock.now += 2
        out.append(await route_once()(router, clock))
        return out

    seen, rec, _ = run(status(429), scenario)
    assert seen == ["osrm_http_429", "osrm_cooldown", "osrm_http_429"]
    assert rec.calls == 2


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


def connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down", request=request)


@pytest.mark.parametrize(
    ("responder", "reason"),
    [(timeout, "osrm_timeout"), (connect_error, "osrm_network"), (status(503), "osrm_server_error"), (status(500), "osrm_server_error")],
)
def test_outage_pauses_two_minutes(responder: Responder, reason: str) -> None:
    async def scenario(router: OsrmRouter, clock: FakeClock) -> list[Any]:
        out = [await route_once()(router, clock)]
        clock.now += OUTAGE_COOLDOWN_S - 1
        out.append(await route_once()(router, clock))
        clock.now += 1
        out.append(await route_once()(router, clock))
        return out

    seen, rec, _ = run(responder, scenario)
    assert seen == [reason, "osrm_cooldown", reason]
    assert rec.calls == 2


def test_real_timeout_is_bounded() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    reason, _, _ = run(slow, route_once(), call_timeout_s=0.05)
    assert reason == "osrm_timeout"


# spacing and budget


def test_requests_are_spaced_by_the_policy() -> None:
    async def scenario(router: OsrmRouter, clock: FakeClock) -> None:
        await route_once()(router, clock)
        await route_once()(router, clock)

    _, rec, clock = run(ok(), scenario)
    assert rec.calls == 2
    assert clock.sleeps == [pytest.approx(MIN_INTERVAL_S)]


def test_no_time_left_makes_no_call() -> None:
    reason, rec, _ = run(ok(), route_once(budget=0))
    assert reason == "osrm_budget" and rec.calls == 0


def test_spacing_wait_can_use_up_the_budget() -> None:
    async def scenario(router: OsrmRouter, clock: FakeClock) -> list[Any]:
        first = await route_once()(router, clock)
        # 0.5 s left, the next call must wait 1.1 s first
        second = await route_once(budget=0.5)(router, clock)
        return [first, second]

    seen, rec, _ = run(ok(), scenario)
    assert seen[1] == "osrm_budget"
    assert rec.calls == 1


def test_budget_cut_call_does_not_pause() -> None:
    calls = {"n": 0}

    async def respond(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(1.0)
        return httpx.Response(200, json=osrm_body())

    async def scenario(router: OsrmRouter, clock: FakeClock) -> list[Any]:
        # 0.05 s of budget cuts the slow call short, osrm itself was fine
        first = await route_once(budget=0.05)(router, clock)
        clock.now += 2
        second = await route_once()(router, clock)
        return [first, second]

    seen, rec, _ = run(respond, scenario)
    assert seen[0] == "osrm_budget_timeout"
    assert seen[1].provider == PROVIDER_OSRM
    assert rec.calls == 2


def test_outage_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.osrm_routes")
    run(timeout, route_once())
    assert "osrm unhealthy, fallback routes paused 120 s" in [r.getMessage() for r in caplog.records]


def test_spacing_is_pinned_to_the_policy_margin() -> None:
    # fossgis: one request per second, plus 10 percent
    assert MIN_INTERVAL_S == 1.1

    async def scenario(router: OsrmRouter, clock: FakeClock) -> None:
        await route_once()(router, clock)
        await route_once()(router, clock)

    _, _, clock = run(ok(), scenario)
    assert clock.sleeps == [pytest.approx(1.1)]


def test_no_budget_means_no_spacing_wait() -> None:
    async def scenario(router: OsrmRouter, clock: FakeClock) -> Any:
        await route_once()(router, clock)
        return await route_once(budget=0)(router, clock)

    reason, rec, clock = run(ok(), scenario)
    # a day with no time left gives up at once, it does not sleep first
    assert reason == "osrm_budget"
    assert clock.sleeps == [] and rec.calls == 1