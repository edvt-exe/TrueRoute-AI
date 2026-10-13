import asyncio
import inspect
import json
import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.schemas import BoundingBox, Coordinates, GeocodeResult, PlaceCategory
import app.services.overpass_client as oc
from app.services.overpass_client import (
    CACHE_TTL_S,
    FAME_MAX_LANGUAGES,
    FAME_PER_LANGUAGE,
    DEFAULT_RADIUS_M,
    EMPTY_TTL_S,
    MAX_BODY_BYTES,
    MAX_PER_KIND,
    MAX_SEARCH_RADIUS_M,
    MIN_RADIUS_M,
    QUERY_TIMEOUT_S,
    QUOTA_EXEMPT_KINDS,
    REFUSAL_COOLDOWN_S,
    TOP_ATTRACTIONS,
    TOP_RESTAURANTS,
    TOTAL_TIMEOUT_S,
    NoPlacesFoundError,
    OverpassClient,
    OverpassError,
    OverpassUnavailableError,
    MIN_NAME_WORD,
    SAME_BUILDING_DISTANCE_M,
    _classify,
    _name_words,
    _score,
    build_query,
    parse_candidates,
    radius_for,
)

UA = "TrueRoute-AI-tests/0.1 (+https://example.org/trueroute)"
MARKER = "SECRET-MARKER"

# bbox from a real nominatim response for bucharest
GEO = GeocodeResult(
    query="Bucharest",
    display_name="Bucharest, Romania",
    center=Coordinates(lat=44.4361414, lon=26.102684),
    bbox=BoundingBox(south=44.3342466, west=25.9666745, north=44.5413964, east=26.2255768),
)
CENTER = Coordinates(lat=44.436, lon=26.103)
RADIUS = 7718


def el(osm_type: str, osm_id: int, lat: float, lon: float, **tags: str) -> dict[str, Any]:
    # nodes carry lat and lon, ways and relations carry a center
    tags = {k.replace("__", ":"): v for k, v in tags.items()}
    if osm_type == "node":
        return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags}
    return {"type": osm_type, "id": osm_id, "center": {"lat": lat, "lon": lon}, "tags": tags}


def museum(osm_id: int, name: str, dlat: float = 0.0, **tags: str) -> dict[str, Any]:
    return el("node", osm_id, CENTER.lat + dlat, CENTER.lon, tourism="museum", name=name, **tags)


def restaurant(osm_id: int, name: str, dlat: float = 0.0, **tags: str) -> dict[str, Any]:
    return el("node", osm_id, CENTER.lat + dlat, CENTER.lon, amenity="restaurant", name=name, **tags)


def payload(elements: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"version": 0.6, "generator": "Overpass API", "osm3s": {}, "elements": elements} | extra


def parse(elements: list[dict[str, Any]], **extra: Any) -> Any:
    return parse_candidates(CENTER, RADIUS, json.dumps(payload(elements, **extra)).encode())


# query and radius


def test_build_query_text() -> None:
    q = build_query(CENTER, RADIUS)
    around = "(around:7718,44.436000,26.103000)"
    assert q.startswith("[out:json][timeout:60][maxsize:134217728];")
    assert f'nwr["tourism"~"^(attraction|museum|gallery|viewpoint|zoo|aquarium|theme_park)$"]["name"]{around};' in q
    assert f'nwr["historic"~"^(castle|monument|fort|archaeological_site)$"]["name"]{around};' in q
    assert f'nwr["leisure"="park"]["name"]{around};' in q
    assert f'nwr["amenity"="restaurant"]["name"]{around}->.r0;' in q
    assert ")->.a;" in q
    # a place tagged as attraction and restaurant is only in the attraction sets
    assert "(.r0; - .a;)->.r;" in q
    # notable places get their own capped output so big ways and relations survive
    assert '(nwr.a["wikidata"]; nwr.a["wikipedia"];)->.an;' in q
    # one notable batch per element type, nodes cannot crowd out buildings
    assert "node.an->.ann;" in q
    assert "way.an->.anw;" in q
    assert "relation.an->.anr;" in q
    assert "(.a; - .an;)->.ao;" in q
    assert '(nwr.r["wikidata"]; nwr.r["wikipedia"];)->.rn;' in q
    assert '(nwr.r["website"]; nwr.r["contact:website"];)->.rw;' in q
    assert "(.rw; - .rn;)->.rq;" in q
    outs = [line for line in q.splitlines() if " out " in line]
    assert outs == [
        ".ann out tags center 150;",
        ".anw out tags center 400;",
        ".anr out tags center 150;",
        ".ao out tags center 200;",
        ".rn out tags center 100;",
        ".rq out tags center 200;",
    ]
    assert ".r out" not in q and ".an out" not in q and ".r0 out" not in q
    for excluded in ("memorial", "artwork", "cafe"):
        assert excluded not in q


@pytest.mark.parametrize("radius", [0, -1, 50_001])
def test_build_query_rejects_bad_radius(radius: int) -> None:
    with pytest.raises(ValueError):
        build_query(CENTER, radius)


def test_radius_bucharest() -> None:
    assert radius_for(GEO) == RADIUS


def test_radius_without_bbox() -> None:
    assert radius_for(GEO.model_copy(update={"bbox": None})) == DEFAULT_RADIUS_M


