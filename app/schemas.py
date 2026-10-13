import math
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

MAX_DAYS = 7
MAX_PLACES = 30
MAX_INTERESTS = 5
MAX_STOPS_PER_DAY = 5
MAX_RADIUS_M = 50_000
MAX_LLM_OUTPUT_CHARS = 20_000
MAX_DETAIL_CHARS = 300
EARTH_RADIUS_KM = 6371.0088
DISCLAIMER = "Distances are straight-line (aerial), not walking or driving routes"
WALKING_DISCLAIMER = "Walking distances and times are route estimates, check conditions on the ground"
MIXED_DISCLAIMER = "Days marked walking use route estimates, other days use straight-line (aerial) distances"
# a day route is downsampled to this many points to keep the response small
MAX_ROUTE_POINTS = 400


class Pace(StrEnum):
    RELAXED = "relaxed"
    MODERATE = "moderate"
    PACKED = "packed"


class Budget(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PlaceCategory(StrEnum):
    ATTRACTION = "attraction"
    RESTAURANT = "restaurant"


class DistanceSource(StrEnum):
    WALKING = "walking"
    AERIAL = "aerial"


# assumption: stop ceiling per day by pace, tune with real usage
MAX_STOPS_BY_PACE: dict[Pace, int] = {
    Pace.RELAXED: 3,
    Pace.MODERATE: 4,
    Pace.PACKED: 5,
}


def _printable(value: str) -> str:
    if not value.isprintable():
        raise ValueError("control characters are not allowed")
    return value


# all free text is untrusted and ends up in prompts, so no control chars
CityName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(_printable),
]
Interest = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
    AfterValidator(_printable),
]
Theme = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
    AfterValidator(_printable),
]
PlaceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(_printable),
]
Address = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
    AfterValidator(_printable),
]
# refs are p1..p30 only, no leading zeros
Ref = Annotated[str, StringConstraints(pattern=r"^p([1-9]|[12][0-9]|30)$")]
Lat = Annotated[float, Field(ge=-90, le=90)]
Lon = Annotated[float, Field(ge=-180, le=180)]
# lat, lon order, the order leaflet expects
LatLon = tuple[Lat, Lon]
# which service drew a walking day, the response credits it
RouteProvider = Literal["openrouteservice", "osrm"]


class ItineraryValidationError(Exception):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail[:MAX_DETAIL_CHARS]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Frozen(_Model):
    model_config = ConfigDict(frozen=True)


# strict because it is only ever parsed from model JSON
class _LLMModel(_Model):
    model_config = ConfigDict(strict=True, frozen=True)


class UserRequest(_Model):
    city: CityName
    days: int = Field(ge=1, le=MAX_DAYS)
    pace: Pace = Pace.MODERATE
    budget: Budget = Budget.MEDIUM
    interests: list[Interest] = Field(default_factory=list, max_length=MAX_INTERESTS)


class Coordinates(_Frozen):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    def distance_km(self, other: "Coordinates") -> float:
        # haversine, aerial distance
        phi1 = math.radians(self.lat)
        phi2 = math.radians(other.lat)
        dphi = phi2 - phi1
        dlmb = math.radians(other.lon - self.lon)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
        return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


class BoundingBox(_Frozen):
    south: float = Field(ge=-90, le=90)
    west: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def _check_lat_order(self) -> Self:
        # west > east is legal across the antimeridian, so only lat is checked
        if self.south > self.north:
            raise ValueError("south must not be greater than north")
        return self


class GeocodeResult(_Frozen):
    query: CityName
    display_name: Address
    center: Coordinates
    bbox: BoundingBox | None = None


class Place(_Frozen):
    ref: Ref
    osm_type: Literal["node", "way", "relation"]
    osm_id: int = Field(gt=0)
    name: PlaceName
    category: PlaceCategory
    coords: Coordinates
    address: Address | None = None


class PlaceCandidates(_Model):
    center: Coordinates
    radius_m: int = Field(gt=0, le=MAX_RADIUS_M)
    places: list[Place] = Field(min_length=1, max_length=MAX_PLACES)

    @model_validator(mode="after")
    def _check_unique(self) -> Self:
        refs = {p.ref for p in self.places}
        if len(refs) != len(self.places):
            raise ValueError("duplicate place refs")
        osm_keys = {(p.osm_type, p.osm_id) for p in self.places}
        if len(osm_keys) != len(self.places):
            raise ValueError("duplicate osm places")
        return self


class LLMDay(_LLMModel):
    day: int = Field(ge=1, le=MAX_DAYS)
    place_refs: list[Ref] = Field(min_length=1, max_length=MAX_STOPS_PER_DAY)
    theme: Theme


class LLMItinerary(_LLMModel):
    days: list[LLMDay] = Field(min_length=1, max_length=MAX_DAYS)

    @model_validator(mode="after")
    def _check_days_and_refs(self) -> Self:
        if [d.day for d in self.days] != list(range(1, len(self.days) + 1)):
            raise ValueError("days must be numbered 1..n in order")
        refs = [r for d in self.days for r in d.place_refs]
        if len(set(refs)) != len(refs):
            raise ValueError("a place ref is used more than once")
        return self


