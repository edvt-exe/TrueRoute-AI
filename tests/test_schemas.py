import json

import pytest
from pydantic import ValidationError

from app.schemas import (
    DISCLAIMER,
    MAX_ROUTE_POINTS,
    MIXED_DISCLAIMER,
    WALKING_DISCLAIMER,
    DayPlan,
    DayStop,
    DistanceSource,
    MAX_DETAIL_CHARS,
    MAX_LLM_OUTPUT_CHARS,
    BoundingBox,
    Budget,
    Coordinates,
    GeocodeResult,
    Itinerary,
    ItineraryValidationError,
    Pace,
    Place,
    PlaceCandidates,
    PlaceCategory,
    UserRequest,
    disclaimer_for,
    parse_llm_output,
    resolve,
)


def make_place(n: int) -> Place:
    # places sit on the equator, one degree apart
    return Place(
        ref=f"p{n}",
        osm_type="node",
        osm_id=n,
        name=f"Place {n}",
        category=PlaceCategory.ATTRACTION,
        coords=Coordinates(lat=0.0, lon=float(n)),
    )


def make_candidates(count: int = 10) -> PlaceCandidates:
    return PlaceCandidates(
        center=Coordinates(lat=0.0, lon=0.0),
        radius_m=5000,
        places=[make_place(n) for n in range(1, count + 1)],
    )


def make_request(**overrides: object) -> UserRequest:
    data: dict[str, object] = {"city": "Bucharest", "days": 2, "pace": "moderate"}
    return UserRequest.model_validate(data | overrides)


def llm_raw(days: list[tuple[int, list[str], str]]) -> str:
    return json.dumps(
        {"days": [{"day": d, "place_refs": r, "theme": t} for d, r, t in days]}
    )


# user request


def test_user_request_defaults_and_strip() -> None:
    req = UserRequest(city="  Bucharest ", days=3)
    assert req.city == "Bucharest"
    assert req.pace == Pace.MODERATE
    assert req.budget == Budget.MEDIUM
    assert req.interests == []


@pytest.mark.parametrize("days", [1, 7])
def test_user_request_days_ok(days: int) -> None:
    assert UserRequest(city="Rome", days=days).days == days


@pytest.mark.parametrize("days", [-1, 0, 8])
def test_user_request_days_rejected(days: int) -> None:
    with pytest.raises(ValidationError):
        UserRequest(city="Rome", days=days)


@pytest.mark.parametrize("city", ["", "   ", "Buch\narest", "Rome\x00", "x" * 101])
def test_user_request_bad_city(city: str) -> None:
    with pytest.raises(ValidationError):
        UserRequest(city=city, days=1)


def test_user_request_interests_limits() -> None:
    assert len(UserRequest(city="Rome", days=1, interests=list("abcde")).interests) == 5
    for bad in (list("abcdef"), ["x" * 41], [""], ["a\nb"]):
        with pytest.raises(ValidationError):
            UserRequest(city="Rome", days=1, interests=bad)


def test_user_request_rejects_extra_and_bad_enum() -> None:
    with pytest.raises(ValidationError):
        UserRequest.model_validate({"city": "Rome", "days": 1, "foo": 1})
    with pytest.raises(ValidationError):
        UserRequest.model_validate({"city": "Rome", "days": 1, "pace": "turbo"})
    with pytest.raises(ValidationError):
        UserRequest.model_validate({"city": "Rome", "days": 1, "budget": "free"})


# coordinates and geo


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(91, 0), (-91, 0), (0, 181), (0, -181), (float("nan"), 0), (0, float("inf"))],
)
def test_coordinates_rejected(lat: float, lon: float) -> None:
    with pytest.raises(ValidationError):
        Coordinates(lat=lat, lon=lon)


def test_coordinates_edges_ok() -> None:
    Coordinates(lat=90, lon=180)
    Coordinates(lat=-90, lon=-180)


def test_distance_known_values() -> None:
    origin = Coordinates(lat=0, lon=0)
    assert origin.distance_km(Coordinates(lat=0, lon=1)) == pytest.approx(111.195, abs=0.01)
    assert origin.distance_km(Coordinates(lat=90, lon=0)) == pytest.approx(10007.56, abs=0.05)
    assert origin.distance_km(origin) == 0.0


