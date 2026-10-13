# TrueRoute AI

TrueRoute builds day-by-day city itineraries from real map data. You send a city, a number of
days and a pace; it returns an ordered route per day with real places, coordinates and
distances between stops.

The rule behind the design: Python collects and checks every fact, the language model only
chooses and orders places it is given. The model never writes a place name, coordinate,
address, opening hour or price.

The project is a backend API. There is no frontend yet.

## How it works

```
request ──► Nominatim ──► Overpass ──► ranking ──► Claude ──► validation ──► itinerary
            city to       places from   top 20 +    picks and   refs resolved
            coordinates   OpenStreetMap 10 places   orders refs back to places
```

1. **Geocoding.** Nominatim turns the city name into a center point and bounding box.
2. **Places.** One Overpass query fetches attractions (tourism, selected historic sites, parks)
   and restaurants around the center. The search radius is derived from the city's bounding box
   and kept between 1.5 and 8 km.
3. **Ranking.** OpenStreetMap has no ratings, so places are scored from the data itself:
   a Wikidata or Wikipedia link, the number of languages the name exists in, how complete the
   tags are, and distance from the center. A diversity rule stops one kind of place (museums,
   for example) from filling the whole list. The best 20 attractions and 10 restaurants get short
   references, `p1` to `p30`.
4. **Planning.** Claude receives only those references, names and coordinates, and answers in a
   JSON schema built for that request. The schema lists the allowed references, the exact
   number of days and a fixed set of stop slots per day, so an invented place, a missing day or
   too many stops cannot be produced.
5. **Validation.** The answer is checked again in Python (no repeated places, valid themes,
   stop limits) and each reference is replaced by the real place. Distances are computed with
   the haversine formula. If the answer fails a check, the model gets one retry with the reason.

## Requirements

