import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.schemas import (
    DISCLAIMER,
    MAX_ROUTE_POINTS,
    MIXED_DISCLAIMER,
    WALKING_DISCLAIMER,
    Coordinates,
    DayPlan,
    DayStop,
    DistanceSource,
    Itinerary,
    Place,
    PlaceCategory,
)
from app.services import walking_routes
from app.services.walking_routes import (
    BUDGET_S,
    CACHE_TTL_S,
    MINUTE_COOLDOWN_S,
    ORS_ATTRIBUTION,
    PER_MINUTE_LIMIT,
    DayRoute,
    RouteError,
    WalkingRouter,
    downsample,
    parse_route,
)

MARKER = "SECRET-MARKER"
KEY = f"5b3ce3597851110001cf6248{MARKER}"
UA = "TrueRoute-AI-tests/0.1 (+https://example.org/trueroute)"
# 2026-10-13 21:00:00 utc, three hours before the daily reset
WALL = 1_791_925_200.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def place(n: int, day: int = 1) -> Place:
    # about 330 m apart going east in central bucharest, a new row per day
    return Place(
        ref=f"p{n}",
        osm_type="node",
        osm_id=n,
        name=f"Place {n}",
        category=PlaceCategory.ATTRACTION,
        coords=Coordinates(lat=44.43 + day * 0.01, lon=26.10 + n * 0.004),
    )


def day_plan(day: int, refs: list[int]) -> DayPlan:
    stops = [DayStop(order=i, place=place(n, day), leg_distance_km=None if i == 1 else 0.33) for i, n in enumerate(refs, start=1)]
    return DayPlan(day=day, theme=f"Day {day}", stops=stops, total_distance_km=round(0.33 * (len(refs) - 1), 2))


def itinerary(*days: list[int]) -> Itinerary:
    return Itinerary(
        city="Bucharest",
        days=len(days),
        pace="packed",
        budget="medium",
        day_plans=[day_plan(d, refs) for d, refs in enumerate(days, start=1)],
        unused_places=0,
    )


def aerial_m(a: list[float], b: list[float]) -> float:
    # a and b are lon, lat
    phi1, phi2 = math.radians(a[1]), math.radians(b[1])
    h = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


def ors_body(coords: list[list[float]], *, factor: float = 1.3, between: int = 3) -> dict[str, Any]:
    # shape follows the ors v2 geojson directions response, values are synthetic
    line: list[list[float]] = []
    way_points: list[int] = []
    segments = []
    for i, point in enumerate(coords):
        way_points.append(len(line))
        line.append([point[0], point[1], 80.0])
        if i + 1 < len(coords):
            nxt = coords[i + 1]
            for k in range(1, between + 1):
                t = k / (between + 1)
                line.append([point[0] + (nxt[0] - point[0]) * t, point[1] + (nxt[1] - point[1]) * t])
            dist = round(aerial_m(point, nxt) * factor, 1)
            segments.append({"distance": dist, "duration": round(dist / 1.39, 1), "steps": []})
    return {
        "type": "FeatureCollection",
        "bbox": [0, 0, 0, 0],
        "features": [
            {
                "bbox": [0, 0, 0, 0],
                "type": "Feature",
                "properties": {
                    "segments": segments,
                    "way_points": way_points,
                    "summary": {"distance": sum(s["distance"] for s in segments), "duration": 1.0},
                },
                "geometry": {"type": "LineString", "coordinates": line},
            }
        ],
        "metadata": {"service": "routing"},
    }


Responder = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def ok(**kwargs: Any) -> Responder:
    def respond(request: httpx.Request) -> httpx.Response:
        coords = json.loads(request.content)["coordinates"]
        return httpx.Response(200, json=ors_body(coords, **kwargs))

    return respond


def status(code: int, headers: dict[str, str] | None = None) -> Responder:
    return lambda request: httpx.Response(code, json={"error": {"code": 2010, "message": "x"}}, headers=headers)


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


Scenario = Callable[[WalkingRouter, FakeClock], Awaitable[Any]]


def run(responder: Responder, scenario: Scenario, key: str | None = KEY, **kwargs: Any) -> tuple[Any, Recorder, FakeClock]:
    clock = FakeClock()
    rec = Recorder(responder)

    async def main() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
            router = WalkingRouter(
                client,
                SecretStr(key) if key is not None else None,
                UA,
                clock=clock,
                wall_clock=lambda: WALL,
                **kwargs,
            )
            return await scenario(router, clock)

    return asyncio.run(main()), rec, clock