def test_distance_is_symmetric() -> None:
    a = Coordinates(lat=44.43, lon=26.10)
    b = Coordinates(lat=45.64, lon=25.59)
    assert a.distance_km(b) == pytest.approx(b.distance_km(a))


def test_bounding_box_lat_order() -> None:
    BoundingBox(south=44.0, west=25.0, north=45.0, east=27.0)
    with pytest.raises(ValidationError):
        BoundingBox(south=46.0, west=25.0, north=45.0, east=27.0)


def test_geocode_result_ok() -> None:
    res = GeocodeResult(
        query="Bucharest",
        display_name="Bucharest, Romania",
        center=Coordinates(lat=44.43, lon=26.10),
    )
    assert res.bbox is None


# place and candidates


@pytest.mark.parametrize("ref", ["p1", "p9", "p10", "p29", "p30"])
def test_place_ref_valid(ref: str) -> None:
    place = make_place(1).model_copy(update={"ref": ref})
    assert Place.model_validate(place.model_dump()).ref == ref


@pytest.mark.parametrize(
    "ref", ["p0", "p31", "p01", "p100", "P1", "p1\n", " p1", "1", "", "p-1"]
)
def test_place_ref_invalid(ref: str) -> None:
    data = make_place(1).model_dump() | {"ref": ref}
    with pytest.raises(ValidationError):
        Place.model_validate(data)


def test_place_is_frozen() -> None:
    place = make_place(1)
    with pytest.raises(ValidationError):
        setattr(place, "name", "other")


