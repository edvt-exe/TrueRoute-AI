import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main
from app.config import ConfigError, load_settings
from app.main import (
    ATTRIBUTION,
    ATTRIBUTION_URL,
    ERROR_TABLE,
    MAX_BODY_BYTES,
    REQUEST_TIMEOUT_S,
    Services,
    create_app,
)
from app.schemas import (
    BoundingBox,
    Coordinates,
    GeocodeResult,
    LLMItinerary,
    Place,
    PlaceCandidates,
    PlaceCategory,
    UserRequest,
    resolve,
)
from app.services.geocoder import CityNotFoundError, Geocoder, GeocoderError, GeocoderUnavailableError
from app.services.llm_agent import (
    AgentResult,
    AgentUsage,
    LLMAgent,
    LLMConfigError,
    LLMInvalidOutputError,
    LLMRefusedError,
    LLMTruncatedError,
    LLMUnavailableError,
    NotEnoughPlacesError,
)
from app.services.overpass_client import NoPlacesFoundError, OverpassClient, OverpassUnavailableError
from app.services.osrm_routes import OSRM_ATTRIBUTION, OsrmRouter
from app.services.walking_routes import ORS_ATTRIBUTION, WalkingRouter
from app.security import CSP, DemoGuard

MARKER = "SECRET-MARKER"
BODY = {"city": "Bucharest", "days": 2, "pace": "moderate", "interests": ["history"]}
GEO = GeocodeResult(
    query="Bucharest",
    display_name="Bucharest, Romania",
    center=Coordinates(lat=44.4361414, lon=26.102684),
    bbox=BoundingBox(south=44.33, west=25.96, north=44.54, east=26.22),
)
CANDS = PlaceCandidates(
    center=Coordinates(lat=44.436, lon=26.103),
    radius_m=7718,
    places=[
        Place(
            ref=f"p{n}",
            osm_type="node",
            osm_id=n,
            name=f"Place {n}",
            category=PlaceCategory.ATTRACTION,
            coords=Coordinates(lat=44.43 + n * 0.001, lon=26.1),
        )
        for n in range(1, 5)
    ],
)


def agent_result(request: UserRequest) -> AgentResult:
    days = [{"day": d, "place_refs": [f"p{2 * d - 1}", f"p{2 * d}"], "theme": f"Day {d}"} for d in range(1, request.days + 1)]
    itinerary = resolve(LLMItinerary.model_validate({"days": days}), CANDS, request)
    usage = AgentUsage(model="claude-sonnet-5-5", attempts=1, input_tokens=2895, output_tokens=113, latency_s=3.6, request_ids=("req_1",))
    return AgentResult(itinerary=itinerary, usage=usage)


class FakeGeocoder:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def geocode(self, city: str) -> GeocodeResult:
        self.calls.append(city)
        if self.error:
            raise self.error
        return GEO


class FakePlaces:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[GeocodeResult] = []

    async def fetch_candidates(self, geo: GeocodeResult) -> PlaceCandidates:
        self.calls.append(geo)
        if self.error:
            raise self.error
        return CANDS


class FakePlanner:
    def __init__(self, error: Exception | None = None, delay_s: float = 0.0) -> None:
        self.error = error
        self.delay_s = delay_s
        self.calls: list[tuple[UserRequest, PlaceCandidates]] = []

    async def plan(self, request: UserRequest, candidates: PlaceCandidates) -> AgentResult:
        self.calls.append((request, candidates))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error:
            raise self.error
        return agent_result(request)


def fakes(
    geocoder: FakeGeocoder | None = None,
    places: FakePlaces | None = None,
    planner: FakePlanner | None = None,
) -> Services:
    return Services(geocoder=geocoder or FakeGeocoder(), places=places or FakePlaces(), planner=planner or FakePlanner())


def post(services: Services, body: Any = BODY, **kwargs: Any) -> Any:
    with TestClient(create_app(services=services, **kwargs), raise_server_exceptions=False) as client:
        return client.post("/api/itinerary", json=body)