def test_radius_clamped() -> None:
    tiny = BoundingBox(south=44.0, west=26.0, north=44.001, east=26.001)
    huge = BoundingBox(south=40.0, west=20.0, north=48.0, east=30.0)
    assert radius_for(GEO.model_copy(update={"bbox": tiny})) == MIN_RADIUS_M
    assert radius_for(GEO.model_copy(update={"bbox": huge})) == MAX_SEARCH_RADIUS_M


def test_radius_across_antimeridian() -> None:
    bbox = BoundingBox(south=-17.0, west=179.99, north=-16.99, east=-179.99)
    assert radius_for(GEO.model_copy(update={"bbox": bbox})) == MIN_RADIUS_M


# parsing


def test_coords_from_node_way_and_relation() -> None:
    res = parse(
        [
            el("node", 1, 44.40, 26.10, tourism="museum", name="A"),
            el("way", 2, 44.41, 26.11, leisure="park", name="B"),
            el("relation", 3, 44.42, 26.12, historic="castle", name="C"),
        ]
    )
    by_name = {p.name: p for p in res.places}
    assert (by_name["A"].osm_type, by_name["A"].coords.lat) == ("node", 44.40)
    assert (by_name["B"].osm_type, by_name["B"].coords.lon) == ("way", 26.11)
    assert (by_name["C"].osm_type, by_name["C"].coords.lat) == ("relation", 44.42)
    assert res.center == CENTER
    assert res.radius_m == RADIUS


def test_unusable_elements_skipped() -> None:
    good = museum(1, "Good")
    res = parse(
        [
            {"type": "node", "id": 2, "tags": {"tourism": "museum", "name": "No coords"}},
            {"type": "area", "id": 3, "lat": 44.4, "lon": 26.1, "tags": {"tourism": "museum", "name": "Area"}},
            {"type": "node", "id": 0, "lat": 44.4, "lon": 26.1, "tags": {"tourism": "museum", "name": "Zero"}},
            {"type": "node", "id": 4, "lat": 91, "lon": 26.1, "tags": {"tourism": "museum", "name": "Bad lat"}},
            {"type": "node", "id": 5, "lat": 44.4, "lon": 26.1, "tags": {"tourism": "museum", "name": 7}},
            {"type": "way", "id": 6, "center": {"lat": "x"}, "tags": {"tourism": "museum", "name": "Bad center"}},
            "not an object",
            el("node", 7, 44.4, 26.1, historic="memorial", name="Plaque"),
            el("node", 8, 44.4, 26.1, amenity="cafe", name="Cafe"),
            el("node", 9, 44.4, 26.1, tourism="museum"),
            good,
        ]
    )
    assert [p.name for p in res.places] == ["Good"]


def test_name_choice_and_safety() -> None:
    res = parse(
        [
            museum(1, "Muzeul Național", name__en="National Museum"),
            museum(2, "Ateneul Român", 0.001, name__en="bad\nname"),
            museum(3, "x" * 201, 0.002),
            museum(4, "bad\x00name", 0.003),
            museum(5, "   ", 0.004),
        ]
    )
    assert [p.name for p in res.places] == ["National Museum", "Ateneul Român"]


def test_categories() -> None:
    res = parse(
        [
            museum(1, "Museum"),
            el("node", 2, 44.436, 26.103, leisure="park", name="Park"),
            el("node", 3, 44.436, 26.103, historic="fort", name="Fort"),
            restaurant(4, "Restaurant"),
            el("node", 5, 44.436, 26.103, tourism="attraction", amenity="restaurant", name="Both"),
        ]
    )
    cats = {p.name: p.category for p in res.places}
    assert cats == {
        "Museum": PlaceCategory.ATTRACTION,
        "Park": PlaceCategory.ATTRACTION,
        "Fort": PlaceCategory.ATTRACTION,
        "Restaurant": PlaceCategory.RESTAURANT,
        "Both": PlaceCategory.ATTRACTION,
    }


def test_same_element_twice_counted_once() -> None:
    both = el("node", 1, 44.436, 26.103, tourism="attraction", amenity="restaurant", name="X")
    res = parse([both, both])
    assert len(res.places) == 1


def test_address() -> None:
    res = parse(
        [
            museum(1, "Full", addr__street="Strada Lipscani", addr__housenumber="5"),
            museum(2, "No number", 0.001, addr__street="Strada Lipscani"),
            museum(3, "Unsafe", 0.002, addr__street="Strada\x00", addr__housenumber="5"),
        ]
    )
    addresses = {p.name: p.address for p in res.places}
    assert addresses == {"Full": "Strada Lipscani 5", "No number": None, "Unsafe": None}


# ranking


def test_score_weights() -> None:
    tags = {"wikidata": "Q1", "website": "w", "cuisine": "c", "name": "n"}
    assert _score(tags, 0.0, 1000) == pytest.approx(4.0)
    assert _score(tags, 1.0, 1000) == pytest.approx(3.0)
    assert _score(tags, 5.0, 1000) == pytest.approx(3.0)
    assert _score({"wikipedia": "en:X"}, 0.5, 1000) == pytest.approx(2.5)
    assert _score({}, 0.0, 1000) == 0.0
    assert _score({k: "v" for k in ("opening_hours", "phone", "heritage", "image")}, 0.0, 1000) == pytest.approx(2.0)


