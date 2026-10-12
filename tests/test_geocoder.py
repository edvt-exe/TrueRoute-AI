import asyncio
import inspect
import json
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.services.geocoder import (
    FOUND_TTL_S,
    MAX_BODY_BYTES,
    MAX_DISPLAY_NAME,
    NOT_FOUND_TTL_S,
    REFUSAL_COOLDOWN_S,
    CityNotFoundError,
    Geocoder,
    GeocoderError,
    GeocoderUnavailableError,
)

UA = "TrueRoute-AI-tests/0.1 (+https://example.org/trueroute)"
MARKER = "SECRET-MARKER"

# shape follows the jsonv2 example in the nominatim docs, values are illustrative
HIT: dict[str, Any] = {
    "place_id": 1,
    "licence": "Data (c) OpenStreetMap contributors, ODbL 1.0",
    "osm_type": "relation",
    "osm_id": 1,
    "lat": "44.4361414",
    "lon": "26.1027202",
    "category": "boundary",
    "type": "administrative",
    "place_rank": 12,
    "importance": 0.8,
    "addresstype": "city",
    "name": "Bucharest",
    "display_name": "Bucharest, Romania",
    "boundingbox": ["44.3342", "44.5414", "25.9667", "26.2244"],
}

Responder = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


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

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    def __init__(self, responder: Responder, clock: FakeClock) -> None:
        self.responder = responder
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.times.append(self.clock())
        # yield a few times so concurrent callers really overlap
        for _ in range(3):
            await asyncio.sleep(0)
        result = self.responder(request)
        if inspect.isawaitable(result):
            result = await result
        return result

    @property
    def calls(self) -> int:
        return len(self.requests)


Scenario = Callable[[Geocoder, FakeClock], Awaitable[Any]]


def run(responder: Responder, scenario: Scenario, **kwargs: Any) -> tuple[Any, Recorder, FakeClock]:
    clock = FakeClock()
    rec = Recorder(responder, clock)

    async def main() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
            geo = Geocoder(client, UA, clock=clock, sleep=clock.sleep, **kwargs)
            return await scenario(geo, clock)

    return asyncio.run(main()), rec, clock


def ok(data: Any = None) -> Responder:
    payload = [HIT] if data is None else data
    return lambda request: httpx.Response(200, json=payload)


def status(code: int, body: str = "") -> Responder:
    return lambda request: httpx.Response(code, text=body)


def raw(body: bytes) -> Responder:
    return lambda request: httpx.Response(200, content=body)


def geocode(city: str = "Bucharest") -> Scenario:
    async def scenario(geo: Geocoder, clock: FakeClock) -> Any:
        return await geo.geocode(city)

    return scenario


def expect_error(scenario: Scenario) -> Scenario:
    async def wrapped(geo: Geocoder, clock: FakeClock) -> Any:
        try:
            await scenario(geo, clock)
        except GeocoderError as exc:
            return exc
        raise AssertionError("expected a GeocoderError")

    return wrapped


# happy path


def test_happy_path_result() -> None:
    res, rec, _ = run(ok(), geocode())
    assert rec.calls == 1
    assert res.query == "Bucharest"
    assert res.display_name == "Bucharest, Romania"
    assert (res.center.lat, res.center.lon) == (44.4361414, 26.1027202)
    assert res.bbox is not None
    assert (res.bbox.south, res.bbox.north, res.bbox.west, res.bbox.east) == (
        44.3342,
        44.5414,
        25.9667,
        26.2244,
    )


def test_happy_path_request_shape() -> None:
    _, rec, _ = run(ok(), geocode("  Cluj-Napoca "))
    req = rec.requests[0]
    assert req.method == "GET"
    assert req.url.scheme == "https"
    assert req.url.host == "nominatim.openstreetmap.org"
    assert req.url.path == "/search"
    assert dict(req.url.params) == {
        "q": "Cluj-Napoca",
        "format": "jsonv2",
        "limit": "1",
        "featureType": "settlement",
        "accept-language": "en",
    }
    assert req.headers["user-agent"] == UA
    assert req.headers["accept"] == "application/json"


def test_custom_base_url_is_used() -> None:
    _, rec, _ = run(ok(), geocode(), base_url="https://geo.example.org/nominatim/")
    assert str(rec.requests[0].url).startswith("https://geo.example.org/nominatim/search?")


def test_bbox_optional() -> None:
    hit = {k: v for k, v in HIT.items() if k != "boundingbox"}
    res, _, _ = run(ok([hit]), geocode())
    assert res.bbox is None


def test_only_first_hit_is_used() -> None:
    second = HIT | {"lat": "1.0", "lon": "1.0", "display_name": "Other"}
    res, _, _ = run(ok([HIT, second]), geocode())
    assert res.display_name == "Bucharest, Romania"


