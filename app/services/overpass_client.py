import asyncio
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.schemas import (
    MAX_RADIUS_M,
    Address,
    Coordinates,
    GeocodeResult,
    Place,
    PlaceCandidates,
    PlaceCategory,
    PlaceName,
)

from app.utils.http_guard import (
    BodyTooLargeError,
    check_base_url,
    check_user_agent,
    read_capped,
)

from app.utils.throttle import Clock, Cooldown, KeyedLocks, Sleep, Spacer, TTLCache

logger = logging.getLogger(__name__)

OsmType = Literal["node", "way", "relation"]

DEFAULT_BASE_URL = "https://overpass-api.de/api"

# osm tag filters agreed in the design
TOURISM_VALUES = ("attraction", "museum", "gallery", "viewpoint", "zoo", "aquarium", "theme_park")
HISTORIC_VALUES = ("castle", "monument", "fort", "archaeological_site")
# places to stay are not sights, even with a historic tag
ACCOMMODATION_VALUES = (
    "alpine_hut",
    "apartment",
    "camp_pitch",
    "camp_site",
    "caravan_site",
    "chalet",
    "guest_house",
    "hostel",
    "hotel",
    "motel",
    "wilderness_hut",
)

QUERY_TIMEOUT_S = 60
QUERY_MAXSIZE_BYTES = 128 * 1024 * 1024
NOTABLE_ATTRACTIONS_CAP = 300
OTHER_ATTRACTIONS_CAP = 200
NOTABLE_RESTAURANTS_CAP = 100
# assumption: a restaurant with no website and no wiki link ranks near the bottom anyway
WEB_RESTAURANTS_CAP = 200
# query time plus up to 15 s server slot wait plus margin
TOTAL_TIMEOUT_S = 80.0
CONNECT_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 2_000_000
# assumption: no per-second limit in the policy, keep requests apart anyway
MIN_INTERVAL_S = 1.0
REFUSAL_COOLDOWN_S = 30.0
# assumption: pois change slowly
CACHE_TTL_S = 7 * 24 * 3600.0
EMPTY_TTL_S = 24 * 3600.0
MAX_CACHE_ENTRIES = 64
# about 110 m, nearby centers share one query
CENTER_DECIMALS = 3

# assumption: radius is a quarter of the city bbox diagonal, clamped
RADIUS_FACTOR = 0.25
MIN_RADIUS_M = 1500
MAX_SEARCH_RADIUS_M = 8000
DEFAULT_RADIUS_M = 5000

TOP_ATTRACTIONS = 20
TOP_RESTAURANTS = 10
# assumption: at most 30 percent of attraction slots for one kind, e.g. museums
MAX_PER_KIND = 6
# generic catch-all tag, says nothing about the kind of place
QUOTA_EXEMPT_KINDS = ("tourism=attraction",)
# same name within this distance is one place, e.g. node plus building way
DEDUPE_DISTANCE_M = 50.0

# assumption: score weights, notability beats completeness beats distance
NOTABILITY_KEYS = ("wikidata", "wikipedia")
NOTABILITY_WEIGHT = 3.0
DETAIL_KEYS = (
    "website",
    "contact:website",
    "opening_hours",
    "phone",
    "heritage",
    "image",
    "wikimedia_commons",
    "cuisine",
    "description",
)
DETAIL_WEIGHT = 0.5
DISTANCE_WEIGHT = 1.0
# calibration, bucharest: landmarks carry 16 to 19 name:xx tags, median attraction 0
# assumption: linear up to 20 languages, max bonus equals the notability weight
FAME_PER_LANGUAGE = 0.15
FAME_MAX_LANGUAGES = 20
_LANGUAGE_NAME = re.compile(r"^name:[a-z]{2,3}(-[A-Za-z]+)?$")

_name_adapter: TypeAdapter[str] = TypeAdapter(PlaceName)
_address_adapter: TypeAdapter[str] = TypeAdapter(Address)


class OverpassError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

class NoPlacesFoundError(OverpassError):
    pass

class OverpassUnavailableError(OverpassError):
    pass