def attach(*days: list[int]) -> Scenario:
    async def scenario(router: WalkingRouter, clock: FakeClock) -> Itinerary:
        return await router.attach(itinerary(*days))

    return scenario


def sources(it: Itinerary) -> list[str]:
    return [p.distance_source.value for p in it.day_plans]


def reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    # fallback reasons from the summary line, so a test proves which check fired
    out: list[str] = []
    for r in caplog.records:
        msg = r.getMessage()
        if "aerial fallback: " in msg:
            out += msg.split("aerial fallback: ", 1)[1].split(",")
    return out


@pytest.fixture
def log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="app.services.walking_routes")
    return caplog


# request shape


def test_request_shape() -> None:
    _, rec, _ = run(ok(), attach([1, 2, 3]))
    assert rec.calls == 1
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"
    assert req.headers["authorization"] == KEY
    assert req.headers["user-agent"] == UA
    # geojson order, lon first
    coords = json.loads(req.content)["coordinates"]
    assert coords == [[26.104, 44.44], [26.108, 44.44], [26.112, 44.44]]
    assert json.loads(req.content) == {"coordinates": coords}


def test_key_only_in_header() -> None:
    _, rec, _ = run(ok(), attach([1, 2]))
    req = rec.requests[0]
    assert MARKER not in str(req.url)
    assert MARKER not in req.content.decode()
    assert [k for k, v in req.headers.items() if MARKER in v] == ["authorization"]


def test_custom_base_url() -> None:
    _, rec, _ = run(ok(), attach([1, 2]), base_url="https://ors.example.org/")
    assert str(rec.requests[0].url) == "https://ors.example.org/v2/directions/foot-walking/geojson"


# happy path


def test_walking_day_attached() -> None:
    it, _, _ = run(ok(), attach([1, 2, 3]))
    day = it.day_plans[0]
    assert day.distance_source is DistanceSource.WALKING
    walk = round(aerial_m([26.104, 44.44], [26.108, 44.44]) * 1.3, 1)
    assert day.stops[0].leg_distance_km is None and day.stops[0].leg_walk_minutes is None
    assert day.stops[1].leg_distance_km == round(walk / 1000, 2)
    assert day.stops[1].leg_walk_minutes == round(round(walk / 1.39, 1) / 60)
    assert day.total_distance_km == round(day.stops[1].leg_distance_km + day.stops[2].leg_distance_km, 2)
    assert day.total_walk_minutes == day.stops[1].leg_walk_minutes + day.stops[2].leg_walk_minutes
    assert it.disclaimer == WALKING_DISCLAIMER


def test_route_is_lat_lon() -> None:
    it, _, _ = run(ok(between=1), attach([1, 2]))
    route = it.day_plans[0].route_latlon
    assert route[0] == (44.44, 26.104)
    assert route[-1] == (44.44, 26.108)
    assert len(route) == 3


def test_minutes_rounding() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = ors_body(json.loads(request.content)["coordinates"])
        segs = body["features"][0]["properties"]["segments"]
        segs[0]["duration"], segs[1]["duration"] = 89.9, 90.0
        return httpx.Response(200, json=body)

    it, _, _ = run(respond, attach([1, 2, 3]))
    stops = it.day_plans[0].stops
    assert (stops[1].leg_walk_minutes, stops[2].leg_walk_minutes) == (1, 2)
    assert it.day_plans[0].total_walk_minutes == 3


def test_stops_and_places_unchanged() -> None:
    before = itinerary([1, 2, 3])

    async def scenario(router: WalkingRouter, clock: FakeClock) -> Itinerary:
        return await router.attach(before)

    it, _, _ = run(ok(), scenario)
    assert [s.place for s in it.day_plans[0].stops] == [s.place for s in before.day_plans[0].stops]
    assert [s.order for s in it.day_plans[0].stops] == [1, 2, 3]
    assert it.day_plans[0].theme == "Day 1"
    # the input itinerary is not mutated
    assert before.day_plans[0].distance_source is DistanceSource.AERIAL


def test_one_call_per_day() -> None:
    it, rec, _ = run(ok(), attach([1, 2], [3, 4, 5], [6, 7]))
    assert rec.calls == 3
    assert sources(it) == ["walking"] * 3