# happy path


def test_itinerary_ok() -> None:
    svc = fakes()
    res = post(svc)
    assert res.status_code == 200
    data = res.json()
    assert set(data) == {"itinerary", "attribution", "attribution_url", "routing_attribution"}
    assert data["routing_attribution"] is None
    assert data["attribution"] == "Data © OpenStreetMap contributors, available under the Open Database License (ODbL)"
    assert data["attribution_url"] == "https://www.openstreetmap.org/copyright"
    it = data["itinerary"]
    assert (it["city"], it["days"], it["pace"], it["budget"]) == ("Bucharest", 2, "moderate", "medium")
    stops = [[s["place"]["name"] for s in d["stops"]] for d in it["day_plans"]]
    assert stops == [["Place 1", "Place 2"], ["Place 3", "Place 4"]]
    first = it["day_plans"][0]["stops"][0]["place"]
    assert first == {
        "ref": "p1",
        "osm_type": "node",
        "osm_id": 1,
        "name": "Place 1",
        "category": "attraction",
        "coords": {"lat": 44.431, "lon": 26.1},
        "address": None,
    }
    assert "aerial" in it["disclaimer"]


def test_pipeline_wiring() -> None:
    geo, places, planner = FakeGeocoder(), FakePlaces(), FakePlanner()
    post(fakes(geo, places, planner), body=BODY | {"city": "  Bucharest "})
    assert geo.calls == ["Bucharest"]
    assert places.calls == [GEO]
    request, candidates = planner.calls[0]
    assert request == UserRequest.model_validate(BODY)
    assert candidates == CANDS


def test_no_internals_in_response() -> None:
    text = post(fakes()).text
    for internal in ("req_1", "2895", "claude-sonnet", "input_tokens", "usage"):
        assert internal not in text


def test_health_makes_no_upstream_calls() -> None:
    svc = fakes()
    with TestClient(create_app(services=svc)) as client:
        res = client.get("/api/health")
    assert (res.status_code, res.json()) == (200, {"status": "ok"})
    assert svc.geocoder.calls == []  # type: ignore[attr-defined]


def test_docs_are_on() -> None:
    with TestClient(create_app(services=fakes())) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
    assert "/api/itinerary" in schema["paths"]


# error table


ERROR_CASES = [
    (FakeGeocoder(CityNotFoundError(f"city not found {MARKER}")), None, None, 404, "city_not_found", "city not found"),
    (FakeGeocoder(GeocoderUnavailableError(f"geocoder returned http 500 {MARKER}")), None, None, 503, "upstream_unavailable", "a data provider is unavailable, try again later"),
    (None, FakePlaces(NoPlacesFoundError(MARKER)), None, 422, "not_enough_places", "not enough places found near this city"),
    (None, FakePlaces(OverpassUnavailableError(MARKER)), None, 503, "upstream_unavailable", "a data provider is unavailable, try again later"),
    (None, None, FakePlanner(NotEnoughPlacesError(MARKER)), 422, "not_enough_places", "not enough places found near this city"),
    (None, None, FakePlanner(LLMUnavailableError(MARKER)), 503, "upstream_unavailable", "a data provider is unavailable, try again later"),
    (None, None, FakePlanner(LLMRefusedError(MARKER)), 502, "planner_failed", "the planner could not produce an itinerary, try again"),
    (None, None, FakePlanner(LLMTruncatedError(MARKER)), 502, "planner_failed", "the planner could not produce an itinerary, try again"),
    (None, None, FakePlanner(LLMInvalidOutputError(MARKER)), 502, "planner_failed", "the planner could not produce an itinerary, try again"),
    (None, None, FakePlanner(LLMConfigError(MARKER)), 500, "server_misconfigured", "the server is misconfigured"),
]