def test_place_accepts_diacritics() -> None:
    data = make_place(1).model_dump() | {"name": "Ateneul Român"}
    assert Place.model_validate(data).name == "Ateneul Român"


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "  "},
        {"name": "a\tb"},
        {"osm_id": 0},
        {"osm_type": "area"},
        {"price": 10},
        {"address": ""},
    ],
)
def test_place_rejected(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Place.model_validate(make_place(1).model_dump() | patch)


def test_candidates_max_30() -> None:
    assert len(make_candidates(30).places) == 30
    places = [make_place(n) for n in range(1, 31)]
    places.append(make_place(1).model_copy(update={"osm_id": 99}))
    with pytest.raises(ValidationError) as info:
        PlaceCandidates(center=Coordinates(lat=0, lon=0), radius_m=5000, places=places)
    assert info.value.errors()[0]["type"] == "too_long"


def test_candidates_rejects_duplicates_and_empty() -> None:
    center = Coordinates(lat=0, lon=0)
    dup_ref = [make_place(1), make_place(2).model_copy(update={"ref": "p1"})]
    with pytest.raises(ValidationError, match="duplicate place refs"):
        PlaceCandidates(center=center, radius_m=5000, places=dup_ref)
    dup_osm = [make_place(1), make_place(2).model_copy(update={"osm_id": 1})]
    with pytest.raises(ValidationError, match="duplicate osm places"):
        PlaceCandidates(center=center, radius_m=5000, places=dup_osm)
    with pytest.raises(ValidationError):
        PlaceCandidates(center=center, radius_m=5000, places=[])


@pytest.mark.parametrize("radius", [0, -5, 50_001])
def test_candidates_radius_rejected(radius: int) -> None:
    with pytest.raises(ValidationError):
        PlaceCandidates(
            center=Coordinates(lat=0, lon=0), radius_m=radius, places=[make_place(1)]
        )


# llm contract


def test_llm_valid_output() -> None:
    llm = parse_llm_output(llm_raw([(1, ["p1", "p2"], "Old town"), (2, ["p3"], "Parks")]))
    assert [d.day for d in llm.days] == [1, 2]
    assert llm.days[0].place_refs == ["p1", "p2"]


BAD_LLM = [
    pytest.param(llm_raw([(2, ["p1"], "a")]), id="starts_at_2"),
    pytest.param(llm_raw([(1, ["p1"], "a"), (3, ["p2"], "b")]), id="gap"),
    pytest.param(llm_raw([(2, ["p1"], "a"), (1, ["p2"], "b")]), id="out_of_order"),
    pytest.param(llm_raw([(1, ["p1"], "a"), (2, ["p1"], "b")]), id="dup_across_days"),
    pytest.param(llm_raw([(1, ["p1", "p1"], "a")]), id="dup_in_day"),
    pytest.param(llm_raw([(1, [], "a")]), id="empty_refs"),
    pytest.param(llm_raw([(1, ["p1", "p2", "p3", "p4", "p5", "p6"], "a")]), id="six_refs"),
    pytest.param(llm_raw([(1, ["p31"], "a")]), id="bad_ref"),
    pytest.param(llm_raw([(1, ["p1"], "")]), id="empty_theme"),
    pytest.param(llm_raw([(1, ["p1"], "x" * 81)]), id="long_theme"),
    pytest.param(llm_raw([(1, ["p1"], "a\nb")]), id="newline_theme"),
    pytest.param(llm_raw([]), id="no_days"),
    pytest.param(
        llm_raw([(d, [f"p{d}"], "a") for d in range(1, 9)]), id="eight_days"
    ),
    pytest.param(
        '{"days":[{"day":"1","place_refs":["p1"],"theme":"a"}]}', id="day_as_string"
    ),
    pytest.param(
        '{"days":[{"day":1,"place_refs":["p1"],"theme":"a","name":"Fake"}]}',
        id="extra_name_key",
    ),
    pytest.param(
        '{"days":[{"day":1,"place_refs":["p1"],"theme":"a","lat":1.0}]}',
        id="extra_lat_key",
    ),
    pytest.param(
        '{"days":[{"day":1,"place_refs":["p1"],"theme":"a"}],"notes":"hi"}',
        id="extra_top_key",
    ),
    pytest.param("not json", id="not_json"),
    pytest.param("", id="empty"),
    pytest.param("null", id="null"),
    pytest.param("[]", id="list"),
]


@pytest.mark.parametrize("raw", BAD_LLM)
def test_llm_rejects(raw: str) -> None:
    with pytest.raises(ItineraryValidationError):
        parse_llm_output(raw)


def test_llm_output_size_limit() -> None:
    with pytest.raises(ItineraryValidationError, match="too large"):
        parse_llm_output(" " * (MAX_LLM_OUTPUT_CHARS + 1))
    valid = llm_raw([(1, ["p1"], "a")])
    padded = valid + " " * (MAX_LLM_OUTPUT_CHARS - len(valid))
    assert len(padded) == MAX_LLM_OUTPUT_CHARS
    assert len(parse_llm_output(padded).days) == 1


def test_llm_error_does_not_echo_input() -> None:
    raw = '{"days":[{"day":1,"place_refs":["p1"],"theme":"a","name":"SECRET-MARKER"}]}'
    with pytest.raises(ItineraryValidationError) as info:
        parse_llm_output(raw)
    assert "SECRET-MARKER" not in str(info.value)
    assert "SECRET-MARKER" not in info.value.detail
    assert "SECRET-MARKER" not in repr(info.value.__cause__)
    assert info.value.detail != ""


def test_llm_error_detail_is_bounded() -> None:
    raw = llm_raw([(d, ["bad"], "") for d in range(1, 8)])
    with pytest.raises(ItineraryValidationError) as info:
        parse_llm_output(raw)
    assert len(info.value.detail) == MAX_DETAIL_CHARS


def test_llm_error_detail_format() -> None:
    with pytest.raises(ItineraryValidationError) as info:
        parse_llm_output("not json")
    assert info.value.detail.startswith("Invalid JSON")
    with pytest.raises(ItineraryValidationError) as info:
        parse_llm_output(llm_raw([(1, ["p1"], "")]))
    assert info.value.detail.startswith("days.0.theme: ")


def test_error_exposes_message_and_detail() -> None:
    err = ItineraryValidationError("boom", "x" * 1000)
    assert str(err) == "boom"
    assert err.message == "boom"
    assert len(err.detail) == MAX_DETAIL_CHARS


# resolve


def test_resolve_happy_path() -> None:
    cands = make_candidates(6)
    req = make_request(days=2, pace="moderate", budget="low")
    llm = parse_llm_output(llm_raw([(1, ["p1", "p3", "p2"], "A"), (2, ["p6"], "B")]))
    it = resolve(llm, cands, req)
    assert (it.city, it.days, it.pace, it.budget) == ("Bucharest", 2, Pace.MODERATE, Budget.LOW)
    day1, day2 = it.day_plans
    assert [s.order for s in day1.stops] == [1, 2, 3]
    assert [s.place for s in day1.stops] == [cands.places[0], cands.places[2], cands.places[1]]
    assert day1.stops[0].leg_distance_km is None
    assert day1.stops[1].leg_distance_km == pytest.approx(222.39, abs=0.02)
    assert day1.stops[2].leg_distance_km == pytest.approx(111.19, abs=0.02)
    assert day1.total_distance_km == pytest.approx(333.58, abs=0.03)
    assert day1.theme == "A"
    assert day2.stops[0].leg_distance_km is None
    assert day2.total_distance_km == 0.0
    assert it.unused_places == 2
    assert it.disclaimer == DISCLAIMER
    assert "aerial" in it.disclaimer


def test_resolve_unknown_ref() -> None:
    llm = parse_llm_output(llm_raw([(1, ["p1", "p9"], "A"), (2, ["p2"], "B")]))
    with pytest.raises(ItineraryValidationError, match="unknown place ref p9"):
        resolve(llm, make_candidates(6), make_request(days=2))


@pytest.mark.parametrize("llm_days", [1, 3])
def test_resolve_day_count_mismatch(llm_days: int) -> None:
    llm = parse_llm_output(
        llm_raw([(d, [f"p{d}"], "a") for d in range(1, llm_days + 1)])
    )
    with pytest.raises(ItineraryValidationError, match="day count"):
        resolve(llm, make_candidates(6), make_request(days=2))


@pytest.mark.parametrize(
    ("pace", "stops", "ok"),
    [
        ("relaxed", 3, True),
        ("relaxed", 4, False),
        ("moderate", 4, True),
        ("moderate", 5, False),
        ("packed", 5, True),
    ],
)
def test_resolve_pace_ceiling(pace: str, stops: int, ok: bool) -> None:
    refs = [f"p{n}" for n in range(1, stops + 1)]
    llm = parse_llm_output(llm_raw([(1, refs, "a")]))
    req = make_request(days=1, pace=pace)
    if ok:
        assert len(resolve(llm, make_candidates(10), req).day_plans[0].stops) == stops
    else:
        with pytest.raises(ItineraryValidationError, match="too many stops"):
            resolve(llm, make_candidates(10), req)


def test_resolve_keeps_code_owned_facts() -> None:
    cands = make_candidates(3)
    llm = parse_llm_output(llm_raw([(1, ["p2"], "A")]))
    it = resolve(llm, cands, make_request(days=1))
    place = it.day_plans[0].stops[0].place
    assert place == cands.places[1]
    assert (place.name, place.coords.lon) == ("Place 2", 2.0)


def test_itinerary_day_count_must_match() -> None:
    llm = parse_llm_output(llm_raw([(1, ["p1"], "a")]))
    it = resolve(llm, make_candidates(3), make_request(days=1))
    with pytest.raises(ValidationError, match="day_plans must match days"):
        Itinerary.model_validate(it.model_dump() | {"days": 2})

# walking day plans


def walking_day(minutes: tuple[int, ...] = (12, 7), **overrides: object) -> dict[str, object]:
    stops: list[dict[str, object]] = [{"order": 1, "place": make_place(1), "leg_distance_km": None}]
    for i, m in enumerate(minutes, start=2):
        stops.append({"order": i, "place": make_place(i), "leg_distance_km": 1.0, "leg_walk_minutes": m})
    data: dict[str, object] = {
        "day": 1,
        "theme": "Old town",
        "stops": stops,
        "total_distance_km": float(len(minutes)),
        "distance_source": "walking",
        "total_walk_minutes": sum(minutes),
        "route_latlon": [(0.0, 1.0), (0.0, 2.0), (0.0, 3.0)],
        "route_provider": "openrouteservice",
    }
    return data | overrides


def test_walking_day_ok() -> None:
    plan = DayPlan.model_validate(walking_day())
    assert plan.distance_source is DistanceSource.WALKING
    assert plan.total_walk_minutes == 19
    assert plan.route_latlon[1] == (0.0, 2.0)


def test_aerial_is_the_default() -> None:
    plan = DayPlan(day=1, theme="t", stops=[DayStop(order=1, place=make_place(1))], total_distance_km=0)
    assert plan.distance_source is DistanceSource.AERIAL
    assert plan.route_latlon == [] and plan.total_walk_minutes is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"distance_source": "aerial"},
        {"distance_source": "aerial", "route_latlon": [], "total_walk_minutes": None},
    ],
)
def test_aerial_day_rejects_walking_data(overrides: dict[str, object]) -> None:
    # minutes on the legs alone are enough to reject an aerial day
    with pytest.raises(ValidationError, match="aerial day"):
        DayPlan.model_validate(walking_day(**overrides))