def test_display_name_truncated() -> None:
    hit = HIT | {"display_name": "a" * 1000}
    res, _, _ = run(ok([hit]), geocode())
    assert res.display_name == "a" * MAX_DISPLAY_NAME


def test_display_name_control_chars_cleaned() -> None:
    hit = HIT | {"display_name": "Bucharest,\n\tRomania\x00"}
    res, _, _ = run(ok([hit]), geocode())
    assert res.display_name == "Bucharest, Romania"


# not found and invalid input


def test_empty_result_is_not_found() -> None:
    exc, rec, _ = run(ok([]), expect_error(geocode("Nowhereville")))
    assert isinstance(exc, CityNotFoundError)
    assert rec.calls == 1


@pytest.mark.parametrize("city", ["", "   ", "a\nb", "x" * 101])
def test_invalid_city_rejected_without_network(city: str) -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> None:
        with pytest.raises(ValueError, match="invalid city"):
            await geo.geocode(city)

    _, rec, _ = run(ok(), scenario)
    assert rec.calls == 0


# http errors, no retries


@pytest.mark.parametrize("code", [403, 429])
def test_refusal_fails_fast_and_cools_down(code: int) -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> list[GeocoderError]:
        errors = []
        for _ in range(2):
            with pytest.raises(GeocoderUnavailableError) as info:
                await geo.geocode("Bucharest")
            errors.append(info.value)
        clock.advance(REFUSAL_COOLDOWN_S - 1)
        with pytest.raises(GeocoderUnavailableError, match="cooling down"):
            await geo.geocode("Rome")
        return errors

    errors, rec, _ = run(status(code, MARKER), scenario)
    assert rec.calls == 1
    assert "refused" in errors[0].message
    assert "cooling down" in errors[1].message
    assert all(MARKER not in str(e) for e in errors)


def test_cooldown_ends() -> None:
    responses = iter([httpx.Response(429), httpx.Response(200, json=[HIT])])

    async def scenario(geo: Geocoder, clock: FakeClock) -> Any:
        with pytest.raises(GeocoderUnavailableError):
            await geo.geocode("Bucharest")
        clock.advance(REFUSAL_COOLDOWN_S)
        return await geo.geocode("Bucharest")

    res, rec, _ = run(lambda request: next(responses), scenario)
    assert rec.calls == 2
    assert res.display_name == "Bucharest, Romania"


@pytest.mark.parametrize("code", [500, 502, 503, 404, 302])
def test_other_status_fails_fast(code: int) -> None:
    exc, rec, _ = run(status(code, MARKER), expect_error(geocode()))
    assert isinstance(exc, GeocoderUnavailableError)
    assert exc.message == f"geocoder returned http {code}"
    assert MARKER not in str(exc)
    assert rec.calls == 1


def test_redirect_not_followed() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.org/"})

    exc, rec, _ = run(responder, expect_error(geocode()))
    assert isinstance(exc, GeocoderUnavailableError)
    assert rec.calls == 1


def test_server_error_not_cached_and_no_cooldown() -> None:
    responses = iter([httpx.Response(500), httpx.Response(200, json=[HIT])])

    async def scenario(geo: Geocoder, clock: FakeClock) -> Any:
        with pytest.raises(GeocoderUnavailableError):
            await geo.geocode("Bucharest")
        return await geo.geocode("Bucharest")

    res, rec, _ = run(lambda request: next(responses), scenario)
    assert rec.calls == 2
    assert res.query == "Bucharest"


def test_read_timeout() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    exc, rec, _ = run(responder, expect_error(geocode()))
    assert isinstance(exc, GeocoderUnavailableError)
    assert exc.message == "geocoder timed out"
    assert rec.calls == 1


def test_connect_error() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    exc, rec, _ = run(responder, expect_error(geocode()))
    assert isinstance(exc, GeocoderUnavailableError)
    assert exc.message == "geocoder request failed"
    assert rec.calls == 1


def test_total_timeout_bounds_slow_server() -> None:
    async def responder(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=[HIT])

    async def scenario(geo: Geocoder, clock: FakeClock) -> Any:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(GeocoderUnavailableError, match="timed out"):
            await geo.geocode("Bucharest")
        return loop.time() - start

    elapsed, rec, _ = run(responder, scenario, total_timeout_s=0.05)
    assert elapsed < 1.0
    assert rec.calls == 1


# bad bodies