@pytest.mark.parametrize(("geo", "places", "planner", "status", "code", "message"), ERROR_CASES)
def test_error_mapping(geo: Any, places: Any, planner: Any, status: int, code: str, message: str) -> None:
    res = post(fakes(geo, places, planner))
    assert res.status_code == status
    assert res.json() == {"error": {"code": code, "message": message}}
    assert MARKER not in res.text


def test_error_table_covers_every_service_error() -> None:
    mapped = {cls for cls, *_ in ERROR_TABLE}
    assert mapped == {
        CityNotFoundError,
        GeocoderUnavailableError,
        NoPlacesFoundError,
        OverpassUnavailableError,
        NotEnoughPlacesError,
        LLMUnavailableError,
        LLMRefusedError,
        LLMTruncatedError,
        LLMInvalidOutputError,
        LLMConfigError,
    }


def test_errors_stop_the_pipeline() -> None:
    places, planner = FakePlaces(), FakePlanner()
    post(fakes(FakeGeocoder(CityNotFoundError("x")), places, planner))
    assert places.calls == [] and planner.calls == []


def test_unmapped_service_error_is_internal() -> None:
    res = post(fakes(FakeGeocoder(GeocoderError(MARKER))))
    assert res.status_code == 500
    assert res.json() == {"error": {"code": "internal_error", "message": "internal server error"}}


def test_unexpected_exception_is_internal(caplog: pytest.LogCaptureFixture) -> None:
    res = post(fakes(planner=FakePlanner(RuntimeError(f"boom {MARKER}"))))
    assert res.status_code == 500
    assert res.json() == {"error": {"code": "internal_error", "message": "internal server error"}}
    assert MARKER not in res.text
    assert "unhandled error" in caplog.text


# timeout


def test_pipeline_timeout() -> None:
    res = post(fakes(planner=FakePlanner(delay_s=5)), request_timeout_s=0.05)
    assert res.status_code == 504
    assert res.json() == {"error": {"code": "request_timeout", "message": "the request took too long, try again"}}


def test_timeout_default_and_validation() -> None:
    assert REQUEST_TIMEOUT_S == 150
    with pytest.raises(ValueError):
        create_app(services=fakes(), request_timeout_s=0)


# request validation and limits


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"days": 2}, "city: Field required"),
        (BODY | {"days": 8}, "days: "),
        (BODY | {"pace": "turbo"}, "pace: "),
        (BODY | {"extra": 1}, "extra: Extra inputs are not permitted"),
        (BODY | {"city": "a\nb"}, "city: "),
    ],
)
def test_invalid_body(body: dict[str, Any], field: str) -> None:
    svc = fakes()
    res = post(svc, body=body)
    assert res.status_code == 422
    err = res.json()["error"]
    assert err["code"] == "invalid_request"
    assert field in err["message"]
    assert svc.geocoder.calls == []  # type: ignore[attr-defined]


def test_invalid_values_not_echoed() -> None:
    res = post(fakes(), body=BODY | {"days": MARKER, "pace": MARKER, "nope": MARKER})
    assert res.status_code == 422
    assert MARKER not in res.text


def test_malformed_json() -> None:
    with TestClient(create_app(services=fakes())) as client:
        res = client.post("/api/itinerary", content=b'{"city": ', headers={"content-type": "application/json"})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_request"


def test_body_too_large() -> None:
    svc = fakes()
    big = b'{"city": "' + b"x" * MAX_BODY_BYTES + b'"}'
    with TestClient(create_app(services=svc)) as client:
        res = client.post("/api/itinerary", content=big, headers={"content-type": "application/json"})
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "payload_too_large"
    assert svc.geocoder.calls == []  # type: ignore[attr-defined]


def test_body_at_limit_is_accepted() -> None:
    body = b'{"city": "Bucharest", "days": 2}'
    padded = body + b" " * (MAX_BODY_BYTES - len(body))
    with TestClient(create_app(services=fakes())) as client:
        res = client.post("/api/itinerary", content=padded, headers={"content-type": "application/json"})
    assert res.status_code == 200
    assert MAX_BODY_BYTES == 16_384


