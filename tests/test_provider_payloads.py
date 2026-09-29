"""Provider payload hardening and status-code semantics.

Every malformed-but-HTTP-200 body, every impossible value, and every
"no route" answer from OSRM/ORS must surface as the documented status (502 for
an unusable provider response, 422 for an unroutable trip) -- never as a 500 --
and the JSON API and the map page must agree.
"""

import json

import pytest
import responses
from django.core.cache import cache
from django.test import Client, override_settings

from routing.client import get_route
from routing.providers import UNROUTABLE_MESSAGE, ORSProvider
from stations.models import FuelStation
from trips.geo import METERS_PER_MILE

API = "/api/v1/route-fuel-plan/"
MAP = "/api/v1/route-fuel-plan/map/?start=40,-90&finish=40,-80"
START, FINISH = {"lat": 40.0, "lng": -90.0}, {"lat": 40.0, "lng": -80.0}
COORDS = {"start": START, "finish": FINISH}

OSRM_BASE, ORS_BASE = "http://osrm.test", "http://ors.test"
OSRM_URL = f"{OSRM_BASE}/route/v1/driving/-90.0,40.0;-80.0,40.0"
ORS_URL = f"{ORS_BASE}/v2/directions/driving-car/geojson"
OSRM_SETTINGS = {"ROUTING_PROVIDER": "osrm", "ORS_API_KEY": "", "OSRM_BASE_URL": OSRM_BASE}
ORS_SETTINGS = {"ROUTING_PROVIDER": "ors", "ORS_API_KEY": "testkey", "ORS_BASE_URL": ORS_BASE}

GOOD_COORDS = [[-90.0, 40.0], [-85.0, 40.0], [-80.0, 40.0]]
GOOD_DISTANCE_M = 530.0 * METERS_PER_MILE


def osrm_body(coords=GOOD_COORDS, distance=GOOD_DISTANCE_M, duration=36000, code="Ok"):
    return {
        "code": code,
        "routes": [
            {
                "distance": distance,
                "duration": duration,
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        ],
    }


def ors_body(coords=GOOD_COORDS, distance=GOOD_DISTANCE_M, duration=36000):
    return {
        "features": [
            {
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"summary": {"distance": distance, "duration": duration}},
            }
        ]
    }


def post(body=COORDS):
    """POST through the real HTTP stack; an unhandled exception shows up as a 500
    status instead of being re-raised, so it can be asserted against."""
    return Client(raise_request_exception=False).post(
        API, data=json.dumps(body), content_type="application/json"
    )


@pytest.fixture
def stations_on_route(db):
    for i, (lng, price) in enumerate([(-89.0, 3.4), (-87.0, 3.1), (-85.0, 2.95), (-81.0, 3.05)], 1):
        FuelStation.objects.create(
            opis_id=i,
            name=f"STOP {i}",
            address="",
            city="X",
            state="IL",
            retail_price=price,
            latitude=40.0,
            longitude=lng,
            is_geocoded=True,
        )


# --- OSRM: bodies that are not usable -> 502 --------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["null", "[]", '"a string"', "42", "<html>proxy error page</html>", "{}"],
    ids=["null", "list", "string", "number", "html", "empty-object"],
)
def test_osrm_non_route_bodies_are_502(raw):
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, body=raw, status=200, content_type="application/json")
        resp = post()
    assert resp.status_code == 502, (resp.status_code, resp.content[:200])
    assert "Routing provider error" in resp.json()["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"code": "Ok", "routes": []},
        {"code": "Ok"},
        {"code": "Ok", "routes": "not-a-list"},
        {"code": "Ok", "routes": [{}]},
        {"code": "Ok", "routes": [None]},
        osrm_body(coords=[]),
        osrm_body(coords=[[-90.0, 40.0]]),
        osrm_body(coords="not-a-list"),
        osrm_body(coords=[[-90.0, 40.0], [-80.0]]),
        osrm_body(coords=[["a", "b"], ["c", "d"]]),
        osrm_body(coords=[[-200.0, 40.0], [-80.0, 40.0]]),
        osrm_body(coords=[[-90.0, 95.0], [-80.0, 40.0]]),
        osrm_body(coords=[[-90.0, 40.0], [-80.0, 40.0]], distance="abc"),
        osrm_body(distance=None),
        osrm_body(distance=0),
        osrm_body(distance=-100000),
        osrm_body(duration=None),
        osrm_body(duration=-1),
    ],
    ids=[
        "routes-empty",
        "routes-missing",
        "routes-not-list",
        "route-empty",
        "route-null",
        "coords-empty",
        "coords-single-point",
        "coords-string",
        "coords-ragged",
        "coords-non-numeric",
        "lng-out-of-range",
        "lat-out-of-range",
        "distance-string",
        "distance-null",
        "distance-zero",
        "distance-negative",
        "duration-null",
        "duration-negative",
    ],
)
def test_osrm_unusable_payloads_are_502(body):
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json=body, status=200)
        resp = post()
    assert resp.status_code == 502, (resp.status_code, resp.content[:200])