- Python 3.11 or newer (developed on 3.14)
- An Anthropic API key
- A contact address for the OpenStreetMap services (see [Data sources](#data-sources-and-usage-rules))

## Setup

```bash
git clone https://github.com/edvt-exe/TrueRoute-AI.git
cd TrueRoute-AI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Then fill in `.env`. Two values are required:

```
ANTHROPIC_API_KEY=your key from the Claude Console
APP_USER_AGENT=TrueRoute-AI/0.1 (you@example.com)
```

`.env` is ignored by git. Never commit it.

## Configuration

All settings are read from `.env` in the project root. Real environment variables take
precedence over the file. Unknown keys are rejected, so a typo fails at startup instead of being
ignored.

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | none, required | Never logged or returned |
| `APP_USER_AGENT` | none, required | Must name the app and give a contact; library defaults are rejected |
| `LLM_MODEL` | `claude-sonnet-5-5` | Any Claude model ID |
| `LLM_EFFORT` | `medium` | `low`, `medium` or `high` |
| `NOMINATIM_BASE_URL` | `https://nominatim.openstreetmap.org` | HTTPS only |
| `OVERPASS_BASE_URL` | `https://overpass-api.de/api` | HTTPS only |
| `LOG_LEVEL` | `INFO` | The Anthropic and HTTP client loggers stay at `WARNING`, because at debug level they log request bodies |

## Running

```bash
uvicorn app.main:app --workers 1
```

Run a single worker. Caches and rate limiters live in process memory, and more workers would
multiply the request rate to the public OpenStreetMap servers beyond what their policies allow.

Interactive API docs are served at `http://127.0.0.1:8000/docs`.

## API

### `POST /itinerary`

```bash
curl -s -X POST http://127.0.0.1:8000/itinerary \
  -H "content-type: application/json" \
  -d '{"city": "Bucharest", "days": 2, "pace": "relaxed", "interests": ["history"]}'
```

| Field | Type | Rules |
|---|---|---|
| `city` | string | 1 to 100 characters, required |
| `days` | integer | 1 to 7, required |
| `pace` | string | `relaxed` (2 to 3 stops a day), `moderate` (3 to 4), `packed` (4 to 5); default `moderate` |
| `budget` | string | `low`, `medium`, `high`; default `medium`. A soft hint only, there are no prices in the data |
| `interests` | list of strings | Up to 5, each up to 40 characters |

Response, shortened:

```json
{
  "itinerary": {
    "city": "Bucharest",
    "days": 2,
    "pace": "relaxed",
    "budget": "medium",
    "day_plans": [
      {
        "day": 1,
        "theme": "Old Town heritage: monastery, inn and princely court",
        "stops": [
          {
            "order": 1,
            "place": {
              "ref": "p16",
              "osm_type": "node",
              "osm_id": 672149975,
              "name": "Stavropoleos Monastery",
              "category": "attraction",
              "coords": {"lat": 44.431803, "lon": 26.0987087},
              "address": "Strada Poștei 6"
            },
            "leg_distance_km": null
          }
        ],
        "total_distance_km": 0.53
      }
    ],
    "unused_places": 24,
    "disclaimer": "Distances are straight-line (aerial), not walking or driving routes"
  },
  "attribution": "Data © OpenStreetMap contributors, available under the Open Database License (ODbL)",
  "attribution_url": "https://www.openstreetmap.org/copyright"
}
```

`osm_type` and `osm_id` identify the place on OpenStreetMap, for example
`https://www.openstreetmap.org/node/672149975`.

### Errors

Every error has the same shape, with a fixed message that never contains internal details or
the submitted values:

```json
{"error": {"code": "city_not_found", "message": "city not found"}}
```

| Status | Code | Cause |
|---|---|---|
| 404 | `city_not_found` | Nominatim has no match for the city |
| 413 | `payload_too_large` | Request body over 16 KB |
| 422 | `invalid_request` | Request body does not match the rules above |
| 422 | `not_enough_places` | Too few usable places near the city for the number of days |
| 500 | `server_misconfigured` | API key or settings rejected; details are in the server log |
| 500 | `internal_error` | Unexpected failure; the traceback is in the server log |
| 502 | `planner_failed` | The model refused, was cut off, or failed validation twice |
| 503 | `upstream_unavailable` | Nominatim, Overpass or the Claude API is down or rate limited |
| 504 | `request_timeout` | The whole request took longer than 150 seconds |

### `GET /health`

Returns `{"status": "ok"}`. It does not call any external service.

## Performance and cost

Measured in October 2026 on a 2-day Bucharest request with `claude-sonnet-5-5` at `medium`
effort:

- About 2,900 input and 110 output tokens per itinerary, roughly $0.007 at list prices.
- 20 to 25 seconds when the city is not cached, mostly the Overpass query.
- About 4 seconds when the city is cached. Geocoding results are cached for 24 hours, places for
  7 days.
- One Overpass response is about 250 KB.

## Data sources and usage rules

Place data comes from [OpenStreetMap](https://www.openstreetmap.org) through two public
services. Both are run by volunteers on donated servers and have usage policies this project
follows:

- **Nominatim** ([policy](https://operations.osmfoundation.org/policies/nominatim/)): at most one
  request per second for the whole application, an identifying User-Agent, cached results.
  The client keeps 1.1 seconds between requests and pauses for 60 seconds after a refusal.
- **Overpass** ([policy](https://wiki.openstreetmap.org/wiki/Overpass_API)): an identifying
  User-Agent, one request at a time, a 30 second pause after HTTP 429 or 406. Regular use should
  stay around 100 queries and 10 MB a day. Commercial use requires a self-hosted or paid
  Overpass server.

Anything that shows these results to people must display the OpenStreetMap attribution and make
clear the data is under the ODbL. The API returns both in every response for that reason.

## Testing

```bash
python -m pytest
```

The test suite needs no network access and no API key. External services are replaced with
`httpx.MockTransport`; the API tests use FastAPI's `TestClient` with fake services.

During development each module was also checked by deliberately breaking its code in many small
ways and confirming the tests failed every time (mutation testing). That tooling is not part of
the repository.

## Project layout

```
app/
  main.py                FastAPI app, startup wiring, error mapping
  config.py              settings from .env, logging setup
  schemas.py             data models, model-output validation, distance calculation
  services/
    geocoder.py          Nominatim client
    overpass_client.py   Overpass client, place ranking and filtering
    llm_agent.py         Claude client with one validation retry
  utils/
    prompts.py           prompts, per-request output schema, slot parsing
    throttle.py          rate limiting, caching, per-key locks
    http_guard.py        User-Agent and URL checks, response size limits
tests/                   one test module per app module
```

## Known limitations

- Distances are straight lines between points, not walking routes.
- Caches and rate limits are per process, so the app must run as a single worker.
- The ranking was tuned on Bucharest and spot-checked on Cluj-Napoca. Other cities, especially
  very large ones, may need different caps or radius limits.
- Restaurant results are limited to places with a website or a Wikidata link, to keep the
  Overpass response small. Good restaurants without either are missed.
- Opening hours and prices are not used, so a plan may include a place that is closed that day.
- The public OpenStreetMap servers are not meant for commercial traffic.

## Planned improvements

Roughly in the order I would do them. Most come from things observed while building and testing
the current version.

### Before any public deployment

- **Protect the endpoint.** Every itinerary costs a Claude call and spends part of the shared
  OpenStreetMap quota, so an open endpoint is a cost and abuse risk. Add API keys or another form
  of authentication, a per-client rate limit, and a monthly spend cap on the Anthropic account.
- **Shared cache and rate limiter.** Move the caches and the OSM request spacing from process
  memory to Redis or similar. That removes the single-worker restriction and keeps cached cities
  across restarts.
- **Own OSM services.** The Nominatim and Overpass URLs are already configurable. For commercial
  traffic, point them at a self-hosted instance or a paid provider, as both usage policies ask.
- **Container and CI.** A Dockerfile, plus a pipeline that runs the tests and a mutation testing
  tool on every change, so the mutation checks currently done by hand become automatic.

### Route quality

- **Walking routes.** Replace straight-line distances with real walking distances and times from
  OSRM or OpenRouteService. Aerial distance understates the walk wherever a river, railway or
  large park is in the way.
- **Stop order computed in code.** The model groups places into days; a small solver could then
  order each day's 2 to 5 stops for the shortest walk. With so few stops an exact answer is
  cheap, and it takes one more decision away from the model.
- **Restaurants near each day's route.** Restaurants are currently chosen city-wide, before the
  days exist. A second pass could fetch restaurants within a few hundred metres of each day's
  stops, which would also cover good restaurants that have no website and are filtered out today.
- **Opening hours.** OSM has an `opening_hours` tag for many places. With travel dates in the
  request, closed places could be filtered out before planning. Prices are not in OSM, so budget
  stays a soft hint.

### Data coverage and ranking

- **Evaluation set across cities.** Save the Overpass responses for ten or so cities of different
  size and language, and keep the ranked results as fixtures. Any change to the scoring then
  shows its effect on every city instead of one. The current ranking was tuned on Bucharest and spot-checked
  on Cluj-Napoca only.
- **Large cities.** The 8 km radius cap and the per-group limits suit a city the size of
  Bucharest. Cities like Paris or London need several search centres, one per district, or a
  user-chosen area.
- **Interests that change the data, not only the choice.** Today interests only guide the model's
  choice among the 30 places. Mapping them to OSM tags (art to galleries, nature to parks and
  viewpoints) would change which places are fetched in the first place.
- **Better duplicate detection.** The same-building check uses names, distance and a word list in
  English and Romanian. Matching shared Wikidata IDs would be language independent and more
  reliable where the tag exists.

### Cost and model choice

- **Model comparison.** Claude Haiku is about 20 times cheaper than Sonnet per token. Run both on a
  fixed set of requests and score the results in code: kilometres walked per day, retries,
  latency and cost. Switching is one setting in `.env`.
- **Prompt caching.** The system prompt is the same for every request. Once the prompt is long
  enough to qualify, caching it would lower the input cost of each call.

### Product

- **Frontend with a map.** Show each day as a route on an OpenStreetMap map, with the attribution
  visible. This needs CORS configured for the frontend's address and a tile provider whose usage
  policy fits the expected traffic.
- **Faster first response.** An uncached city takes 20 to 25 seconds, mostly the Overpass query.
  Return a job ID and let the client poll, or stream progress, instead of holding one request
  open. Pre-warming the cache for popular cities would make most requests take about 4 seconds.
- **Observability.** Structured logs with a request ID, plus metrics per stage (geocoding,
  Overpass, model) for latency, retries and token cost, so slow or expensive requests can be
  found without reading raw logs.