def test_unknown_route_and_method_use_error_shape() -> None:
    with TestClient(create_app(services=fakes())) as client:
        missing = client.get("/nope")
        wrong = client.get("/api/itinerary")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "http_404", "message": "Not Found"}}
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "http_405"


# logging


def test_success_log_has_no_user_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.main")
    post(fakes(), body=BODY | {"city": f"Bucharest {MARKER}", "interests": [MARKER]})
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "app.main")
    assert "itinerary ok: days 2 pace moderate" in text
    assert "tokens in 2895 out 113" in text
    assert MARKER not in text
    assert "Place 1" not in text


def test_error_log_names_code_not_message(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.main")
    post(fakes(planner=FakePlanner(LLMRefusedError(MARKER))))
    assert "itinerary failed: planner_failed (LLMRefusedError)" in caplog.text
    assert MARKER not in caplog.text


# real lifespan wiring, no network calls


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("ANTHROPIC_API_KEY", "APP_USER_AGENT", "LLM_MODEL", "LLM_EFFORT", "LOG_LEVEL", "NOMINATIM_BASE_URL", "OVERPASS_BASE_URL", "ORS_API_KEY", "ORS_BASE_URL", "OSRM_ENABLED", "OSRM_BASE_URL", "DEMO_ACCESS_CODE", "RATE_LIMIT_PER_IP_HOUR", "DAILY_ITINERARY_CAP"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        f"ANTHROPIC_API_KEY=sk-ant-test-{MARKER}\nAPP_USER_AGENT=TrueRoute-AI-tests/0.1 (+https://example.org)\n"
        "LLM_MODEL=claude-haiku-5-5\nLLM_EFFORT=low\nLOG_LEVEL=WARNING\n"
        "NOMINATIM_BASE_URL=https://geo.example.org\nOVERPASS_BASE_URL=https://ovp.example.org/api\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def restore_logging() -> Any:
    names = ("", "anthropic", "httpx2", "httpx", "httpcore")
    saved = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_lifespan_builds_and_closes_real_services(settings_file: Path, restore_logging: None) -> None:
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("anthropic").setLevel(logging.DEBUG)
    app = create_app(settings=load_settings(settings_file))
    with TestClient(app) as client:
        svc: Services = app.state.services
        assert isinstance(svc.geocoder, Geocoder)
        assert isinstance(svc.places, OverpassClient)
        assert isinstance(svc.planner, LLMAgent)
        assert svc.planner._model == "claude-haiku-5-5"
        assert svc.planner._effort == "low"
        # one shared http client for both osm services
        assert svc.geocoder._client is svc.places._client
        assert svc.geocoder._user_agent == "TrueRoute-AI-tests/0.1 (+https://example.org)"
        assert svc.places._user_agent == "TrueRoute-AI-tests/0.1 (+https://example.org)"
        # urls and log levels come from the settings
        assert svc.geocoder._search_url == "https://geo.example.org/search"
        assert svc.places._url == "https://ovp.example.org/api/interpreter"
        # walking routes share the client, off without a key
        assert isinstance(svc.routes, WalkingRouter)
        assert svc.routes._client is svc.geocoder._client
        # no ors key in the test settings, the osrm fallback is on by default
        assert svc.routes._key is None
        assert isinstance(svc.routes._fallback, OsrmRouter)
        assert svc.routes._fallback._client is svc.geocoder._client
        assert svc.routes.enabled is True
        # demo guard on with the default limits, no code in the test settings
        guard: DemoGuard = app.state.guard
        assert isinstance(guard, DemoGuard)
        assert guard.requires_code is False
        assert guard._per_ip_hour == 5
        assert logging.getLogger().level == logging.WARNING
        assert logging.getLogger("anthropic").level == logging.WARNING
        assert client.get("/api/health").status_code == 200
        http = svc.geocoder._client
        llm = svc.planner._client
        assert not http.is_closed and not llm.is_closed()
    assert http.is_closed
    assert llm.is_closed()


def test_lifespan_loads_settings_at_startup(settings_file: Path, monkeypatch: pytest.MonkeyPatch, restore_logging: None) -> None:
    calls: list[int] = []

    def fake_load() -> Any:
        calls.append(1)
        return load_settings(settings_file)

    monkeypatch.setattr(main, "load_settings", fake_load)
    app = create_app()
    assert calls == []
    with TestClient(app):
        assert calls == [1]


def test_bad_settings_stop_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / ".env"
    bad.write_text("LLM_EFFORT=max\n", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("APP_USER_AGENT", raising=False)
    monkeypatch.setattr(main, "load_settings", lambda: load_settings(bad))
    with pytest.raises(ConfigError) as info:
        with TestClient(create_app()):
            pass
    assert "LLM_EFFORT" in info.value.message


def test_module_app_exists_without_env() -> None:
    assert main.app.title == "TrueRoute AI"
    assert ATTRIBUTION.startswith("Data © OpenStreetMap contributors")
    assert ATTRIBUTION_URL == "https://www.openstreetmap.org/copyright"

# walking routes


def ors_ok(request: httpx.Request) -> httpx.Response:
    # minimal ors v2 geojson shape, straight lines between the stops
    coords = json.loads(request.content)["coordinates"]
    legs = len(coords) - 1
    body = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "segments": [{"distance": 250.0, "duration": 180.0} for _ in range(legs)],
                    "way_points": list(range(len(coords))),
                },
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        ],
    }
    return httpx.Response(200, json=body)