def test_osrm_nan_coordinates_are_502():
    raw = json.dumps(osrm_body(coords=[[-90.0, 40.0], [-80.0, 40.0]])).replace("-80.0", "NaN")
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, body=raw, status=200, content_type="application/json")
        resp = post()
    assert resp.status_code == 502


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_osrm_http_errors_are_502(status):
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json={"message": "nope"}, status=status)
        resp = post()
    assert resp.status_code == 502
    assert f"OSRM HTTP {status}" in resp.json()["error"]


# --- OSRM: no drivable route -> 422 with a clear message ---------------------------


@pytest.mark.parametrize(
    "status,code",
    [(400, "NoRoute"), (400, "NoSegment"), (200, "NoRoute")],
    ids=["http400-NoRoute", "http400-NoSegment", "http200-NoRoute"],
)
def test_osrm_no_route_is_422(status, code):
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(
            responses.GET,
            OSRM_URL,
            status=status,
            json={"code": code, "message": "Impossible route between points"},
        )
        resp = post()
    assert resp.status_code == 422
    assert resp.json() == {"error": UNROUTABLE_MESSAGE, "code": "unroutable"}


def test_osrm_other_400_codes_stay_502():
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, status=400, json={"code": "InvalidQuery"})
        resp = post()
    assert resp.status_code == 502
    assert "OSRM HTTP 400 (InvalidQuery)" in resp.json()["error"]


# --- OSRM: tolerated shapes ---------------------------------------------------------


@pytest.mark.django_db
def test_osrm_3d_coordinates_are_accepted_and_flattened(stations_on_route):
    coords_3d = [[-90.0, 40.0, 180.5], [-85.0, 40.0, 190.0], [-80.0, 40.0, 200.25]]
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json=osrm_body(coords=coords_3d), status=200)
        resp = post()
    assert resp.status_code == 200, resp.content[:200]
    assert resp.json()["route"]["geometry"]["coordinates"] == GOOD_COORDS
    assert resp.json()["fuel_stops"]


def test_get_route_normalises_points_from_3d_geometry():
    coords_3d = [[-90.0, 40.0, 1.0], [-80.0, 40.0, 2.0]]
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json=osrm_body(coords=coords_3d), status=200)
        result = get_route(START, FINISH)
    assert result.points == [(40.0, -90.0), (40.0, -80.0)]
    assert result.geometry["coordinates"] == [[-90.0, 40.0], [-80.0, 40.0]]
    assert result.total_distance_miles == pytest.approx(530.0)


# --- ORS --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (404, {"error": {"code": 2009, "message": "Route could not be found"}}, 422),
        (404, {"error": {"code": 2010, "message": "Could not find routable point"}}, 422),
        (404, {"error": {"code": 2004, "message": "Request exceeds limits"}}, 502),
        (401, {"error": {"code": 401, "message": "bad key"}}, 502),
        (429, "quota", 502),
        (503, None, 502),
    ],
    ids=["2009-unroutable", "2010-unroutable", "2004-limits", "401", "429-text", "503-empty"],
)
def test_ors_error_statuses(status, body, expected):
    with responses.RequestsMock() as rsps, override_settings(**ORS_SETTINGS):
        kw = {"json": body} if isinstance(body, dict) else {"body": body or ""}
        rsps.add(responses.POST, ORS_URL, status=status, **kw)
        resp = post()
    assert resp.status_code == expected, (resp.status_code, resp.content[:200])
    if expected == 422:
        assert resp.json() == {"error": UNROUTABLE_MESSAGE, "code": "unroutable"}


@pytest.mark.parametrize(
    "raw",
    [
        "null",
        "[]",
        '"str"',
        "<html></html>",
        "{}",
        json.dumps({"features": None}),
        json.dumps({"features": [None]}),
        json.dumps({"features": [{"geometry": {"coordinates": GOOD_COORDS}}]}),
        json.dumps(
            {
                "features": [
                    {
                        "geometry": {"coordinates": GOOD_COORDS},
                        "properties": {"summary": {"distance": "abc", "duration": 1}},
                    }
                ]
            }
        ),
        json.dumps(ors_body(coords=[[-90.0, 40.0]])),
        json.dumps(ors_body(distance=0)),
    ],
    ids=[
        "null",
        "list",
        "string",
        "html",
        "empty",
        "features-null",
        "feature-null",
        "summary-missing",
        "distance-string",
        "single-point",
        "distance-zero",
    ],
)
def test_ors_unusable_payloads_are_502(raw):
    with responses.RequestsMock() as rsps, override_settings(**ORS_SETTINGS):
        rsps.add(responses.POST, ORS_URL, body=raw, status=200, content_type="application/json")
        resp = post()
    assert resp.status_code == 502, (resp.status_code, resp.content[:200])