BAD_BODIES = [
    pytest.param(b"<html>" + MARKER.encode() + b"</html>", id="html"),
    pytest.param(b"", id="empty"),
    pytest.param(b"\xff\xfe", id="not_utf8"),
    pytest.param(b"[" * 50_000 + b"]" * 50_000, id="deep_nesting"),
    pytest.param(b'{"error": "' + MARKER.encode() + b'"}', id="object"),
    pytest.param(b"null", id="null"),
    pytest.param(b'["' + MARKER.encode() + b'"]', id="list_of_string"),
    pytest.param(b'[{"lon": "1", "display_name": "x"}]', id="missing_lat"),
    pytest.param(b'[{"lat": "' + MARKER.encode() + b'", "lon": "1", "display_name": "x"}]', id="lat_text"),
    pytest.param(b'[{"lat": "91", "lon": "1", "display_name": "x"}]', id="lat_range"),
    pytest.param(b'[{"lat": "1", "lon": "181", "display_name": "x"}]', id="lon_range"),
    pytest.param(b'[{"lat": "nan", "lon": "1", "display_name": "x"}]', id="lat_nan"),
    pytest.param(b'[{"lat": "1", "lon": "1", "display_name": " \\n "}]', id="blank_name"),
    pytest.param(b'[{"lat": "1", "lon": "1", "display_name": "x", "boundingbox": ["1", "2"]}]', id="short_bbox"),
    pytest.param(
        b'[{"lat": "1", "lon": "1", "display_name": "x", "boundingbox": ["5", "1", "0", "1"]}]',
        id="bbox_south_above_north",
    ),
]