def post_with_ors(responder: Any, key: str | None = f"ors-{MARKER}") -> tuple[Any, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    async def build() -> WalkingRouter:
        return WalkingRouter(
            httpx.AsyncClient(transport=httpx.MockTransport(record)),
            SecretStr(key) if key else None,
            "TrueRoute-AI-tests/0.1 (+https://example.org)",
        )

    router = asyncio.run(build())
    svc = Services(geocoder=FakeGeocoder(), places=FakePlaces(), planner=FakePlanner(), routes=router)
    return post(svc), seen


def test_walking_days_in_response() -> None:
    res, seen = post_with_ors(ors_ok)
    assert res.status_code == 200
    data = res.json()
    assert len(seen) == 2
    assert data["routing_attribution"] == ORS_ATTRIBUTION
    it = data["itinerary"]
    assert "aerial" not in it["disclaimer"]
    day = it["day_plans"][0]
    assert day["distance_source"] == "walking"
    assert day["total_walk_minutes"] == 3
    assert day["stops"][1]["leg_distance_km"] == 0.25
    assert day["stops"][1]["leg_walk_minutes"] == 3
    # lat, lon pairs for leaflet
    assert day["route_latlon"] == [[44.431, 26.1], [44.432, 26.1]]
    assert MARKER not in res.text


def test_routing_failure_keeps_aerial_itinerary() -> None:
    res, seen = post_with_ors(lambda r: httpx.Response(503))
    assert res.status_code == 200
    data = res.json()
    assert data["routing_attribution"] is None
    it = data["itinerary"]
    assert [d["distance_source"] for d in it["day_plans"]] == ["aerial", "aerial"]
    assert [d["route_latlon"] for d in it["day_plans"]] == [[], []]
    assert it["day_plans"][0]["stops"][1]["leg_walk_minutes"] is None
    assert "aerial" in it["disclaimer"]


def test_mixed_days_response() -> None:
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return ors_ok(request) if calls["n"] == 1 else httpx.Response(404, json={"error": {"code": 2010}})

    res, _ = post_with_ors(responder)
    data = res.json()
    assert data["routing_attribution"] == ORS_ATTRIBUTION
    assert [d["distance_source"] for d in data["itinerary"]["day_plans"]] == ["walking", "aerial"]
    assert "aerial" in data["itinerary"]["disclaimer"]


def test_no_ors_key_makes_no_call() -> None:
    res, seen = post_with_ors(ors_ok, key=None)
    assert res.status_code == 200
    assert seen == []
    assert res.json()["routing_attribution"] is None


class SlowRoutes:
    async def attach(self, itinerary: Any) -> Any:
        await asyncio.sleep(5)
        return itinerary


def test_routing_counts_toward_request_timeout() -> None:
    svc = Services(geocoder=FakeGeocoder(), places=FakePlaces(), planner=FakePlanner(), routes=SlowRoutes())
    res = post(svc, request_timeout_s=0.05)
    assert res.status_code == 504

# demo guard and /api


CODE = f"demo-{MARKER}"


def guarded(guard: DemoGuard, svc: Services | None = None) -> tuple[TestClient, Services]:
    svc = svc or fakes()
    return TestClient(create_app(services=svc, guard=guard), raise_server_exceptions=False), svc


def test_access_code_required() -> None:
    client, svc = guarded(DemoGuard(SecretStr(CODE)))
    with client:
        missing = client.post("/api/itinerary", json=BODY)
        wrong = client.post("/api/itinerary", json=BODY, headers={"X-Access-Code": "wrong-code"})
        right = client.post("/api/itinerary", json=BODY, headers={"X-Access-Code": CODE})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == {"error": {"code": "access_denied", "message": "a valid access code is required"}}
    assert right.status_code == 200
    # refused requests never reach the pipeline
    assert svc.geocoder.calls == ["Bucharest"]  # type: ignore[attr-defined]
    assert MARKER not in missing.text + wrong.text


def test_access_checked_before_body_validation() -> None:
    client, _ = guarded(DemoGuard(SecretStr(CODE)))
    with client:
        res = client.post("/api/itinerary", json={"city": ""})
    assert res.status_code == 401


def test_rate_limit_response() -> None:
    client, svc = guarded(DemoGuard(None, per_ip_hour=2))
    with client:
        codes = [client.post("/api/itinerary", json=BODY).status_code for _ in range(2)]
        limited = client.post("/api/itinerary", json=BODY)
    assert codes == [200, 200]
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert limited.headers["retry-after"].isdigit()
    assert 1 <= int(limited.headers["retry-after"]) <= 3600
    assert len(svc.geocoder.calls) == 2  # type: ignore[attr-defined]


def test_daily_cap_response() -> None:
    client, _ = guarded(DemoGuard(None, per_ip_hour=10, daily_cap=1))
    with client:
        client.post("/api/itinerary", json=BODY)
        res = client.post("/api/itinerary", json=BODY)
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "daily_cap_reached"
    assert "retry-after" in res.headers


def test_forwarded_header_cannot_dodge_the_limit() -> None:
    # proxies are resolved by uvicorn FORWARDED_ALLOW_IPS, the app never reads the header
    client, _ = guarded(DemoGuard(None, per_ip_hour=1))
    with client:
        first = client.post("/api/itinerary", json=BODY, headers={"X-Forwarded-For": "198.51.100.1"})
        second = client.post("/api/itinerary", json=BODY, headers={"X-Forwarded-For": "198.51.100.2"})
    assert (first.status_code, second.status_code) == (200, 429)


def test_health_is_not_guarded() -> None:
    client, _ = guarded(DemoGuard(SecretStr(CODE), per_ip_hour=1))
    with client:
        statuses = [client.get("/api/health").status_code for _ in range(5)]
    assert statuses == [200] * 5


def test_old_paths_are_gone() -> None:
    with TestClient(create_app(services=fakes())) as client:
        assert client.post("/itinerary", json=BODY).status_code == 404
        assert client.get("/health").status_code == 404
        paths = set(client.get("/openapi.json").json()["paths"])
    assert paths == {"/api/itinerary", "/api/health"}


def test_openapi_documents_guard_errors() -> None:
    with TestClient(create_app(services=fakes())) as client:
        responses = client.get("/openapi.json").json()["paths"]["/api/itinerary"]["post"]["responses"]
    assert {"401", "429"} <= set(responses)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/itinerary", BODY),
        ("get", "/api/health", None),
        ("get", "/api/nope", None),
        ("post", "/api/itinerary", {"city": ""}),
    ],
)
def test_security_headers_on_api(method: str, path: str, body: Any) -> None:
    with TestClient(create_app(services=fakes())) as client:
        res = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert res.headers["content-security-policy"] == CSP
    assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["cache-control"] == "no-store"