@pytest.mark.django_db
def test_ors_3d_coordinates_are_accepted(stations_on_route):
    coords_3d = [[c[0], c[1], 100.0] for c in GOOD_COORDS]
    with responses.RequestsMock() as rsps, override_settings(**ORS_SETTINGS):
        rsps.add(responses.POST, ORS_URL, json=ors_body(coords=coords_3d), status=200)
        resp = post()
    assert resp.status_code == 200, resp.content[:200]
    assert resp.json()["route"]["geometry"]["coordinates"] == GOOD_COORDS


# --- ORS geocode fallback never raises on garbage -----------------------------------


@pytest.mark.parametrize(
    "raw,expect_hit",
    [
        ("null", False),
        ("[]", False),
        ('"str"', False),
        (json.dumps({"features": "x"}), False),
        (json.dumps({"features": [[]]}), False),
        (json.dumps({"features": [{"properties": "x"}]}), False),
        (
            json.dumps(
                {
                    "features": [
                        {
                            "properties": {"confidence": 0.9, "layer": "locality"},
                            "geometry": {"coordinates": "ab"},
                        }
                    ]
                }
            ),
            False,
        ),
        (
            json.dumps(
                {
                    "features": [
                        {
                            "properties": {"confidence": 0.9, "layer": "locality", "label": "L"},
                            "geometry": {"coordinates": [-80.0, 40.5, 12.0]},
                        }
                    ]
                }
            ),
            True,
        ),
    ],
    ids=[
        "null",
        "list",
        "string",
        "features-string",
        "feature-list",
        "props-string",
        "coords-string",
        "coords-3d-accepted",
    ],
)
def test_ors_geocode_garbage_returns_none_never_raises(raw, expect_hit):
    provider = ORSProvider(api_key="testkey", base_url=ORS_BASE)
    with responses.RequestsMock() as rsps, override_settings(ORS_GEOCODE_MIN_CONFIDENCE=0.5):
        rsps.add(
            responses.GET,
            f"{ORS_BASE}/geocode/search",
            body=raw,
            status=200,
            content_type="application/json",
        )
        hit = provider.geocode("Somewhere, XX")
    assert (hit is not None) is expect_hit
    if hit:
        assert (hit.lat, hit.lng, hit.label) == (40.5, -80.0, "L")


# --- The map page agrees with the API -----------------------------------------------


@pytest.mark.django_db
def test_map_and_api_agree_on_provider_outage_and_unroutable():
    client = Client(raise_request_exception=False)
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json={"message": "down"}, status=503)
        api, page = post(), client.get(MAP)
    assert (api.status_code, page.status_code) == (502, 502)
    assert "Could not render map" in page.content.decode()

    cache.clear()  # the 503 above is remembered for ROUTING_FAILURE_CACHE_SECONDS
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json={"code": "NoRoute"}, status=400)
        api, page = post(), client.get(MAP)
    assert (api.status_code, page.status_code) == (422, 422)
    assert UNROUTABLE_MESSAGE in page.content.decode()


@pytest.mark.django_db
def test_empty_geometry_is_a_provider_error_not_a_misleading_422():
    """Before: an empty geometry fell through to 'No fuel station found near the
    route... increase buffer_miles' -- blaming the client for a provider fault."""
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json=osrm_body(coords=[]), status=200)
        resp = post()
    assert resp.status_code == 502
    assert "buffer_miles" not in resp.content.decode()


# --- ORS request shape ----------------------------------------------------------------


def test_ors_request_snaps_endpoints_to_the_nearest_road_without_a_radius_limit():
    """ORS's default 350 m snapping radius rejects small-town centroids as
    'not routable' (error 2010); radiuses=-1 lifts the limit, like OSRM."""
    with responses.RequestsMock() as rsps, override_settings(**ORS_SETTINGS):
        rsps.add(responses.POST, ORS_URL, json=ors_body(), status=200)
        get_route(START, FINISH)
        body = json.loads(rsps.calls[0].request.body)
    assert body["coordinates"] == [[-90.0, 40.0], [-80.0, 40.0]]
    assert body["radiuses"] == [-1, -1]