class DayStop(_Model):
    order: int = Field(ge=1, le=MAX_STOPS_PER_DAY)
    place: Place
    # walking km when the day is walking, aerial km otherwise
    leg_distance_km: float | None = Field(default=None, ge=0)
    leg_walk_minutes: int | None = Field(default=None, ge=0)


class DayPlan(_Model):
    day: int = Field(ge=1, le=MAX_DAYS)
    theme: Theme
    stops: list[DayStop] = Field(min_length=1, max_length=MAX_STOPS_PER_DAY)
    total_distance_km: float = Field(ge=0)
    distance_source: DistanceSource = DistanceSource.AERIAL
    total_walk_minutes: int | None = Field(default=None, ge=0)
    route_latlon: list[LatLon] = Field(default_factory=list, max_length=MAX_ROUTE_POINTS)
    route_provider: RouteProvider | None = None

    @model_validator(mode="after")
    def _check_source(self) -> Self:
        minutes = [s.leg_walk_minutes for s in self.stops[1:]]
        if self.distance_source is DistanceSource.AERIAL:
            # no invented walking time or path for a straight-line day
            if self.route_latlon or self.route_provider is not None or self.total_walk_minutes is not None or any(
                s.leg_walk_minutes is not None for s in self.stops
            ):
                raise ValueError("an aerial day has no walking route or minutes")
            return self
        if len(self.stops) < 2 or len(self.route_latlon) < 2:
            raise ValueError("a walking day needs at least two stops and a route")
        if self.route_provider is None:
            raise ValueError("a walking day needs the provider that drew it")
        if self.stops[0].leg_walk_minutes is not None or None in minutes:
            raise ValueError("a walking day needs minutes on every leg after the first stop")
        if self.total_walk_minutes != sum(m for m in minutes if m is not None):
            raise ValueError("total_walk_minutes must be the sum of the legs")
        return self


class Itinerary(_Model):
    city: CityName
    days: int = Field(ge=1, le=MAX_DAYS)
    pace: Pace
    budget: Budget
    day_plans: list[DayPlan] = Field(min_length=1, max_length=MAX_DAYS)
    unused_places: int = Field(ge=0, le=MAX_PLACES)
    disclaimer: str = DISCLAIMER

    @model_validator(mode="after")
    def _check_day_count(self) -> Self:
        if len(self.day_plans) != self.days:
            raise ValueError("day_plans must match days")
        return self


def disclaimer_for(plans: list[DayPlan]) -> str:
    sources = {p.distance_source for p in plans}
    if sources == {DistanceSource.WALKING}:
        return WALKING_DISCLAIMER
    if DistanceSource.WALKING in sources:
        return MIXED_DISCLAIMER
    return DISCLAIMER


def _safe_detail(exc: ValidationError) -> str:
    # loc and msg only, never the input value
    parts = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(p) for p in err["loc"])
        # a whole-document error such as broken json has no field path
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)


def parse_llm_output(raw: str) -> LLMItinerary:
    if len(raw) > MAX_LLM_OUTPUT_CHARS:
        raise ItineraryValidationError("model output too large")
    try:
        return LLMItinerary.model_validate_json(raw)
    except ValidationError as exc:
        # from None so the raw model output is not kept in the traceback chain
        raise ItineraryValidationError("invalid model output", _safe_detail(exc)) from None


def resolve(llm: LLMItinerary, candidates: PlaceCandidates, request: UserRequest) -> Itinerary:
    if len(llm.days) != request.days:
        raise ItineraryValidationError("day count does not match request")
    by_ref = {p.ref: p for p in candidates.places}
    max_stops = MAX_STOPS_BY_PACE[request.pace]
    plans: list[DayPlan] = []
    used = 0
    for day in llm.days:
        if len(day.place_refs) > max_stops:
            raise ItineraryValidationError(f"day {day.day} has too many stops for the pace")
        stops: list[DayStop] = []
        total = 0.0
        prev: Place | None = None
        for order, ref in enumerate(day.place_refs, start=1):
            place = by_ref.get(ref)
            if place is None:
                raise ItineraryValidationError(f"unknown place ref {ref}")
            leg: float | None = None
            if prev is not None:
                leg = round(prev.coords.distance_km(place.coords), 2)
                total = round(total + leg, 2)
            stops.append(DayStop(order=order, place=place, leg_distance_km=leg))
            prev = place
        used += len(stops)
        plans.append(
            DayPlan(day=day.day, theme=day.theme, stops=stops, total_distance_km=total)
        )
    return Itinerary(
        city=request.city,
        days=request.days,
        pace=request.pace,
        budget=request.budget,
        day_plans=plans,
        unused_places=len(candidates.places) - used,
    )