def test_security_headers_on_413_and_refusals() -> None:
    client, _ = guarded(DemoGuard(SecretStr(CODE)))
    with client:
        big = client.post("/api/itinerary", content=b"x" * (MAX_BODY_BYTES + 1), headers={"content-type": "application/json"})
        denied = client.post("/api/itinerary", json=BODY)
    for res in (big, denied):
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["content-security-policy"] == CSP
    assert big.status_code == 413


def test_docs_page_works_without_strict_csp() -> None:
    with TestClient(create_app(services=fakes())) as client:
        res = client.get("/docs")
    assert res.status_code == 200
    assert "content-security-policy" not in res.headers
    assert res.headers["x-frame-options"] == "DENY"


def test_lifespan_builds_guard_with_code(settings_file: Path, restore_logging: None) -> None:
    with settings_file.open("a", encoding="utf-8") as f:
        f.write(f"DEMO_ACCESS_CODE={CODE}\nRATE_LIMIT_PER_IP_HOUR=7\nDAILY_ITINERARY_CAP=50\n")
    app = create_app(settings=load_settings(settings_file))
    with TestClient(app, raise_server_exceptions=False) as client:
        guard: DemoGuard = app.state.guard
        assert guard.requires_code is True
        assert guard._per_ip_hour == 7
        assert guard._daily._limit == 50
        # refused before any upstream call, so no network is needed here
        res = client.post("/api/itinerary", json=BODY)
    assert res.status_code == 401