def test_aerial_day_rejects_route_alone() -> None:
    data = walking_day(distance_source="aerial", total_walk_minutes=None)
    data["stops"] = [dict(s, leg_walk_minutes=None) for s in data["stops"]]  # type: ignore[attr-defined]
    with pytest.raises(ValidationError, match="aerial day"):
        DayPlan.model_validate(data)
    data["route_latlon"] = []
    # the provider alone is still walking data
    with pytest.raises(ValidationError, match="aerial day"):
        DayPlan.model_validate(data)
    data["route_provider"] = None
    assert DayPlan.model_validate(data).distance_source is DistanceSource.AERIAL


def test_walking_day_needs_route() -> None:
    with pytest.raises(ValidationError, match="route"):
        DayPlan.model_validate(walking_day(route_latlon=[(0.0, 1.0)]))


def test_walking_day_needs_two_stops() -> None:
    with pytest.raises(ValidationError, match="two stops"):
        DayPlan.model_validate(walking_day(minutes=(), total_distance_km=0.0))


def test_walking_day_needs_minutes_on_every_leg() -> None:
    data = walking_day()
    data["stops"][2]["leg_walk_minutes"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="every leg"):
        DayPlan.model_validate(data)


def test_walking_day_first_stop_has_no_minutes() -> None:
    data = walking_day()
    data["stops"][0]["leg_walk_minutes"] = 3  # type: ignore[index]
    with pytest.raises(ValidationError, match="every leg"):
        DayPlan.model_validate(data)