def test_single_stop_day_makes_no_call() -> None:
    it, rec, _ = run(ok(), attach([1], [2, 3]))
    assert rec.calls == 1
    assert sources(it) == ["aerial", "walking"]
    assert it.disclaimer == MIXED_DISCLAIMER


# fallbacks


def test_no_key_makes_no_call() -> None:
    it, rec, _ = run(ok(), attach([1, 2]), key=None)
    assert rec.calls == 0
    assert sources(it) == ["aerial"]
    assert it.disclaimer == DISCLAIMER


def test_blank_key_is_no_key() -> None:
    _, rec, _ = run(ok(), attach([1, 2]), key="")
    assert rec.calls == 0


def test_key_with_space_rejected() -> None:
    with pytest.raises(ValueError):
        run(ok(), attach([1, 2]), key="abc def")


@pytest.mark.parametrize("code", [400, 404, 413])
def test_client_error_affects_only_that_day(code: int, log: pytest.LogCaptureFixture) -> None:
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(code, json={"error": {"code": 2010, "message": "x"}})
        return ok()(request)  # type: ignore[return-value]

    it, rec, _ = run(respond, attach([1, 2], [3, 4]))
    assert sources(it) == ["aerial", "walking"]
    assert rec.calls == 2
    assert reasons(log) == [f"http_{code}"]
    # aerial day keeps its original aerial numbers
    assert it.day_plans[0] == day_plan(1, [1, 2])


@pytest.mark.parametrize("code", [500, 502, 503])
def test_server_error_stops_further_calls(code: int) -> None:
    it, rec, _ = run(status(code), attach([1, 2], [3, 4], [5, 6]))
    assert rec.calls == 1
    assert sources(it) == ["aerial"] * 3
    assert it.disclaimer == DISCLAIMER


def test_timeout_falls_back() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    it, rec, _ = run(respond, attach([1, 2], [3, 4]))
    assert sources(it) == ["aerial", "aerial"]
    assert rec.calls == 1


def test_real_timeout_is_bounded() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    it, rec, _ = run(respond, attach([1, 2]), call_timeout_s=0.05)
    assert sources(it) == ["aerial"]


def test_network_error_falls_back() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    it, rec, _ = run(respond, attach([1, 2], [3, 4]))
    assert sources(it) == ["aerial", "aerial"]
    assert rec.calls == 1


def test_redirect_not_followed() -> None:
    it, rec, _ = run(status(302, {"location": "https://evil.example.org/"}), attach([1, 2]))
    assert sources(it) == ["aerial"]
    assert rec.calls == 1


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b: b["features"][0]["properties"]["segments"].pop(), "leg_count_mismatch"),
        (lambda b: b["features"][0]["properties"]["way_points"].pop(), "leg_count_mismatch"),
        (lambda b: b["features"][0]["properties"]["segments"][0].update(distance=-1), "invalid_response"),
        (lambda b: b["features"][0]["properties"]["segments"][0].update(duration=-5), "invalid_response"),
        (lambda b: b["features"][0]["geometry"]["coordinates"][1].__setitem__(1, 95.0), "invalid_response"),
        (lambda b: b["features"][0]["geometry"]["coordinates"][1].__setitem__(0, 200.0), "invalid_response"),
        (lambda b: b["features"][0]["geometry"].update(type="Point"), "invalid_response"),
        (lambda b: b["features"].clear(), "invalid_response"),
        (lambda b: b.update(type="Feature"), "invalid_response"),
        (lambda b: b["features"][0]["properties"].pop("segments"), "invalid_response"),
        (lambda b: b["features"][0]["properties"].update(way_points=[0, 99, 2]), "invalid_response"),
        (lambda b: b["features"][0]["properties"].update(way_points=[0, 2, 999]), "invalid_response"),
    ],
)
def test_invalid_response_falls_back(
    mutate: Callable[[dict[str, Any]], Any], reason: str, log: pytest.LogCaptureFixture
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = ors_body(json.loads(request.content)["coordinates"], between=0)
        mutate(body)
        return httpx.Response(200, json=body)

    it, _, _ = run(respond, attach([1, 2, 3]))
    assert sources(it) == ["aerial"]
    assert reasons(log) == [reason]


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["distance", "duration"])
def test_non_finite_numbers_fall_back(token: str, field: str, log: pytest.LogCaptureFixture) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        text = json.dumps(ors_body(json.loads(request.content)["coordinates"]))
        text = text.replace(f'"{field}": ', f'"{field}": {token}, "x": ', 1)
        return httpx.Response(200, content=text.encode())

    it, _, _ = run(respond, attach([1, 2]))
    assert sources(it) == ["aerial"]
    assert reasons(log) == ["invalid_response"]


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"{}", b"[" * 100_000])
def test_malformed_body_falls_back(body: bytes, log: pytest.LogCaptureFixture) -> None:
    it, _, _ = run(lambda r: httpx.Response(200, content=body), attach([1, 2]))
    assert sources(it) == ["aerial"]
    assert reasons(log) == ["invalid_response"]