def test_fame_signal() -> None:
    langs = {f"name:{code}": "x" for code in ("de", "fr", "es", "it", "ru")}
    assert _score(langs, 0.0, 1000) == pytest.approx(0.75)
    many = {f"name:{chr(97 + i // 26)}{chr(97 + i % 26)}": "x" for i in range(30)}
    assert _score(many, 0.0, 1000) == pytest.approx(3.0)
    # variants with a region or script suffix count, other name keys do not
    tags = {"name": "x", "name:zh-Hans": "x", "name:pt-BR": "x", "old_name": "x", "name:etymology": "x", "official_name:en": "x"}
    assert _score(tags, 0.0, 1000) == pytest.approx(0.3)


def test_fame_lifts_landmark_over_detailed_museum() -> None:
    detail = {"website": "w", "opening_hours": "o", "phone": "p", "image": "i", "wikidata": "Q"}
    langs = {f"name:{chr(97 + i // 26)}{chr(97 + i % 26)}": "x" for i in range(18)}
    res = parse(
        [
            museum(1, "Small museum", **detail),
            el("relation", 2, CENTER.lat + 0.01, CENTER.lon, tourism="attraction", name="Palace", wikidata="Q", **langs),
        ]
    )
    assert [p.name for p in res.places] == ["Palace", "Small museum"]


def test_rank_order() -> None:
    res = parse(
        [
            museum(1, "Plain near"),
            museum(2, "Detailed", 0.01, website="w", opening_hours="Mo-Su", phone="1"),
            museum(3, "Notable far", 0.05, wikidata="Q1"),
            museum(4, "Plain far", 0.02),
        ]
    )
    assert [p.name for p in res.places] == ["Notable far", "Detailed", "Plain near", "Plain far"]
    assert [p.ref for p in res.places] == ["p1", "p2", "p3", "p4"]


def test_rank_tie_breaks_by_osm_identity() -> None:
    res = parse([museum(9, "B"), museum(3, "A"), el("way", 1, CENTER.lat, CENTER.lon, tourism="museum", name="C")])
    assert [(p.osm_type, p.osm_id) for p in res.places] == [("node", 3), ("node", 9), ("way", 1)]


def test_rank_tie_breaks_by_distance_beyond_radius() -> None:
    # way centers can fall outside the circle, the penalty is capped there
    res = parse([museum(1, "Far", 0.2), museum(2, "Less far", 0.1)])
    assert [p.name for p in res.places] == ["Less far", "Far"]


def test_dedupe() -> None:
    res = parse(
        [
            museum(1, "Palace", 0.0),
            el("way", 2, CENTER.lat + 0.0003, CENTER.lon, tourism="museum", name="palace", wikidata="Q1"),
            museum(3, "Palace", 0.002),
            museum(4, "Other", 0.0001),
        ]
    )
    # 0.0003 deg is about 33 m, 0.002 deg is about 220 m
    names = [(p.name, p.osm_type) for p in res.places]
    assert names == [("palace", "way"), ("Other", "node"), ("Palace", "node")]
    assert [p.osm_id for p in res.places if p.name == "Palace"] == [3]


# same building in two categories


def at(dlat_m: float, dlon_m: float = 0.0) -> tuple[float, float]:
    # metres to degrees near bucharest, about 111 km per degree of latitude
    return CENTER.lat + dlat_m / 111_195, CENTER.lon + dlon_m / 79_400


def attraction(osm_id: int, name: str, north_m: float = 0.0, **tags: str) -> dict[str, Any]:
    lat, lon = at(north_m)
    return el("relation", osm_id, lat, lon, tourism="attraction", name=name, wikidata="Q", **tags)


def eatery(osm_id: int, name: str, north_m: float = 0.0, **tags: str) -> dict[str, Any]:
    lat, lon = at(north_m)
    return el("node", osm_id, lat, lon, amenity="restaurant", name=name, **tags)


def names(res: Any) -> list[str]:
    return [p.name for p in res.places]


def test_restaurant_inside_attraction_is_dropped() -> None:
    # live data: Hanul Manuc and Manuc's Inn are about 30 m apart
    res = parse([attraction(1, "Hanul Manuc"), eatery(2, "Manuc's Inn", 30), eatery(3, "Caru' cu Bere", 60)])
    assert names(res) == ["Hanul Manuc", "Caru' cu Bere"]


def test_freed_restaurant_slot_goes_to_the_next_one() -> None:
    eateries = [eatery(100 + i, f"Bistro Number{i}", 200 + i) for i in range(1, 12)]
    res = parse([attraction(1, "Hanul Manuc"), eatery(2, "Manuc's Inn", 10, website="w"), *eateries])
    restaurants = [p.name for p in res.places if p.category is PlaceCategory.RESTAURANT]
    assert "Manuc's Inn" not in restaurants
    assert len(restaurants) == TOP_RESTAURANTS


def test_close_restaurant_with_other_name_stays() -> None:
    # Caru' cu Bere stands next to Stavropoleos but is its own place
    res = parse([attraction(1, "Stavropoleos Monastery"), eatery(2, "Caru' cu Bere", 20)])
    assert names(res) == ["Stavropoleos Monastery", "Caru' cu Bere"]


def test_same_word_far_away_stays() -> None:
    res = parse([attraction(1, "Hanul Manuc"), eatery(2, "Manuc Grill", 51)])
    assert names(res) == ["Hanul Manuc", "Manuc Grill"]