class _Center(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lat: float
    lon: float


class _Element(BaseModel):
    # third-party payload, unknown fields are ignored on purpose
    model_config = ConfigDict(extra="ignore")

    type: OsmType
    id: int = Field(gt=0)
    lat: float | None = None
    lon: float | None = None
    center: _Center | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    elements: list[Any]
    remark: str | None = None


@dataclass(frozen=True)
class _Scored:
    score: float
    distance_km: float
    osm_type: OsmType
    osm_id: int
    name: str
    category: PlaceCategory
    kind: str
    coords: Coordinates
    address: str | None

    def sort_key(self) -> tuple[float, float, str, int]:
        # deterministic: score, then closer, then stable osm identity
        return (-self.score, self.distance_km, self.osm_type, self.osm_id)


def radius_for(geo: GeocodeResult) -> int:
    if geo.bbox is None:
        return DEFAULT_RADIUS_M
    sw = Coordinates(lat=geo.bbox.south, lon=geo.bbox.west)
    ne = Coordinates(lat=geo.bbox.north, lon=geo.bbox.east)
    diagonal_m = sw.distance_km(ne) * 1000
    radius = min(max(diagonal_m * RADIUS_FACTOR, MIN_RADIUS_M), MAX_SEARCH_RADIUS_M)
    return int(round(radius))


def build_query(center: Coordinates, radius_m: int) -> str:
    # only validated numbers are interpolated, never user text
    if not 0 < radius_m <= MAX_RADIUS_M:
        raise ValueError("radius_m out of range")
    around = f"(around:{radius_m},{center.lat:.6f},{center.lon:.6f})"
    tourism = "|".join(TOURISM_VALUES)
    historic = "|".join(HISTORIC_VALUES)
    return (
        f"[out:json][timeout:{QUERY_TIMEOUT_S}][maxsize:{QUERY_MAXSIZE_BYTES}];\n"
        "(\n"
        f'  nwr["tourism"~"^({tourism})$"]["name"]{around};\n'
        f'  nwr["historic"~"^({historic})$"]["name"]{around};\n'
        f'  nwr["leisure"="park"]["name"]{around};\n'
        ")->.a;\n"
        f'nwr["amenity"="restaurant"]["name"]{around}->.r;\n'
        '(nwr.a["wikidata"]; nwr.a["wikipedia"];)->.an;\n'
        "(.a; - .an;)->.ao;\n"
        '(nwr.r["wikidata"]; nwr.r["wikipedia"];)->.rn;\n'
        '(nwr.r["website"]; nwr.r["contact:website"];)->.rw;\n'
        "(.rw; - .rn;)->.rq;\n"
        f".an out tags center {NOTABLE_ATTRACTIONS_CAP};\n"
        f".ao out tags center {OTHER_ATTRACTIONS_CAP};\n"
        f".rn out tags center {NOTABLE_RESTAURANTS_CAP};\n"
        f".rq out tags center {WEB_RESTAURANTS_CAP};\n"
    )


def _classify(tags: dict[str, str]) -> tuple[PlaceCategory, str] | None:
    # kind is the tag that matched, used for the diversity quota
    tourism = tags.get("tourism")
    if tourism in ACCOMMODATION_VALUES:
        # a hotel restaurant is still a restaurant, the hotel is not a sight
        if tags.get("amenity") == "restaurant":
            return PlaceCategory.RESTAURANT, "amenity=restaurant"
        return None
    if tourism in TOURISM_VALUES:
        return PlaceCategory.ATTRACTION, f"tourism={tourism}"
    historic = tags.get("historic")
    if historic in HISTORIC_VALUES:
        return PlaceCategory.ATTRACTION, f"historic={historic}"
    if tags.get("leisure") == "park":
        return PlaceCategory.ATTRACTION, "leisure=park"
    if tags.get("amenity") == "restaurant":
        return PlaceCategory.RESTAURANT, "amenity=restaurant"
    return None


def _name(tags: dict[str, str]) -> str | None:
    # names are untrusted text, an unsafe name drops the place instead of being cut
    for key in ("name:en", "name"):
        raw = tags.get(key)
        if raw is None:
            continue
        try:
            return _name_adapter.validate_python(raw)
        except ValidationError:
            continue
    return None


def _address(tags: dict[str, str]) -> str | None:
    street = tags.get("addr:street")
    number = tags.get("addr:housenumber")
    if not street or not number:
        return None
    try:
        return _address_adapter.validate_python(f"{street} {number}")
    except ValidationError:
        return None


def _coords(element: _Element) -> Coordinates | None:
    if element.lat is not None and element.lon is not None:
        lat, lon = element.lat, element.lon
    elif element.center is not None:
        lat, lon = element.center.lat, element.center.lon
    else:
        return None
    try:
        return Coordinates(lat=lat, lon=lon)
    except ValidationError:
        return None


def _score(tags: dict[str, str], distance_km: float, radius_m: int) -> float:
    score = 0.0
    if any(key in tags for key in NOTABILITY_KEYS):
        score += NOTABILITY_WEIGHT
    score += DETAIL_WEIGHT * sum(1 for key in DETAIL_KEYS if key in tags)
    languages = sum(1 for key in tags if _LANGUAGE_NAME.match(key))
    score += FAME_PER_LANGUAGE * min(languages, FAME_MAX_LANGUAGES)
    score -= DISTANCE_WEIGHT * min(distance_km * 1000 / radius_m, 1.0)
    return round(score, 6)


def _dedupe(ranked: list[_Scored]) -> list[_Scored]:
    kept: list[_Scored] = []
    by_name: dict[str, list[_Scored]] = {}
    for item in ranked:
        same = by_name.setdefault(item.name.casefold(), [])
        if any(item.coords.distance_km(k.coords) * 1000 <= DEDUPE_DISTANCE_M for k in same):
            continue
        same.append(item)
        kept.append(item)
    return kept


def _pick_attractions(ranked: list[_Scored]) -> list[_Scored]:
    picked: list[_Scored] = []
    skipped: list[_Scored] = []
    per_kind: Counter[str] = Counter()
    for item in ranked:
        if len(picked) == TOP_ATTRACTIONS:
            break
        if item.kind not in QUOTA_EXEMPT_KINDS and per_kind[item.kind] >= MAX_PER_KIND:
            skipped.append(item)
            continue
        per_kind[item.kind] += 1
        picked.append(item)
    # the quota is a preference, free slots go back to the best skipped places
    picked += skipped[: TOP_ATTRACTIONS - len(picked)]
    return sorted(picked, key=_Scored.sort_key)


def parse_candidates(center: Coordinates, radius_m: int, body: bytes) -> PlaceCandidates:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise OverpassUnavailableError("overpass returned invalid json") from None
    try:
        response = _Response.model_validate(data)
    except ValidationError:
        raise OverpassUnavailableError("unexpected overpass response") from None
    if response.remark is not None:
        # a remark means a runtime problem, the data may be partial
        logger.warning("overpass returned a remark")
        raise OverpassUnavailableError("overpass reported a query problem")
    scored: list[_Scored] = []
    skipped = 0
    for raw in response.elements:
        try:
            element = _Element.model_validate(raw)
        except ValidationError:
            skipped += 1
            continue
        classified = _classify(element.tags)
        name = _name(element.tags)
        coords = _coords(element)
        if classified is None or name is None or coords is None:
            skipped += 1
            continue
        distance_km = center.distance_km(coords)
        scored.append(
            _Scored(
                score=_score(element.tags, distance_km, radius_m),
                distance_km=distance_km,
                osm_type=element.type,
                osm_id=element.id,
                name=name,
                category=classified[0],
                kind=classified[1],
                coords=coords,
                address=_address(element.tags),
            )
        )
    if skipped:
        logger.info("overpass skipped %d unusable elements", skipped)
    # also drops an element listed twice, it has the same name at 0 m
    ranked = _dedupe(sorted(scored, key=_Scored.sort_key))
    attractions = _pick_attractions([s for s in ranked if s.category is PlaceCategory.ATTRACTION])
    restaurants = [s for s in ranked if s.category is PlaceCategory.RESTAURANT][:TOP_RESTAURANTS]
    chosen = attractions + restaurants
    if not chosen:
        raise NoPlacesFoundError("no places found near this city")
    places = [
        Place(
            ref=f"p{index}",
            osm_type=item.osm_type,
            osm_id=item.osm_id,
            name=item.name,
            category=item.category,
            coords=item.coords,
            address=item.address,
        )
        for index, item in enumerate(chosen, start=1)
    ]
    return PlaceCandidates(center=center, radius_m=radius_m, places=places)


class OverpassClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        min_interval_s: float = MIN_INTERVAL_S,
        cache_ttl_s: float = CACHE_TTL_S,
        empty_ttl_s: float = EMPTY_TTL_S,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
        total_timeout_s: float = TOTAL_TIMEOUT_S,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if cache_ttl_s <= 0 or empty_ttl_s <= 0:
            raise ValueError("cache ttls must be positive")
        if total_timeout_s <= 0:
            raise ValueError("total_timeout_s must be positive")
        self._client = client
        self._user_agent = check_user_agent(user_agent)
        self._url = check_base_url(base_url) + "/interpreter"
        self._cache_ttl_s = cache_ttl_s
        self._empty_ttl_s = empty_ttl_s
        self._total_timeout_s = total_timeout_s
        self._http_timeout = httpx.Timeout(
            total_timeout_s, connect=min(CONNECT_TIMEOUT_S, total_timeout_s)
        )
        self._spacer = Spacer(min_interval_s, clock, sleep)
        self._cooldown = Cooldown(clock)
        # policy: no parallel requests, one in flight at a time
        self._request_lock = asyncio.Lock()
        # none means a cached empty area
        self._cache: TTLCache[tuple[float, float, int], PlaceCandidates | None] = TTLCache(
            max_cache_entries, clock
        )
        self._locks: KeyedLocks[tuple[float, float, int]] = KeyedLocks()

    async def fetch_candidates(self, geo: GeocodeResult) -> PlaceCandidates:
        radius = radius_for(geo)
        center = Coordinates(
            lat=round(geo.center.lat, CENTER_DECIMALS),
            lon=round(geo.center.lon, CENTER_DECIMALS),
        )
        key = (center.lat, center.lon, radius)
        found, cached = self._cache.get(key)
        if found:
            return _answer(cached)
        async with self._locks.hold(key) as slot:
            # concurrent callers share the outcome of the first fetch
            if slot.error is not None:
                raise OverpassUnavailableError(slot.error)
            found, cached = self._cache.get(key)
            if found:
                return _answer(cached)
            try:
                result = await self._fetch(center, radius)
            except NoPlacesFoundError:
                self._cache.put(key, None, self._empty_ttl_s)
                raise
            except OverpassUnavailableError as exc:
                slot.error = exc.message
                raise
            self._cache.put(key, result, self._cache_ttl_s)
            return _answer(result)

    async def _fetch(self, center: Coordinates, radius_m: int) -> PlaceCandidates:
        query = build_query(center, radius_m)
        async with self._request_lock:
            # checked after waiting too, a refusal may have happened meanwhile
            if self._cooldown.active:
                raise OverpassUnavailableError("overpass is cooling down after a refusal")
            await self._spacer.wait()
            try:
                async with asyncio.timeout(self._total_timeout_s):
                    body = await self._post(query)
            except (TimeoutError, httpx.TimeoutException) as exc:
                logger.warning("overpass timed out")
                raise OverpassUnavailableError("overpass timed out") from exc
            except httpx.HTTPError as exc:
                logger.warning("overpass request failed: %s", type(exc).__name__)
                raise OverpassUnavailableError("overpass request failed") from exc
        return parse_candidates(center, radius_m, body)

    async def _post(self, query: str) -> bytes:
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        async with self._client.stream(
            "POST",
            self._url,
            data={"data": query},
            headers=headers,
            timeout=self._http_timeout,
            follow_redirects=False,
        ) as response:
            status = response.status_code
            if status in (406, 429):
                self._cooldown.trip(REFUSAL_COOLDOWN_S)
                logger.warning("overpass refused the request, http %s", status)
                raise OverpassUnavailableError("overpass refused the request, try again later")
            if status == 504:
                logger.warning("overpass is busy, http 504")
                raise OverpassUnavailableError("overpass is busy, try again later")
            if status != 200:
                logger.warning("overpass returned http %s", status)
                raise OverpassUnavailableError(f"overpass returned http {status}")
            try:
                return await read_capped(response, MAX_BODY_BYTES)
            except BodyTooLargeError:
                raise OverpassUnavailableError("overpass response too large") from None


def _answer(result: PlaceCandidates | None) -> PlaceCandidates:
    if result is None:
        raise NoPlacesFoundError("no places found near this city")
    # copy so callers cannot change the cached list
    return result.model_copy(deep=True)