# USA Fuel-Optimal Route API

A Django REST API that, given a **start** and **finish** in the USA, returns the
driving route, the **cost-optimal fuel stops** along it, and the **total fuel
cost** — for a vehicle with a **500-mile range** at **10 mpg**, using truck-stop
prices from `fuel-prices-for-be-assessment.csv`.

It makes **exactly one** call to a free routing API per request (cached
thereafter), and returns results in **milliseconds** once warm.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # OPTIONAL — runs key-free without it
python manage.py migrate
python manage.py load_fuel_prices    # parses, dedups, geocodes the CSV (one-time)
python manage.py runserver
```

No API key and no external database are required: it defaults to the keyless
public **OSRM** routing server and **SQLite**. To use the more robust
**OpenRouteService**, put a free `ORS_API_KEY` in `.env`.

### Try it

```bash
# JSON POST (place names or coordinates)
curl -s -X POST localhost:8000/api/v1/route-fuel-plan/ \
  -H 'Content-Type: application/json' \
  -d '{"start":"Chicago, IL","finish":"Houston, TX"}' | python -m json.tool

# GET form
curl -s 'localhost:8000/api/v1/route-fuel-plan/?start=Chicago,%20IL&finish=Houston,%20TX'
```

The response's `route.map_url` opens an interactive **Leaflet map** of the route
with the fuel stops plotted.

### Run the tests

```bash
pytest
```

---

## How it works

The CSV has **no coordinates** — only `City, State` and a free-text highway
`Address` — and the routing API must not be called repeatedly. The design
resolves this by doing all geocoding **offline, once, at load time**:

1. **Load (`load_fuel_prices`)** — parse the CSV (handling CRLF + quoted commas),
   drop the ~620 Canadian-province rows, dedup the ~6,700 stations by OPIS ID
   (keeping the lowest of any conflicting prices), and geocode each `(City,
   State)` against a vendored US-cities centroid dataset (`data/uscities.csv`).
   Coordinates are stored on the `FuelStation` table. **~99.8%** of stations
   geocode; the rest are kept but excluded from routing. Re-running is idempotent.
2. **Per request** — make **one** routing call (OpenRouteService or OSRM) for the
   route geometry + distance. Endpoints given as `"City, ST"` are resolved
   offline, so the typical request is a single API call.
3. **Corridor** — a scipy KD-tree (built once from the table) prefilters stations
   near the route; exact membership uses true perpendicular (haversine) distance
   to the route polyline, and each station gets a "mile marker" distance from the
   start.
4. **Optimize** — the classic minimum-cost **gas-station problem** is solved with
   a provably-optimal greedy (verified against a linear program in the tests):
   at each stop, buy just enough to reach the next cheaper station within range,
   otherwise fill the tank and jump to the cheapest reachable station.

### API response shape

```json
{
  "route": {
    "geometry": { "type": "LineString", "coordinates": [[lng, lat], ...] },
    "total_distance_miles": 1100.0,
    "duration_minutes": 1198.0,
    "provider": "osrm",
    "start": {"lat": 41.88, "lng": -87.63},
    "finish": {"lat": 29.76, "lng": -95.37},
    "map_url": "http://localhost:8000/api/v1/route-fuel-plan/map/?start=..."
  },
  "fuel": {
    "mpg": 10.0, "tank_range_miles": 500.0, "start_tank_assumption": "empty",
    "total_gallons": 110.0, "total_cost_usd": "323.48"
  },
  "fuel_stops": [
    {
      "order": 1, "opis_id": 12345, "name": "...", "city": "...", "state": "IL",
      "location": {"lat": 40.0, "lng": -88.0}, "route_mile_marker": 212.3,
      "detour_miles": 0.4, "price_per_gallon": "2.999",
      "gallons_purchased": 10.7, "cost_usd": "32.10"
    }
  ]
}
```

### Status codes

| Code | Meaning |
|------|---------|
| 200  | Plan returned |
| 400  | Endpoint unresolvable, outside the USA, or invalid `buffer_miles` |
| 422  | No fueling plan within range (returns the offending mile segment) |
| 429  | Rate limit exceeded (see `ANON_THROTTLE_RATE`) |
| 502  | Routing provider failed |

---

## Configuration (`.env`)

| Variable | Default | Notes |
|----------|---------|-------|
| `ROUTING_PROVIDER` | auto | `ors`, `osrm`, or blank (ORS if a key is set, else OSRM) |
| `ORS_API_KEY` | — | Free key from openrouteservice.org; optional |
| `VEHICLE_RANGE_MILES` | 500 | Tank range |
| `VEHICLE_MPG` | 10 | Fuel economy |
| `CORRIDOR_BUFFER_MILES` | 7 | How far off-route a station may be |
| `DEBUG` | True | Set `False` in production (activates the guards + TLS settings below) |
| `SECRET_KEY` | dev key | **Required** when `DEBUG=False` (boot fails otherwise) |
| `ALLOWED_HOSTS` | localhost | Comma-separated; **required** when `DEBUG=False` |
| `REDIS_URL` | — | When set, the cache + throttle counters are shared across workers |
| `ANON_THROTTLE_RATE` | 30/min | Per-IP request limit on the API endpoint |
| `USER_THROTTLE_RATE` | 120/min | Per-user request limit |
| `LOG_LEVEL` | INFO | Verbosity of the app loggers |

### Production notes

- **Safe by default when `DEBUG=False`.** The app refuses to boot with the
  insecure dev `SECRET_KEY` or an empty `ALLOWED_HOSTS`, and enables
  `SECURE_SSL_REDIRECT`, HSTS, and secure cookies (assumes TLS terminated
  upstream and forwarded via `X-Forwarded-Proto`).
- **Rate limited.** The endpoint triggers an external routing call and CPU work
  on a cache miss, so it is throttled per-IP/per-user (DRF throttling).
- **Shared cache via `REDIS_URL`.** Without it the in-process `LocMemCache` is
  per-worker, so the "one routing call per route" budget holds only within a
  worker; set `REDIS_URL` to share it across a multi-process deployment.
- **OSRM default is a demo server.** `router.project-osrm.org` is fair-use only;
  for real traffic, self-host OSRM or use ORS.

---

## Documented assumptions & limitations

- **Tank starts and finishes empty.** The whole trip's fuel (`distance / mpg`
  gallons) is purchased along the route, so "total money spent on fuel" is the
  trip's full fuel cost. The first stop also covers the (usually negligible)
  origin → first-station lead-in. *(Switching to a "start full" assumption is a
  one-line change in `trips/optimizer.py`.)*
- **City-centroid geocoding.** Stations are placed at their city centroid, not
  the exact highway exit, and several stations in one city share a point. The
  corridor buffer (default 7 mi) absorbs this; selection among same-city
  stations is by price. Parsing the highway/exit from `Address` could refine
  this and is a natural next step.
- **Conflicting duplicate prices** (597 OPIS IDs) are resolved by keeping the
  lowest price.
- **Detour fuel** (driving off-highway to a station) is not counted.
- **"Within USA" check** uses a bounding box; a rectangle cannot perfectly
  separate border cities (e.g. Toronto vs. Buffalo). Place-name endpoints are
  inherently constrained to US cities by the offline geocoder.

## Project layout

```
config/    Django project (settings, urls)
stations/  FuelStation model, offline geocoder, load_fuel_prices command
routing/   provider abstraction (ORS/OSRM) + single-call client
trips/     geo math, KD-tree index, corridor, optimizer, API views, Leaflet map
tests/     optimizer (incl. greedy==LP fuzz), loader, corridor, routing, e2e, error contract
data/      the fuel CSV + vendored uscities.csv
```

## Data attribution

City coordinates: [kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database) (MIT).
Routing/tiles: OpenRouteService / OSRM / OpenStreetMap contributors.