def test_distance_limit_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    places = [attraction(1, "Hanul Manuc"), eatery(2, "Manuc's Inn", 40)]
    a, b = (Coordinates(lat=p["center"]["lat"] if "center" in p else p["lat"], lon=p["center"]["lon"] if "center" in p else p["lon"]) for p in places)
    exact = a.distance_km(b) * 1000
    monkeypatch.setattr("app.services.overpass_client.SAME_BUILDING_DISTANCE_M", exact)
    assert names(parse(places)) == ["Hanul Manuc"]
    monkeypatch.setattr("app.services.overpass_client.SAME_BUILDING_DISTANCE_M", exact - 1e-6)
    assert names(parse(places)) == ["Hanul Manuc", "Manuc's Inn"]


def test_generic_words_do_not_match() -> None:
    res = parse(
        [
            attraction(1, "Muzeul National de Arta"),
            eatery(2, "Muzeul Restaurant National", 10),
            attraction(3, "Grand Palace Park", 200),
            eatery(4, "Grand Park House Bistro", 210),
            attraction(5, "Manastirea Stavropoleos Monastery Church", 400),
            eatery(6, "Monastery Kitchen", 410),
        ]
    )
    assert len(res.places) == 6


def test_short_words_do_not_match() -> None:
    res = parse([attraction(1, "Arc de Triumf"), eatery(2, "Arc Bar", 10)])
    assert names(res) == ["Arc de Triumf", "Arc Bar"]


def test_diacritics_and_case_are_ignored() -> None:
    res = parse([attraction(1, "Palatul Brăgădiru"), eatery(2, "BRAGADIRU Brewery", 20)])
    assert names(res) == ["Palatul Brăgădiru"]


def test_only_chosen_attractions_count() -> None:
    # the matching attraction is not in the top 20, so the restaurant stays
    top = [attraction(10 + i, f"Landmark Number{i}", 500 + i, website="w", phone="p") for i in range(1, 21)]
    weak = el("node", 99, CENTER.lat, CENTER.lon, tourism="museum", name="Hanul Manuc")
    res = parse([*top, weak, eatery(2, "Manuc's Inn", 10)])
    assert "Hanul Manuc" not in names(res)
    assert "Manuc's Inn" in names(res)


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("Hanul Manuc", {"manuc"}),
        ("Manuc's Inn", {"manuc"}),
        ("Caru' cu Bere", {"caru", "bere"}),
        ("Muzeul Național de Artă", {"arta"}),
        ("Stavropoleos Monastery", {"stavropoleos"}),
        ("Mănăstirea Stavropoleos", {"stavropoleos"}),
        ("Grand Park", set()),
    ],
)
def test_name_words(name: str, words: set[str]) -> None:
    assert _name_words(name) == words


def test_same_building_limits() -> None:
    assert (SAME_BUILDING_DISTANCE_M, MIN_NAME_WORD) == (50, 4)


def test_top_caps_and_ref_order() -> None:
    attractions = [museum(i, f"M{i}", i * 0.0001) for i in range(1, 31)]
    restaurants = [restaurant(100 + i, f"R{i}", i * 0.0001) for i in range(1, 16)]
    res = parse(restaurants + attractions)
    cats = [p.category for p in res.places]
    assert cats == [PlaceCategory.ATTRACTION] * TOP_ATTRACTIONS + [PlaceCategory.RESTAURANT] * TOP_RESTAURANTS
    assert [p.ref for p in res.places] == [f"p{i}" for i in range(1, 31)]
    assert res.places[0].name == "M1"
    assert res.places[20].name == "R1"


def test_diversity_quota() -> None:
    museums = [museum(i, f"M{i}", i * 0.0001, wikidata="Q") for i in range(1, 16)]
    parks = [el("way", 100 + i, CENTER.lat + i * 0.001, CENTER.lon, leisure="park", name=f"P{i}") for i in range(1, 15)]
    res = parse(museums + parks)
    names = [p.name for p in res.places]
    # 6 museums rank first, then parks fill the rest up to the quota and beyond via backfill
    assert sum(n.startswith("M") for n in names) == 6 + 8
    assert names[:6] == [f"M{i}" for i in range(1, 7)]
    assert [n for n in names if n.startswith("P")] == [f"P{i}" for i in range(1, 7)]
    # backfilled museums are merged back into rank order, not appended
    assert names == [f"M{i}" for i in range(1, 15)] + [f"P{i}" for i in range(1, 7)]
    assert [p.ref for p in res.places] == [f"p{i}" for i in range(1, 21)]


def test_attraction_cap_with_many_kinds() -> None:
    kinds = [("tourism", "museum"), ("tourism", "gallery"), ("historic", "castle"), ("leisure", "park"), ("tourism", "zoo")]
    places = [
        el("node", 10 * k + i, CENTER.lat + (5 * k + i) * 0.0001, CENTER.lon, **{key: value}, name=f"{value}{i}")
        for k, (key, value) in enumerate(kinds)
        for i in range(1, 6)
    ]
    res = parse(places)
    assert len(res.places) == TOP_ATTRACTIONS


def test_diversity_quota_lets_other_kinds_in() -> None:
    museums = [museum(i, f"M{i}", i * 0.0001, wikidata="Q") for i in range(1, 25)]
    castles = [el("node", 200 + i, CENTER.lat, CENTER.lon, historic="castle", name=f"C{i}") for i in range(1, 4)]
    names = [p.name for p in parse(museums + castles).places]
    assert set(names) >= {"C1", "C2", "C3"}
    assert names[:6] == [f"M{i}" for i in range(1, 7)]