@pytest.mark.parametrize("body", BAD_BODIES)
def test_bad_body_is_unavailable(body: bytes, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    exc, rec, _ = run(raw(body), expect_error(geocode()))
    assert isinstance(exc, GeocoderUnavailableError)
    assert rec.calls == 1
    assert MARKER not in str(exc)
    # from None: no chained error that could carry body values
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__
    assert MARKER not in "".join(traceback.format_exception(exc))
    assert MARKER not in caplog.text


def test_body_too_large_by_header() -> None:
    exc, _, _ = run(raw(b" " * (MAX_BODY_BYTES + 1)), expect_error(geocode()))
    assert isinstance(exc, GeocoderUnavailableError)
    assert exc.message == "geocoder response too large"


def test_declared_length_refused_before_reading() -> None:
    consumed: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        consumed.append(1)
        yield b"[]"

    def responder(request: httpx.Request) -> httpx.Response:
        headers = {"content-length": str(MAX_BODY_BYTES + 1)}
        return httpx.Response(200, headers=headers, content=chunks())

    exc, _, _ = run(responder, expect_error(geocode()))
    assert isinstance(exc, GeocoderUnavailableError)
    assert exc.message == "geocoder response too large"
    assert consumed == []


def test_body_too_large_streamed() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b" " * (MAX_BODY_BYTES // 2)

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    exc, _, _ = run(responder, expect_error(geocode()))
    assert isinstance(exc, GeocoderUnavailableError)
    assert exc.message == "geocoder response too large"


def test_body_at_limit_is_read() -> None:
    body = json.dumps([HIT]).encode()
    padded = body + b" " * (MAX_BODY_BYTES - len(body))
    assert len(padded) == MAX_BODY_BYTES
    res, _, _ = run(raw(padded), geocode())
    assert res.display_name == "Bucharest, Romania"


# cache


def test_cache_hit_bypasses_network() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> list[Any]:
        return [await geo.geocode(c) for c in ("Bucharest", " bucharest ", "BUCHAREST")]

    results, rec, _ = run(ok(), scenario)
    assert rec.calls == 1
    assert [r.query for r in results] == ["Bucharest", "bucharest", "BUCHAREST"]
    assert len({r.center for r in results}) == 1


def test_cache_collapses_inner_whitespace() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> None:
        await geo.geocode("New York")
        await geo.geocode("new   york")

    _, rec, _ = run(ok(), scenario)
    assert rec.calls == 1


def counting(responder: Responder) -> tuple[Responder, list[int]]:
    seen: list[int] = []

    def wrapped(request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        seen.append(1)
        return responder(request)

    return wrapped, seen


def test_found_ttl() -> None:
    responder, seen = counting(ok())

    async def scenario(geo: Geocoder, clock: FakeClock) -> list[int]:
        await geo.geocode("Bucharest")
        clock.advance(FOUND_TTL_S - 1)
        await geo.geocode("Bucharest")
        after_hit = len(seen)
        clock.advance(1)
        await geo.geocode("Bucharest")
        return [after_hit, len(seen)]

    counts, _, _ = run(responder, scenario)
    assert counts == [1, 2]


def test_not_found_ttl() -> None:
    responder, seen = counting(ok([]))

    async def scenario(geo: Geocoder, clock: FakeClock) -> list[int]:
        with pytest.raises(CityNotFoundError):
            await geo.geocode("Nowhereville")
        clock.advance(NOT_FOUND_TTL_S - 1)
        with pytest.raises(CityNotFoundError):
            await geo.geocode("nowhereville")
        after_hit = len(seen)
        clock.advance(1)
        with pytest.raises(CityNotFoundError):
            await geo.geocode("Nowhereville")
        return [after_hit, len(seen)]

    counts, _, _ = run(responder, scenario)
    assert counts == [1, 2]


def test_lru_eviction() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> None:
        await geo.geocode("A")
        await geo.geocode("B")
        await geo.geocode("A")
        await geo.geocode("C")
        await geo.geocode("A")
        await geo.geocode("B")

    _, rec, _ = run(ok(), scenario, max_cache_entries=2)
    assert [r.url.params["q"] for r in rec.requests] == ["A", "B", "C", "B"]


# concurrency and spacing


def test_concurrent_same_city_one_call() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> list[Any]:
        return list(await asyncio.gather(*(geo.geocode("Bucharest") for _ in range(3))))

    results, rec, _ = run(ok(), scenario)
    assert rec.calls == 1
    assert len(results) == 3
    assert all(r == results[0] for r in results)


def test_concurrent_same_city_shares_failure() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> list[Any]:
        calls = [geo.geocode("Bucharest") for _ in range(3)]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results, rec, _ = run(status(500), scenario)
    assert rec.calls == 1
    assert all(isinstance(r, GeocoderUnavailableError) for r in results)


def test_concurrent_same_city_shares_not_found() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> list[Any]:
        calls = [geo.geocode("Nowhereville") for _ in range(3)]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results, rec, _ = run(ok([]), scenario)
    assert rec.calls == 1
    assert all(isinstance(r, CityNotFoundError) for r in results)


def test_sequential_requests_are_spaced() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> None:
        await geo.geocode("A")
        await geo.geocode("B")

    _, rec, clock = run(ok(), scenario)
    assert rec.times[1] - rec.times[0] == pytest.approx(1.1)
    assert clock.sleeps == [pytest.approx(1.1)]


def test_no_wait_when_interval_already_passed() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> None:
        await geo.geocode("A")
        clock.advance(5)
        await geo.geocode("B")

    _, rec, clock = run(ok(), scenario)
    assert rec.calls == 2
    assert clock.sleeps == []


def test_concurrent_different_cities_are_spaced() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> None:
        await asyncio.gather(*(geo.geocode(c) for c in ("A", "B", "C")))

    _, rec, clock = run(ok(), scenario)
    assert rec.calls == 3
    assert clock.sleeps == [pytest.approx(1.1), pytest.approx(1.1)]


def test_cache_hit_does_not_wait() -> None:
    async def scenario(geo: Geocoder, clock: FakeClock) -> None:
        await geo.geocode("A")
        await geo.geocode("A")

    _, _, clock = run(ok(), scenario)
    assert clock.sleeps == []


# constructor


def make(**kwargs: Any) -> Geocoder:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    args: dict[str, Any] = {"user_agent": UA} | kwargs
    return Geocoder(client, **args)


@pytest.mark.parametrize(
    "agent",
    [
        "",
        "   ",
        f"python-httpx/{httpx.__version__}",
        "python-requests/2.32.3",
        "Python-urllib/3.12",
        "curl/8.5.0",
        "Wget/1.21",
        "Mozilla/5.0 (X11; Linux x86_64)",
        "aiohttp/3.9",
        "app\r\nX-Injected: 1",
        "x" * 201,
    ],
)
def test_bad_user_agent_rejected(agent: str) -> None:
    with pytest.raises(ValueError, match="user_agent"):
        make(user_agent=agent)


@pytest.mark.parametrize(
    "url",
    [
        "http://nominatim.openstreetmap.org",
        "ftp://nominatim.openstreetmap.org",
        "",
        "https://",
        "nominatim.openstreetmap.org",
        "https://geo.example.org/?key=1",
        "https://user:pass@geo.example.org",
    ],
)
def test_bad_base_url_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        make(base_url=url)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_interval_s": 0.5},
        {"min_interval_s": 0.99},
        {"found_ttl_s": 0},
        {"not_found_ttl_s": -1},
        {"max_cache_entries": 0},
        {"total_timeout_s": 0},
    ],
)
def test_bad_limits_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make(**kwargs)


def test_policy_minimum_interval_allowed() -> None:
    make(min_interval_s=1.0)


def test_spec_limits() -> None:
    # values agreed in the design, change only on purpose
    assert FOUND_TTL_S == 24 * 3600
    assert NOT_FOUND_TTL_S == 3600
    assert MAX_BODY_BYTES == 100_000
    assert MAX_DISPLAY_NAME == 300
    assert REFUSAL_COOLDOWN_S == 60


def test_error_hierarchy() -> None:
    assert issubclass(CityNotFoundError, GeocoderError)
    assert issubclass(GeocoderUnavailableError, GeocoderError)
    assert not issubclass(CityNotFoundError, GeocoderUnavailableError)
    err = GeocoderUnavailableError("boom")
    assert (str(err), err.message) == ("boom", "boom")