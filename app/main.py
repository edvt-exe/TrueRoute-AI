import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

import anthropic
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, configure_logging, load_settings
from app.schemas import GeocodeResult, Itinerary, PlaceCandidates, UserRequest
from app.services.geocoder import CityNotFoundError, Geocoder, GeocoderError, GeocoderUnavailableError
from app.services.llm_agent import (
    AgentResult,
    LLMAgent,
    LLMConfigError,
    LLMError,
    LLMInvalidOutputError,
    LLMRefusedError,
    LLMTruncatedError,
    LLMUnavailableError,
    NotEnoughPlacesError,
)
from app.services.overpass_client import (
    NoPlacesFoundError,
    OverpassClient,
    OverpassError,
    OverpassUnavailableError,
)

logger = logging.getLogger(__name__)

# whole pipeline cap, longer than this is a bad synchronous request
REQUEST_TIMEOUT_S = 150.0
# assumption: a valid UserRequest is under 1 KB
MAX_BODY_BYTES = 16_384
# osm copyright page: show the attribution notice and make the odbl licence clear
ATTRIBUTION = "Data © OpenStreetMap contributors, available under the Open Database License (ODbL)"
ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"

_UNAVAILABLE = "a data provider is unavailable, try again later"
_PLANNER = "the planner could not produce an itinerary, try again"
_FEW_PLACES = "not enough places found near this city"

# fixed public messages, internal error text never reaches the client
ERROR_TABLE: tuple[tuple[type[Exception], int, str, str], ...] = (
    (CityNotFoundError, 404, "city_not_found", "city not found"),
    (NoPlacesFoundError, 422, "not_enough_places", _FEW_PLACES),
    (NotEnoughPlacesError, 422, "not_enough_places", _FEW_PLACES),
    (GeocoderUnavailableError, 503, "upstream_unavailable", _UNAVAILABLE),
    (OverpassUnavailableError, 503, "upstream_unavailable", _UNAVAILABLE),
    (LLMUnavailableError, 503, "upstream_unavailable", _UNAVAILABLE),
    (LLMRefusedError, 502, "planner_failed", _PLANNER),
    (LLMTruncatedError, 502, "planner_failed", _PLANNER),
    (LLMInvalidOutputError, 502, "planner_failed", _PLANNER),
    (LLMConfigError, 500, "server_misconfigured", "the server is misconfigured"),
)


class PipelineTimeoutError(Exception):
    pass


class GeocoderService(Protocol):
    async def geocode(self, city: str) -> GeocodeResult: ...


class PlacesService(Protocol):
    async def fetch_candidates(self, geo: GeocodeResult) -> PlaceCandidates: ...


class PlannerService(Protocol):
    async def plan(self, request: UserRequest, candidates: PlaceCandidates) -> AgentResult: ...


@dataclass(frozen=True)
class Services:
    geocoder: GeocoderService
    places: PlacesService
    planner: PlannerService


class ItineraryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    itinerary: Itinerary
    attribution: str = ATTRIBUTION
    attribution_url: str = ATTRIBUTION_URL


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=body.model_dump())


async def _pipeline_error(request: Request, exc: Exception) -> JSONResponse:
    for cls, status, code, message in ERROR_TABLE:
        if isinstance(exc, cls):
            log = logger.error if status == 500 else logger.warning
            log("itinerary failed: %s (%s)", code, type(exc).__name__)
            return _error(status, code, message)
    if isinstance(exc, PipelineTimeoutError):
        logger.warning("itinerary failed: request_timeout")
        return _error(504, "request_timeout", "the request took too long, try again")
    # a subclass nobody mapped yet, treat as a bug
    logger.error("itinerary failed: unmapped %s", type(exc).__name__)
    return _error(500, "internal_error", "internal server error")


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # field and reason only, the submitted values are never echoed
    parts = [
        f"{'.'.join(str(p) for p in err['loc'] if p != 'body') or 'body'}: {err['msg']}"
        for err in exc.errors()[:10]
    ]
    return _error(422, "invalid_request", "; ".join(parts))


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return _error(exc.status_code, f"http_{exc.status_code}", str(exc.detail))


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # traceback stays in the server log
    logger.exception("unhandled error")
    return _error(500, "internal_error", "internal server error")


def _build_lifespan(
    settings: Settings | None, services: Services | None
) -> Callable[[FastAPI], AsyncIterator[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            app.state.services = services
            yield
            return
        # a bad .env stops startup here with a safe ConfigError
        cfg = settings or load_settings()
        configure_logging(cfg)
        async with (
            httpx.AsyncClient() as http,
            anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key.get_secret_value()) as llm,
        ):
            app.state.services = Services(
                geocoder=Geocoder(http, cfg.app_user_agent, cfg.nominatim_base_url),
                places=OverpassClient(http, cfg.app_user_agent, cfg.overpass_base_url),
                planner=LLMAgent(llm, cfg.llm_model, cfg.llm_effort),
            )
            logger.info("trueroute started, model %s effort %s", cfg.llm_model, cfg.llm_effort)
            yield
        logger.info("trueroute stopped")

    return lifespan  # type: ignore[return-value]


def create_app(
    settings: Settings | None = None,
    services: Services | None = None,
    *,
    request_timeout_s: float = REQUEST_TIMEOUT_S,
) -> FastAPI:
    if request_timeout_s <= 0:
        raise ValueError("request_timeout_s must be positive")
    app = FastAPI(
        title="TrueRoute AI",
        version="0.1.0",
        lifespan=_build_lifespan(settings, services),  # type: ignore[arg-type]
    )
    for cls in (GeocoderError, OverpassError, LLMError, PipelineTimeoutError):
        app.add_exception_handler(cls, _pipeline_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)

    @app.middleware("http")
    async def limit_body(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _error(413, "payload_too_large", f"request body over {MAX_BODY_BYTES} bytes")
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        # no upstream calls, health checks must not spend osm or llm quota
        return {"status": "ok"}

    @app.post(
        "/itinerary",
        response_model=ItineraryResponse,
        responses={
            status: {"model": ErrorResponse}
            for status in (404, 413, 422, 500, 502, 503, 504)
        },
    )
    async def create_itinerary(body: UserRequest, request: Request) -> ItineraryResponse:
        services_: Services = request.app.state.services
        start = time.monotonic()
        try:
            async with asyncio.timeout(request_timeout_s):
                geo = await services_.geocoder.geocode(body.city)
                candidates = await services_.places.fetch_candidates(geo)
                result = await services_.planner.plan(body, candidates)
        except TimeoutError:
            raise PipelineTimeoutError() from None
        # no city, interests or place names in the log
        logger.info(
            "itinerary ok: days %d pace %s in %.1f s, llm attempts %d tokens in %d out %d",
            body.days,
            body.pace.value,
            time.monotonic() - start,
            result.usage.attempts,
            result.usage.input_tokens,
            result.usage.output_tokens,
        )
        return ItineraryResponse(itinerary=result.itinerary)

    return app


app = create_app()