def test_generic_attraction_kind_is_exempt_from_quota() -> None:
    sights = [el("node", i, CENTER.lat + i * 0.0001, CENTER.lon, tourism="attraction", name=f"A{i}", wikidata="Q") for i in range(1, 13)]
    museums = [museum(50 + i, f"M{i}", 0.002 + i * 0.0001, website="w") for i in range(1, 15)]
    parks = [el("way", 100 + i, CENTER.lat + i * 0.001, CENTER.lon, leisure="park", name=f"P{i}") for i in range(1, 15)]
    names = [p.name for p in parse(sights + museums + parks).places]
    # all 12 generic sights fit, other kinds fill the rest within their quota
    assert names == [f"A{i}" for i in range(1, 13)] + [f"M{i}" for i in range(1, 7)] + ["P1", "P2"]


def test_quota_backfill_keeps_rank_order() -> None:
    museums = [museum(i, f"M{i}", i * 0.0001) for i in range(1, 10)]
    names = [p.name for p in parse(museums).places]
    assert names == [f"M{i}" for i in range(1, 10)]


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"tourism": "museum"}, (PlaceCategory.ATTRACTION, "tourism=museum")),
        ({"tourism": "hotel", "historic": "monument"}, None),
        ({"tourism": "guest_house", "leisure": "park"}, None),
        ({"tourism": "hostel"}, None),
        ({"tourism": "hotel", "amenity": "restaurant"}, (PlaceCategory.RESTAURANT, "amenity=restaurant")),
        ({"tourism": "museum", "historic": "castle"}, (PlaceCategory.ATTRACTION, "tourism=museum")),
        ({"historic": "castle", "leisure": "park"}, (PlaceCategory.ATTRACTION, "historic=castle")),
        ({"leisure": "park", "amenity": "restaurant"}, (PlaceCategory.ATTRACTION, "leisure=park")),
        ({"amenity": "restaurant"}, (PlaceCategory.RESTAURANT, "amenity=restaurant")),
        ({"amenity": "cafe"}, None),
        ({"historic": "memorial"}, None),
        ({}, None),
    ],
)
def test_classify(tags: dict[str, str], expected: Any) -> None:
    assert _classify(tags) == expected


def test_no_backfill_when_few_restaurants() -> None:
    res = parse([museum(i, f"M{i}", i * 0.0001) for i in range(1, 31)] + [restaurant(200, "R")])
    assert len(res.places) == TOP_ATTRACTIONS + 1


def test_spec_limits() -> None:
    # values agreed in the design, change only on purpose
    assert (TOP_ATTRACTIONS, TOP_RESTAURANTS) == (20, 10)
    assert REFUSAL_COOLDOWN_S == 30
    assert CACHE_TTL_S == 7 * 24 * 3600
    assert EMPTY_TTL_S == 24 * 3600
    assert (MIN_RADIUS_M, MAX_SEARCH_RADIUS_M, DEFAULT_RADIUS_M) == (1500, 8000, 5000)
    assert MAX_BODY_BYTES == 2_000_000
    assert MAX_PER_KIND == 6
    assert QUOTA_EXEMPT_KINDS == ("tourism=attraction",)
    assert (FAME_PER_LANGUAGE, FAME_MAX_LANGUAGES) == (0.15, 20)
    assert (QUERY_TIMEOUT_S, TOTAL_TIMEOUT_S) == (60, 80)


@pytest.mark.parametrize(
    "elements",
    [[], [el("node", 1, 44.4, 26.1, amenity="cafe", name="Cafe")]],
    ids=["empty", "nothing_usable"],
)
def test_no_places(elements: list[dict[str, Any]]) -> None:
    with pytest.raises(NoPlacesFoundError):
        parse(elements)


BAD_BODIES = [
    pytest.param(b"<html>" + MARKER.encode() + b"</html>", id="html"),
    pytest.param(b"", id="empty"),
    pytest.param(b"\xff\xfe", id="not_utf8"),
    pytest.param(b"[" * 50_000 + b"]" * 50_000, id="deep_nesting"),
    pytest.param(b'["' + MARKER.encode() + b'"]', id="list"),
    pytest.param(b'{"elements": "' + MARKER.encode() + b'"}', id="elements_not_list"),
    pytest.param(b'{"version": 0.6}', id="no_elements"),
    pytest.param(
        json.dumps(payload([museum(1, "A")], remark="runtime error: " + MARKER)).encode(),
        id="remark",
    ),
]