def test_lifespan_without_osrm(settings_file: Path, restore_logging: None) -> None:
    with settings_file.open("a", encoding="utf-8") as f:
        f.write("OSRM_ENABLED=false\n")
    app = create_app(settings=load_settings(settings_file))
    with TestClient(app):
        assert app.state.services.routes._fallback is None
        assert app.state.services.routes.enabled is False


def plan_with(providers: list[str | None]) -> Any:
    from app.schemas import DayPlan, DayStop, Itinerary

    plans = []
    for d, provider in enumerate(providers, start=1):
        a, b = CANDS.places[0], CANDS.places[1]
        if provider is None:
            plans.append(DayPlan(day=d, theme="t", stops=[DayStop(order=1, place=a), DayStop(order=2, place=b, leg_distance_km=0.1)], total_distance_km=0.1))
        else:
            plans.append(
                DayPlan(
                    day=d,
                    theme="t",
                    stops=[DayStop(order=1, place=a), DayStop(order=2, place=b, leg_distance_km=0.2, leg_walk_minutes=3)],
                    total_distance_km=0.2,
                    distance_source="walking",
                    total_walk_minutes=3,
                    route_latlon=[(44.431, 26.1), (44.432, 26.1)],
                    route_provider=provider,
                )
            )
    return Itinerary(city="Bucharest", days=len(plans), pace="moderate", budget="medium", day_plans=plans, unused_places=0)


@pytest.mark.parametrize(
    ("providers", "expected"),
    [
        ([None, None], None),
        (["openrouteservice", None], ORS_ATTRIBUTION),
        (["osrm", "osrm"], OSRM_ATTRIBUTION),
        (["osrm", "openrouteservice"], f"{ORS_ATTRIBUTION}; {OSRM_ATTRIBUTION}"),
    ],
)
def test_routing_attribution_credits_each_provider(providers: list[str | None], expected: str | None) -> None:
    assert main.routing_attribution(plan_with(providers)) == expected