def test_walking_day_total_must_match() -> None:
    with pytest.raises(ValidationError, match="sum of the legs"):
        DayPlan.model_validate(walking_day(total_walk_minutes=20))


@pytest.mark.parametrize("point", [(91.0, 0.0), (0.0, 181.0), (-90.5, 0.0)])
def test_route_points_are_bounded(point: tuple[float, float]) -> None:
    with pytest.raises(ValidationError):
        DayPlan.model_validate(walking_day(route_latlon=[(0.0, 1.0), point]))


def test_route_point_cap() -> None:
    many = [(0.0, i / 1000) for i in range(MAX_ROUTE_POINTS + 1)]
    with pytest.raises(ValidationError):
        DayPlan.model_validate(walking_day(route_latlon=many))
    assert len(DayPlan.model_validate(walking_day(route_latlon=many[:-1])).route_latlon) == MAX_ROUTE_POINTS


def test_negative_minutes_rejected() -> None:
    with pytest.raises(ValidationError):
        DayPlan.model_validate(walking_day(minutes=(-1, 5)))


def test_disclaimer_for() -> None:
    walking = DayPlan.model_validate(walking_day())
    aerial = DayPlan(day=1, theme="t", stops=[DayStop(order=1, place=make_place(1))], total_distance_km=0)
    assert disclaimer_for([aerial, aerial]) == DISCLAIMER
    assert disclaimer_for([walking, walking]) == WALKING_DISCLAIMER
    assert disclaimer_for([walking, aerial]) == MIXED_DISCLAIMER
    assert "aerial" in MIXED_DISCLAIMER and "aerial" not in WALKING_DISCLAIMER


def test_walking_day_needs_a_provider() -> None:
    with pytest.raises(ValidationError, match="provider that drew it"):
        DayPlan.model_validate(walking_day(route_provider=None))
    assert DayPlan.model_validate(walking_day(route_provider="osrm")).route_provider == "osrm"
    with pytest.raises(ValidationError):
        DayPlan.model_validate(walking_day(route_provider="google"))