@pytest.mark.parametrize("body", BAD_BODIES)
def test_bad_body(body: bytes, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(OverpassUnavailableError) as info:
        parse_candidates(CENTER, RADIUS, body)
    exc = info.value
    assert MARKER not in str(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__
    assert MARKER not in "".join(traceback.format_exception(exc))
    assert MARKER not in caplog.text


# client harness


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
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # yield a few times so concurrent callers really overlap
            for _ in range(5):
                await asyncio.sleep(0)
            result = self.responder(request)
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            self.in_flight -= 1

    @property
    def calls(self) -> int:
        return len(self.requests)


Scenario = Callable[[OverpassClient, FakeClock], Awaitable[Any]]


def run(responder: Responder, scenario: Scenario, **kwargs: Any) -> tuple[Any, Recorder, FakeClock]:
    clock = FakeClock()
    rec = Recorder(responder)

    async def main() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
            ovp = OverpassClient(client, UA, clock=clock, sleep=clock.sleep, **kwargs)
            return await scenario(ovp, clock)

    return asyncio.run(main()), rec, clock


def ok(elements: list[dict[str, Any]] | None = None) -> Responder:
    body = payload([museum(1, "A"), restaurant(2, "R")] if elements is None else elements)
    return lambda request: httpx.Response(200, json=body)


def status(code: int, body: str = "") -> Responder:
    return lambda request: httpx.Response(code, text=body)


def fetch(geo: GeocodeResult = GEO) -> Scenario:
    async def scenario(ovp: OverpassClient, clock: FakeClock) -> Any:
        return await ovp.fetch_candidates(geo)

    return scenario


def expect_error(scenario: Scenario) -> Scenario:
    async def wrapped(ovp: OverpassClient, clock: FakeClock) -> Any:
        try:
            await scenario(ovp, clock)
        except OverpassError as exc:
            return exc
        raise AssertionError("expected an OverpassError")

    return wrapped


def other_geo(dlat: float) -> GeocodeResult:
    center = Coordinates(lat=GEO.center.lat + dlat, lon=GEO.center.lon)
    return GEO.model_copy(update={"center": center})


# client: request and result


def test_request_shape() -> None:
    res, rec, _ = run(ok(), fetch())
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://overpass-api.de/api/interpreter"
    assert req.headers["user-agent"] == UA
    assert req.headers["accept"] == "application/json"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(req.content.decode()) == {"data": [build_query(CENTER, RADIUS)]}
    assert [p.name for p in res.places] == ["A", "R"]
    assert (res.center, res.radius_m) == (CENTER, RADIUS)


def test_custom_base_url() -> None:
    _, rec, _ = run(ok(), fetch(), base_url="https://overpass.example.org/api/")
    assert str(rec.requests[0].url) == "https://overpass.example.org/api/interpreter"


# client: errors, no retries


@pytest.mark.parametrize("code", [406, 429])
def test_refusal_cools_down(code: int) -> None:
    async def scenario(ovp: OverpassClient, clock: FakeClock) -> list[str]:
        messages = []
        for _ in range(2):
            with pytest.raises(OverpassUnavailableError) as info:
                await ovp.fetch_candidates(GEO)
            messages.append(info.value.message)
        clock.advance(REFUSAL_COOLDOWN_S - 1)
        with pytest.raises(OverpassUnavailableError, match="cooling down"):
            await ovp.fetch_candidates(other_geo(0.1))
        return messages

    messages, rec, _ = run(status(code, MARKER), scenario)
    assert rec.calls == 1
    assert "refused" in messages[0]
    assert "cooling down" in messages[1]
    assert all(MARKER not in m for m in messages)


def test_cooldown_ends() -> None:
    responses = iter([httpx.Response(429), httpx.Response(200, json=payload([museum(1, "A")]))])

    async def scenario(ovp: OverpassClient, clock: FakeClock) -> Any:
        with pytest.raises(OverpassUnavailableError):
            await ovp.fetch_candidates(GEO)
        clock.advance(REFUSAL_COOLDOWN_S)
        return await ovp.fetch_candidates(GEO)

    res, rec, _ = run(lambda request: next(responses), scenario)
    assert rec.calls == 2
    assert res.places[0].name == "A"


def test_busy_has_no_cooldown() -> None:
    responses = iter([httpx.Response(504), httpx.Response(200, json=payload([museum(1, "A")]))])

    async def scenario(ovp: OverpassClient, clock: FakeClock) -> Any:
        with pytest.raises(OverpassUnavailableError, match="busy"):
            await ovp.fetch_candidates(GEO)
        return await ovp.fetch_candidates(GEO)

    _, rec, _ = run(lambda request: next(responses), scenario)
    assert rec.calls == 2


@pytest.mark.parametrize("code", [400, 404, 500, 502, 302])
def test_other_status(code: int) -> None:
    exc, rec, _ = run(status(code, MARKER), expect_error(fetch()))
    assert isinstance(exc, OverpassUnavailableError)
    assert exc.message == f"overpass returned http {code}"
    assert MARKER not in str(exc)
    assert rec.calls == 1


def test_redirect_not_followed() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.org/"})

    exc, rec, _ = run(responder, expect_error(fetch()))
    assert exc.message == "overpass returned http 302"
    assert rec.calls == 1


def test_timeouts_and_transport_errors() -> None:
    def read_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    exc, rec, _ = run(read_timeout, expect_error(fetch()))
    assert (exc.message, rec.calls) == ("overpass timed out", 1)
    exc, rec, _ = run(connect_error, expect_error(fetch()))
    assert (exc.message, rec.calls) == ("overpass request failed", 1)


def test_total_timeout_bounds_slow_server() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=payload([museum(1, "A")]))

    async def scenario(ovp: OverpassClient, clock: FakeClock) -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(OverpassUnavailableError, match="timed out"):
            await ovp.fetch_candidates(GEO)
        return loop.time() - start

    elapsed, _, _ = run(slow, scenario, total_timeout_s=0.05)
    assert elapsed < 1.0


def test_body_too_large() -> None:
    exc, _, _ = run(lambda r: httpx.Response(200, content=b" " * (MAX_BODY_BYTES + 1)), expect_error(fetch()))
    assert exc.message == "overpass response too large"


def test_remark_through_client() -> None:
    body = payload([museum(1, "A")], remark="runtime error: Query timed out")
    exc, _, _ = run(lambda r: httpx.Response(200, json=body), expect_error(fetch()))
    assert isinstance(exc, OverpassUnavailableError)


# client: cache


def test_cache_hit_and_copies() -> None:
    async def scenario(ovp: OverpassClient, clock: FakeClock) -> list[Any]:
        first = await ovp.fetch_candidates(GEO)
        first.places.clear()
        return [first, await ovp.fetch_candidates(GEO)]

    (first, second), rec, _ = run(ok(), scenario)
    assert rec.calls == 1
    assert first.places == []
    assert [p.name for p in second.places] == ["A", "R"]


def test_cache_key_rounding() -> None:
    async def scenario(ovp: OverpassClient, clock: FakeClock) -> None:
        await ovp.fetch_candidates(GEO)
        await ovp.fetch_candidates(other_geo(0.0001))
        await ovp.fetch_candidates(other_geo(0.01))
        await ovp.fetch_candidates(GEO.model_copy(update={"bbox": None}))

    _, rec, _ = run(ok(), scenario)
    # same rounded center shares, a far center or another radius does not
    assert rec.calls == 3


def test_cache_ttl() -> None:
    seen: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(200, json=payload([museum(1, "A")]))

    async def scenario(ovp: OverpassClient, clock: FakeClock) -> list[int]:
        await ovp.fetch_candidates(GEO)
        clock.advance(CACHE_TTL_S - 1)
        await ovp.fetch_candidates(GEO)
        after_hit = len(seen)
        clock.advance(1)
        await ovp.fetch_candidates(GEO)
        return [after_hit, len(seen)]

    counts, _, _ = run(responder, scenario)
    assert counts == [1, 2]


def test_empty_area_ttl() -> None:
    seen: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(200, json=payload([]))

    async def scenario(ovp: OverpassClient, clock: FakeClock) -> list[int]:
        with pytest.raises(NoPlacesFoundError):
            await ovp.fetch_candidates(GEO)
        clock.advance(EMPTY_TTL_S - 1)
        with pytest.raises(NoPlacesFoundError):
            await ovp.fetch_candidates(GEO)
        after_hit = len(seen)
        clock.advance(1)
        with pytest.raises(NoPlacesFoundError):
            await ovp.fetch_candidates(GEO)
        return [after_hit, len(seen)]

    counts, _, _ = run(responder, scenario)
    assert counts == [1, 2]


def test_errors_not_cached() -> None:
    responses = iter([httpx.Response(500), httpx.Response(200, json=payload([museum(1, "A")]))])

    async def scenario(ovp: OverpassClient, clock: FakeClock) -> Any:
        with pytest.raises(OverpassUnavailableError):
            await ovp.fetch_candidates(GEO)
        return await ovp.fetch_candidates(GEO)

    _, rec, _ = run(lambda request: next(responses), scenario)
    assert rec.calls == 2


# client: concurrency


def test_concurrent_same_area_one_call() -> None:
    async def scenario(ovp: OverpassClient, clock: FakeClock) -> list[Any]:
        return list(await asyncio.gather(*(ovp.fetch_candidates(GEO) for _ in range(3))))

    results, rec, _ = run(ok(), scenario)
    assert rec.calls == 1
    assert all(r == results[0] for r in results)


def test_concurrent_same_area_shares_failure() -> None:
    async def scenario(ovp: OverpassClient, clock: FakeClock) -> list[Any]:
        calls = [ovp.fetch_candidates(GEO) for _ in range(3)]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results, rec, _ = run(status(500), scenario)
    assert rec.calls == 1
    assert all(isinstance(r, OverpassUnavailableError) for r in results)


def test_different_areas_never_overlap_and_are_spaced() -> None:
    async def scenario(ovp: OverpassClient, clock: FakeClock) -> None:
        await asyncio.gather(*(ovp.fetch_candidates(other_geo(d)) for d in (0.0, 0.1, 0.2)))

    _, rec, clock = run(ok(), scenario)
    assert rec.calls == 3
    assert rec.max_in_flight == 1
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_refusal_stops_queued_requests() -> None:
    async def scenario(ovp: OverpassClient, clock: FakeClock) -> list[Any]:
        calls = [ovp.fetch_candidates(other_geo(d)) for d in (0.0, 0.1, 0.2)]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results, rec, _ = run(status(429), scenario)
    # the first refusal blocks the requests already waiting in line
    assert rec.calls == 1
    assert all(isinstance(r, OverpassUnavailableError) for r in results)


# constructor


def make(**kwargs: Any) -> OverpassClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    return OverpassClient(client, **({"user_agent": UA} | kwargs))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user_agent": f"python-httpx/{httpx.__version__}"},
        {"user_agent": ""},
        {"base_url": "http://overpass-api.de/api"},
        {"base_url": "https://u:p@overpass-api.de/api"},
        {"cache_ttl_s": 0},
        {"empty_ttl_s": -1},
        {"total_timeout_s": 0},
        {"max_cache_entries": 0},
        {"min_interval_s": 0},
    ],
)
def test_bad_constructor_args(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make(**kwargs)

# dense cities


def test_notable_caps_cover_each_type() -> None:
    assert oc.NOTABLE_CAPS == {"node": 150, "way": 400, "relation": 150}
    q = build_query(CENTER, RADIUS)
    for osm_type, cap in oc.NOTABLE_CAPS.items():
        assert f"{osm_type}.an->" in q
        assert f" out tags center {cap};" in q


def test_worst_case_fits_the_body_cap() -> None:
    elements = sum(oc.NOTABLE_CAPS.values()) + oc.OTHER_ATTRACTIONS_CAP + oc.NOTABLE_RESTAURANTS_CAP + oc.WEB_RESTAURANTS_CAP
    assert elements == 1200
    # calibration: paris returned 800 elements in 420144 bytes
    per_element = 420_144 / 800
    assert elements * per_element * 2 < oc.MAX_BODY_BYTES


def paris_shaped() -> list[dict[str, Any]]:
    # 97 notable nodes like central paris, then the big landmarks as a way and a relation
    els: list[dict[str, Any]] = []
    for n in range(1, 98):
        els.append(
            {
                "type": "node",
                "id": n,
                "lat": 44.436 + n * 0.0001,
                "lon": 26.103,
                "tags": {"name": f"Statue {n}", "tourism": "attraction", "wikidata": f"Q{n}", "website": "x", "phone": "x"},
            }
        )
    langs = {f"name:{c}": "x" for c in ("de", "fr", "ja", "ru", "zh", "es", "it", "pt", "pl", "nl", "sv", "fi", "da", "cs", "hu", "ro", "el", "tr", "ko", "ar", "he", "uk")}
    els.append(
        {
            "type": "way",
            "id": 9_000_001,
            "center": {"lat": 44.44, "lon": 26.11},
            "tags": {"name": "Arc de Triomphe", "historic": "monument", "wikidata": "Q64436", **langs},
        }
    )
    els.append(
        {
            "type": "relation",
            "id": 7_515_426,
            "center": {"lat": 44.437, "lon": 26.1},
            "tags": {"name": "Musée du Louvre", "name:en": "Louvre Museum", "tourism": "museum", "wikidata": "Q19675", **langs},
        }
    )
    return els


def test_paris_shaped_landmarks_survive_and_rank_first() -> None:
    body = json.dumps({"elements": paris_shaped()}).encode()
    cands = parse_candidates(CENTER, RADIUS, body)
    names = [p.name for p in cands.places]
    assert names[:2] == ["Louvre Museum", "Arc de Triomphe"]
    louvre = cands.places[0]
    assert (louvre.osm_type, louvre.osm_id) == ("relation", 7_515_426)


def test_full_notable_batch_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.overpass_client")
    nodes = [
        {"type": "node", "id": n, "lat": 44.436, "lon": 26.103 + n * 0.00001, "tags": {"name": f"N{n}", "tourism": "museum", "wikidata": "Q1"}}
        for n in range(1, oc.NOTABLE_NODES_CAP + 1)
    ]
    parse_candidates(CENTER, RADIUS, json.dumps({"elements": nodes}).encode())
    assert [r.getMessage() for r in caplog.records] == ["overpass notable nodes hit the cap of 150"]


def test_batch_below_cap_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.overpass_client")
    nodes = [
        {"type": "node", "id": n, "lat": 44.436, "lon": 26.103 + n * 0.00001, "tags": {"name": f"N{n}", "tourism": "museum", "wikidata": "Q1"}}
        for n in range(1, oc.NOTABLE_NODES_CAP)
    ]
    # restaurants and plain attractions never count toward the notable caps
    nodes += [
        {"type": "node", "id": 10_000 + n, "lat": 44.436, "lon": 26.2, "tags": {"name": f"R{n}", "amenity": "restaurant", "wikidata": "Q2"}}
        for n in range(5)
    ]
    nodes += [{"type": "node", "id": 20_000 + n, "lat": 44.436, "lon": 26.2, "tags": {"name": f"A{n}", "tourism": "museum"}} for n in range(5)]
    parse_candidates(CENTER, RADIUS, json.dumps({"elements": nodes}).encode())
    assert caplog.records == []


def test_candidates_are_listed_best_first_per_category() -> None:
    # prompts.py turns list position into the rank the planner sees
    els = paris_shaped()
    els.append({"type": "node", "id": 50_001, "lat": 44.436, "lon": 26.103, "tags": {"name": "Plain Bistro", "amenity": "restaurant", "website": "x"}})
    els.append({"type": "node", "id": 50_002, "lat": 44.436, "lon": 26.104, "tags": {"name": "Famous Bistro", "amenity": "restaurant", "wikidata": "Q9", "website": "x"}})
    body = json.dumps({"elements": els}).encode()
    cands = parse_candidates(CENTER, RADIUS, body)
    tags_by_id = {(e["type"], e["id"]): e["tags"] for e in els}
    for category in PlaceCategory:
        group = [p for p in cands.places if p.category is category]
        scores = [oc._score(tags_by_id[(p.osm_type, p.osm_id)], CENTER.distance_km(p.coords), RADIUS) for p in group]
        assert scores == sorted(scores, reverse=True), category
    restaurants = [p.name for p in cands.places if p.category is PlaceCategory.RESTAURANT]
    assert restaurants == ["Famous Bistro", "Plain Bistro"]
    # attractions come first, then restaurants
    cats = [p.category for p in cands.places]
    assert cats == sorted(cats, key=lambda c: c is PlaceCategory.RESTAURANT)
