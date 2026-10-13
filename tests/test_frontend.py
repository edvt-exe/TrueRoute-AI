import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.security as security
from app.main import ERROR_TABLE, FRONTEND_DIR, REQUEST_TIMEOUT_S, ItineraryResponse, create_app
from app.schemas import MAX_DAYS, MAX_INTERESTS, MAX_STOPS_BY_PACE, Budget, DistanceSource, Pace, UserRequest
from tests.test_main import BODY, fakes

APP_JS = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
INDEX = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
STYLES = (FRONTEND_DIR / "styles.css").read_text(encoding="utf-8")
# sha256 of the files in the leaflet 1.9.4 npm package, a silent swap fails here
LEAFLET_SHA256 = {
    "leaflet.js": "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a",
    "leaflet.css": "a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6",
}


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_scripts = 0
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_scripts += 1


def page() -> Page:
    parser = Page()
    parser.feed(INDEX)
    return parser


def js_const(name: str) -> str:
    match = re.search(rf"^const {name} = (.+);$", APP_JS, re.MULTILINE)
    assert match, f"{name} not found in app.js"
    return match.group(1)


def schema_keys(node: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties" and isinstance(v, dict):
                keys |= set(v)
            keys |= schema_keys(v)
    elif isinstance(node, list):
        for item in node:
            keys |= schema_keys(item)
    return keys


@pytest.fixture(scope="module")
def client() -> Any:
    with TestClient(create_app(services=fakes())) as c:
        yield c


# serving


def test_page_served_at_root(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["content-security-policy"] == security.CSP
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "<title>TrueRoute</title>" in res.text


@pytest.mark.parametrize(
    ("path", "mime"),
    [
        ("/app.js", "text/javascript"),
        ("/styles.css", "text/css"),
        ("/favicon.svg", "image/svg+xml"),
        ("/vendor/leaflet/leaflet.js", "text/javascript"),
        ("/vendor/leaflet/leaflet.css", "text/css"),
        ("/vendor/leaflet/images/layers.png", "image/png"),
        ("/fonts/Geist-Variable.woff2", "font/woff2"),
    ],
)
def test_assets_have_types_nosniff_accepts(client: TestClient, path: str, mime: str) -> None:
    # with nosniff a wrong type would block the script or stylesheet
    res = client.get(path)
    assert res.status_code == 200
    assert res.headers["content-type"].split(";")[0] == mime


@pytest.mark.parametrize("accept", ["application/json", "text/html"])
def test_api_typos_are_not_the_page(client: TestClient, accept: str) -> None:
    # a browser navigation sends text/html, an index fallback would answer 200 with the page
    res = client.get("/api/nope", headers={"accept": accept})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "http_404"
    assert client.get("/nope.html", headers={"accept": accept}).status_code == 404
    assert client.get("/some/client/route", headers={"accept": accept}).status_code == 404


def test_api_still_wins_over_files(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.post("/api/itinerary", json=BODY).status_code == 200


def test_frontend_dir_is_in_the_repo() -> None:
    assert FRONTEND_DIR == Path(main.__file__).resolve().parent.parent / "frontend"
    assert (FRONTEND_DIR / "index.html").is_file()


# csp compatibility of the page


def test_no_inline_code_or_styles() -> None:
    p = page()
    assert p.inline_scripts == 0
    tags = [t for t, _ in p.tags]
    assert "style" not in tags
    for tag, attrs in p.tags:
        assert "style" not in attrs, tag
        assert not any(k.startswith("on") for k in attrs), tag
        if tag == "script":
            assert attrs.get("src")


def test_every_local_reference_exists() -> None:
    refs = [a.get("src") or a.get("href") for t, a in page().tags if t in ("script", "link")]
    css_refs = re.findall(r"url\(([^)#]+)\)", STYLES)
    assert refs and css_refs
    for ref in refs + css_refs:
        assert ref and not ref.startswith(("http:", "https:", "//")), ref
        assert (FRONTEND_DIR / ref.strip("\"'")).is_file(), ref
    leaflet_css = (FRONTEND_DIR / "vendor/leaflet/leaflet.css").read_text(encoding="utf-8")
    for ref in re.findall(r"url\((images/[^)]+)\)", leaflet_css):
        assert (FRONTEND_DIR / "vendor/leaflet" / ref).is_file(), ref


def test_external_links_are_known() -> None:
    hrefs = {a.get("href") for t, a in page().tags if t == "a"}
    assert hrefs == {"/", "/docs", "https://www.openstreetmap.org/copyright"}
    urls = set(re.findall(r"https?://[^\s\"'`<>)]+", APP_JS))
    assert urls == {
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://www.openstreetmap.org/copyright",
        "https://www.openstreetmap.org/",
        "https://www.openstreetmap.org/${place.osm_type}/${place.osm_id}",
        "https://leafletjs.com",
    }


def test_tile_host_is_allowed_by_csp() -> None:
    tile = js_const("TILE_URL").strip('"')
    assert tile.startswith("https://tile.openstreetmap.org/")
    assert "img-src 'self' data: https://tile.openstreetmap.org" in security.CSP


def test_referrer_is_sent_to_tiles() -> None:
    # osm tile policy, a no-referrer page or meta would get tiles refused
    assert "no-referrer" not in INDEX and "referrerpolicy" not in INDEX.lower()
    assert security.REFERRER_POLICY == "strict-origin-when-cross-origin"


# untrusted text


@pytest.mark.parametrize("sink", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"])
def test_no_html_sinks(sink: str) -> None:
    assert sink not in APP_JS


def test_names_go_in_as_text() -> None:
    assert "node.textContent = text" in APP_JS
    assert 'el("span", "stop-name", stop.place.name)' in APP_JS
    assert 'el("p", "popup-name", stop.place.name)' in APP_JS
    # popups get a dom node, never an html string
    assert "bindPopup(popupContent(day, stop)" in APP_JS
    # the one html string is escaped before leaflet sees it
    assert "state.routingAttribution = text ? escapeHtml(text) : null;" in APP_JS
    assert "setRoutingAttribution(data.routing_attribution);" in APP_JS


def test_osm_links_are_built_from_checked_parts() -> None:
    assert js_const("OSM_TYPES") == 'new Set(["node", "way", "relation"])'
    assert "Number.isInteger(place.osm_id) && " not in APP_JS
    assert "!Number.isInteger(place.osm_id) || place.osm_id <= 0" in APP_JS


# api contract


def test_endpoint_and_header() -> None:
    assert js_const("API_URL") == '"/api/itinerary"'
    assert js_const("ACCESS_HEADER") == f'"{security.ACCESS_HEADER}"'


def test_client_waits_longer_than_the_server() -> None:
    assert int(js_const("CLIENT_TIMEOUT_MS")) > REQUEST_TIMEOUT_S * 1000


def test_interest_limits_match() -> None:
    assert int(js_const("MAX_INTERESTS")) == MAX_INTERESTS
    limit = UserRequest.model_json_schema()["properties"]["interests"]["items"]["maxLength"]
    assert int(js_const("MAX_INTEREST_CHARS")) == limit


def backend_error_codes() -> set[str]:
    codes = {code for _, _, code, _ in ERROR_TABLE}
    main_src = Path(main.__file__).read_text(encoding="utf-8")
    codes |= set(re.findall(r'_error\(\d+, "(\w+)"', main_src))
    sec_src = Path(security.__file__).read_text(encoding="utf-8")
    codes |= set(re.findall(r'Refusal\(\s*\d+, "(\w+)"', sec_src))
    return codes


def test_every_error_code_has_a_message() -> None:
    codes = backend_error_codes()
    assert {"access_denied", "rate_limited", "daily_cap_reached", "invalid_request", "payload_too_large"} <= codes
    block = APP_JS[APP_JS.index("const MESSAGES = {") : APP_JS.index("};", APP_JS.index("const MESSAGES = {"))]
    handled = set(re.findall(r"^  (\w+): ", block, re.MULTILINE))
    assert codes <= handled, codes - handled


def test_unknown_codes_fall_back() -> None:
    assert "MESSAGES[code] || MESSAGES.internal_error" in APP_JS


@pytest.mark.parametrize(
    "field",
    [
        "itinerary",
        "attribution",
        "attribution_url",
        "routing_attribution",
        "city",
        "days",
        "day_plans",
        "disclaimer",
        "day",
        "theme",
        "stops",
        "order",
        "place",
        "name",
        "category",
        "coords",
        "lat",
        "lon",
        "osm_type",
        "osm_id",
        "leg_distance_km",
        "leg_walk_minutes",
        "total_distance_km",
        "total_walk_minutes",
        "distance_source",
        "route_latlon",
    ],
)
def test_fields_read_by_the_page_exist(field: str) -> None:
    assert re.search(rf"\.{field}\b", APP_JS), field
    assert field in schema_keys(ItineraryResponse.model_json_schema())


def test_distance_source_values() -> None:
    # the page checks for walking, anything else is drawn as straight legs
    assert {s.value for s in DistanceSource} == {"walking", "aerial"}
    compared = re.findall(r'distance_source === "(\w+)"', APP_JS)
    assert compared
    assert set(compared) <= {s.value for s in DistanceSource}


# form matches the request schema


def radio_values(name: str) -> list[str]:
    return [a["value"] or "" for t, a in page().tags if t == "input" and a.get("name") == name]


def test_days_stepper_matches() -> None:
    days = next(a for t, a in page().tags if t == "input" and a.get("id") == "days")
    assert (days["type"], days["min"], days["max"]) == ("number", "1", str(MAX_DAYS))
    assert int(js_const("MAX_DAYS")) == MAX_DAYS
    assert 1 <= int(days["value"] or 0) <= MAX_DAYS


def test_interest_pills_fit_the_api() -> None:
    limit = UserRequest.model_json_schema()["properties"]["interests"]["items"]["maxLength"]
    values = [a["value"] or "" for t, a in page().tags if t == "input" and a.get("name") == "interest"]
    assert values
    for value in values:
        assert 1 <= len(value) <= limit and value.isprintable()
    assert len({v.lower() for v in values}) == len(values)


def test_pace_and_budget_choices() -> None:
    assert radio_values("pace") == [p.value for p in Pace]
    assert radio_values("budget") == [b.value for b in Budget]


def test_pace_labels_match_stop_caps() -> None:
    for pace, cap in MAX_STOPS_BY_PACE.items():
        assert re.search(rf'id="pace-{pace.value}"[^>]*><label[^>]*>\w+<span class="seg-hint">{cap} stops</span>', INDEX), pace


def test_defaults_match_the_api() -> None:
    fields = UserRequest.model_fields
    for name, default in (("pace", fields["pace"].default.value), ("budget", fields["budget"].default.value)):
        checked = [a["value"] for t, a in page().tags if t == "input" and a.get("name") == name and "checked" in a]
        assert checked == [default]


def test_city_length_matches() -> None:
    limit = UserRequest.model_json_schema()["properties"]["city"]["maxLength"]
    city = next(a for t, a in page().tags if t == "input" and a.get("id") == "city")
    assert city["maxlength"] == str(limit)


def test_access_code_field_is_masked_and_bounded() -> None:
    code = next(a for t, a in page().tags if t == "input" and a.get("id") == "access-code")
    assert code["type"] == "password"
    assert code["maxlength"] == str(security.MAX_ACCESS_CODE)
    assert code["autocomplete"] == "off"


# accessibility basics


def test_status_is_announced() -> None:
    status = next(a for t, a in page().tags if a.get("id") == "status")
    assert status.get("role") == "status" and status.get("aria-live") == "polite"


def test_every_input_has_a_label() -> None:
    p = page()
    label_for = {a.get("for") for t, a in p.tags if t == "label"}
    for tag, attrs in p.tags:
        if tag == "input":
            assert attrs.get("id") in label_for, attrs


def test_reduced_motion_respected() -> None:
    assert "@media (prefers-reduced-motion: reduce)" in STYLES
    assert "reducedMotion()" in APP_JS


def test_dark_theme_present() -> None:
    assert "@media (prefers-color-scheme: dark)" in STYLES
    assert '<meta name="color-scheme" content="light dark">' in INDEX


# vendored code and licences


@pytest.mark.parametrize(("name", "digest"), sorted(LEAFLET_SHA256.items()))
def test_leaflet_is_the_pinned_release(name: str, digest: str) -> None:
    data = (FRONTEND_DIR / "vendor/leaflet" / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest


def test_leaflet_version_and_licence() -> None:
    assert "Leaflet 1.9.4" in (FRONTEND_DIR / "vendor/leaflet/leaflet.js").read_text(encoding="utf-8")[:200]
    assert "BSD 2-Clause License" in (FRONTEND_DIR / "vendor/leaflet/LICENSE").read_text(encoding="utf-8")


def test_fonts_are_licensed_and_present() -> None:
    assert "SIL Open Font License" in (FRONTEND_DIR / "fonts/OFL.txt").read_text(encoding="utf-8")
    fonts = sorted(p.name for p in (FRONTEND_DIR / "fonts").glob("*.woff2"))
    assert fonts == sorted(re.findall(r"url\(fonts/([\w-]+\.woff2)\)", STYLES))