def test_too_large_body_falls_back(log: pytest.LogCaptureFixture) -> None:
    big = json.dumps(ors_body([[26.104, 44.44], [26.108, 44.44]])).encode()
    big = big[:-1] + b" " * walking_routes.MAX_BODY_BYTES + b"}"
    it, _, _ = run(lambda r: httpx.Response(200, content=big), attach([1, 2]))
    assert sources(it) == ["aerial"]
    assert reasons(log) == ["too_large"]


def test_declared_too_large_falls_back(log: pytest.LogCaptureFixture) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.dumps(ors_body(json.loads(request.content)["coordinates"])).encode()
        return httpx.Response(200, content=body, headers={"content-length": str(walking_routes.MAX_BODY_BYTES + 1)})

    it, _, _ = run(respond, attach([1, 2]))
    assert sources(it) == ["aerial"]
    assert reasons(log) == ["too_large"]


def test_implausibly_short_walk_falls_back(log: pytest.LogCaptureFixture) -> None:
    # stops about 3 km apart, a 600 m walk is impossible even with snapping
    it, _, _ = run(ok(factor=0.2), attach([1, 10]))
    assert sources(it) == ["aerial"]
    assert reasons(log) == ["implausible_distance"]


def test_call_timeout_capped_by_remaining_budget() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return ok()(request)  # type: ignore[return-value]

    it, rec, _ = run(respond, attach([1, 2]), budget_s=0.05)
    assert rec.calls == 1
    assert sources(it) == ["aerial"]


def test_slightly_short_walk_from_snapping_is_kept() -> None:
    it, _, _ = run(ok(factor=0.9), attach([1, 2]))
    assert sources(it) == ["walking"]


def test_unexpected_bug_never_fails_the_itinerary(monkeypatch: pytest.MonkeyPatch, log: pytest.LogCaptureFixture) -> None:
    def boom(body: bytes, key: Any) -> DayRoute:
        raise KeyError(MARKER)

    monkeypatch.setattr(walking_routes, "parse_route", boom)
    it, _, _ = run(ok(), attach([1, 2], [3, 4]))
    assert sources(it) == ["aerial", "aerial"]
    assert reasons(log) == ["internal", "internal"]


# cooldowns and limits


def test_429_pauses_for_a_minute() -> None:
    async def scenario(router: WalkingRouter, clock: FakeClock) -> list[list[str]]:
        out = [sources(await router.attach(itinerary([1, 2], [3, 4])))]
        clock.now += MINUTE_COOLDOWN_S - 1
        out.append(sources(await router.attach(itinerary([1, 2]))))
        clock.now += 1
        return out

    seen, rec, _ = run(status(429), scenario)
    # the 429 day and every later call inside the cooldown make no request
    assert rec.calls == 1
    assert seen == [["aerial", "aerial"], ["aerial"]]


def test_recovers_after_429_cooldown() -> None:
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return ok()(request)  # type: ignore[return-value]

    async def scenario(router: WalkingRouter, clock: FakeClock) -> list[str]:
        await router.attach(itinerary([1, 2]))
        clock.now += MINUTE_COOLDOWN_S
        return sources(await router.attach(itinerary([1, 2])))

    seen, rec, _ = run(respond, scenario)
    assert seen == ["walking"]
    assert rec.calls == 2


@pytest.mark.parametrize("code", [401, 403])
def test_403_pauses_until_utc_midnight(code: int) -> None:
    async def scenario(router: WalkingRouter, clock: FakeClock) -> list[int]:
        await router.attach(itinerary([1, 2]))
        clock.now += 3 * 3600 - 1
        await router.attach(itinerary([3, 4]))
        n = router_calls()
        clock.now += 1
        await router.attach(itinerary([5, 6]))
        return [n, router_calls()]

    rec_ref: list[Recorder] = []

    def router_calls() -> int:
        return rec_ref[0].calls

    clock = FakeClock()
    rec = Recorder(status(code))
    rec_ref.append(rec)

    async def main() -> list[int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
            router = WalkingRouter(client, SecretStr(KEY), UA, clock=clock, wall_clock=lambda: WALL)
            return await scenario(router, clock)

    assert asyncio.run(main()) == [1, 2]


def test_ratelimit_remaining_zero_pauses_after_success() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        coords = json.loads(request.content)["coordinates"]
        return httpx.Response(200, json=ors_body(coords), headers={"x-ratelimit-remaining": "0"})

    it, rec, _ = run(respond, attach([1, 2], [3, 4]))
    assert sources(it) == ["walking", "aerial"]
    assert rec.calls == 1


def test_ratelimit_remaining_nonzero_keeps_going() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        coords = json.loads(request.content)["coordinates"]
        return httpx.Response(200, json=ors_body(coords), headers={"x-ratelimit-remaining": "10"})

    it, rec, _ = run(respond, attach([1, 2], [3, 4]))
    assert sources(it) == ["walking", "walking"]


def test_per_minute_window() -> None:
    async def scenario(router: WalkingRouter, clock: FakeClock) -> list[str]:
        out: list[str] = []
        for n in range(3):
            out += sources(await router.attach(itinerary([10 + n, 20 + n])))
        clock.now += 60
        out += sources(await router.attach(itinerary([1, 2])))
        return out

    seen, rec, _ = run(ok(), scenario, per_minute=2)
    assert seen == ["walking", "walking", "aerial", "walking"]
    assert rec.calls == 3


@pytest.mark.parametrize("per_minute", [0, PER_MINUTE_LIMIT + 1])
def test_per_minute_cannot_exceed_ors_limit(per_minute: int) -> None:
    with pytest.raises(ValueError):
        run(ok(), attach([1, 2]), per_minute=per_minute)


def test_budget_cuts_later_days() -> None:
    clock_ref: list[FakeClock] = []

    def respond(request: httpx.Request) -> httpx.Response:
        clock_ref[0].now += 15
        return ok()(request)  # type: ignore[return-value]

    async def scenario(router: WalkingRouter, clock: FakeClock) -> list[str]:
        clock_ref.append(clock)
        return sources(await router.attach(itinerary([1, 2], [3, 4], [5, 6])))

    seen, rec, _ = run(respond, scenario)
    assert BUDGET_S == 20.0
    # day 2 starts with 5 s left, day 3 has nothing left
    assert seen == ["walking", "walking", "aerial"]
    assert rec.calls == 2


# cache


def test_cache_hit_makes_no_call() -> None:
    async def scenario(router: WalkingRouter, clock: FakeClock) -> list[Itinerary]:
        return [await router.attach(itinerary([1, 2, 3])) for _ in range(3)]

    results, rec, _ = run(ok(), scenario)
    assert rec.calls == 1
    assert results[0] == results[2]


def test_cache_expires() -> None:
    async def scenario(router: WalkingRouter, clock: FakeClock) -> None:
        await router.attach(itinerary([1, 2]))
        clock.now += CACHE_TTL_S - 1
        await router.attach(itinerary([1, 2]))
        clock.now += 1
        await router.attach(itinerary([1, 2]))

    _, rec, _ = run(ok(), scenario)
    assert rec.calls == 2


def test_cache_is_order_sensitive() -> None:
    async def scenario(router: WalkingRouter, clock: FakeClock) -> None:
        await router.attach(itinerary([1, 2]))
        await router.attach(itinerary([2, 1]))

    _, rec, _ = run(ok(), scenario)
    assert rec.calls == 2


def test_failures_are_not_cached() -> None:
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(404, json={"error": {"code": 2010}})
        return ok()(request)  # type: ignore[return-value]

    async def scenario(router: WalkingRouter, clock: FakeClock) -> list[str]:
        await router.attach(itinerary([1, 2]))
        return sources(await router.attach(itinerary([1, 2])))

    seen, rec, _ = run(respond, scenario)
    assert seen == ["walking"]
    assert rec.calls == 2


def test_cache_serves_during_cooldown() -> None:
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return ok()(request)  # type: ignore[return-value]
        return httpx.Response(429)

    async def scenario(router: WalkingRouter, clock: FakeClock) -> list[str]:
        await router.attach(itinerary([1, 2]))
        await router.attach(itinerary([3, 4]))
        return sources(await router.attach(itinerary([1, 2])))

    seen, rec, _ = run(respond, scenario)
    assert seen == ["walking"]
    assert rec.calls == 2


# geometry


def test_downsample_keeps_cap_ends_and_stops() -> None:
    points = [(0.0, i / 10_000) for i in range(5000)]
    keep = [0, 1234, 2500, 3777, 4999]
    out = downsample(points, keep)
    assert len(out) == MAX_ROUTE_POINTS
    assert out[0] == points[0] and out[-1] == points[-1]
    for i in keep:
        assert points[i] in out
    # order is kept
    assert out == sorted(out, key=lambda p: p[1])


def test_downsample_short_route_untouched() -> None:
    points = [(0.0, float(i)) for i in range(MAX_ROUTE_POINTS)]
    assert downsample(points, [0, MAX_ROUTE_POINTS - 1]) == points


def test_downsample_spreads_evenly() -> None:
    points = [(0.0, float(i)) for i in range(1000)]
    out = downsample(points, [0, 999], cap=11)
    xs = [p[1] for p in out]
    assert xs == [0.0, 56.0, 167.0, 278.0, 389.0, 500.0, 610.0, 721.0, 832.0, 943.0, 999.0]
    gaps = [b - a for a, b in zip(xs[1:-2], xs[2:-1])]
    assert max(gaps) - min(gaps) <= 1


def test_downsample_cap_smaller_than_stops() -> None:
    with pytest.raises(ValueError):
        downsample([(0.0, float(i)) for i in range(10)], [0, 3, 5, 9], cap=3)


def test_long_route_is_capped_in_response() -> None:
    it, _, _ = run(ok(between=300), attach([1, 2, 3, 4, 5]))
    day = it.day_plans[0]
    assert day.distance_source is DistanceSource.WALKING
    assert len(day.route_latlon) == MAX_ROUTE_POINTS
    for stop in day.stops:
        assert (round(stop.place.coords.lat, 5), round(stop.place.coords.lon, 5)) in day.route_latlon


def test_parse_route_direct() -> None:
    key = ((44.44, 26.104), (44.44, 26.108))
    body = json.dumps(ors_body([[26.104, 44.44], [26.108, 44.44]], between=0)).encode()
    route = parse_route(body, key)
    assert len(route.legs) == 1
    assert route.route_latlon == ((44.44, 26.104), (44.44, 26.108))
    with pytest.raises(RouteError) as info:
        parse_route(body, key + ((44.44, 26.112),))
    assert info.value.reason == "leg_count_mismatch"


def test_parse_route_segment_count_alone() -> None:
    key = ((44.44, 26.104), (44.44, 26.108), (44.44, 26.112))
    data = ors_body([[lon, lat] for lat, lon in key], between=0)
    data["features"][0]["properties"]["segments"].append({"distance": 300.0, "duration": 200.0})
    with pytest.raises(RouteError) as info:
        parse_route(json.dumps(data).encode(), key)
    assert info.value.reason == "leg_count_mismatch"


# logging


def test_no_secret_or_body_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=f"bad key {MARKER}")

    run(respond, attach([1, 2]))
    run(ok(), attach([1, 2]), key=KEY)
    run(lambda r: httpx.Response(200, content=f'{{"x": "{MARKER}"'.encode()), attach([1, 2]))
    assert caplog.records
    for record in caplog.records:
        assert MARKER not in record.getMessage()
        assert "Place 1" not in record.getMessage()


def test_fallback_reasons_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.services.walking_routes")
    run(status(404), attach([1, 2]))
    assert any("aerial fallback: http_404" in r.getMessage() for r in caplog.records)


def test_attribution_text() -> None:
    # ors terms of service wording
    assert ORS_ATTRIBUTION == "© openrouteservice.org by HeiGIT | Map data © OpenStreetMap contributors"


def test_enabled_flag() -> None:
    async def scenario(router: WalkingRouter, clock: FakeClock) -> bool:
        return router.enabled

    assert run(ok(), scenario)[0] is True
    assert run(ok(), scenario, key=None)[0] is False