"""Tests for the rendered Leaflet map page (``/api/v1/route-fuel-plan/map/``).

The page embeds its data with Django's ``json_script`` filter and the inline
script does ``JSON.parse(textContent)`` exactly once per block, so each block
must decode to an object/array. A regression here (e.g. pre-dumping the data to
a JSON string in the view) renders a blank map with a JavaScript TypeError.
"""

import json
import re
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from routing.client import RouteResult
from routing.exceptions import RouteProviderError
from routing.providers import OSRMProvider
from stations.models import FuelStation
from trips.geo import cumulative_miles

API = "/api/v1/route-fuel-plan/"
MAP = "/api/v1/route-fuel-plan/map/"
MAP_URL = f"{MAP}?start=40,-90&finish=40,-80"

ROUTE_POINTS = [(40.0, -90.0), (40.0, -80.0)]
ROUTE_LEN = cumulative_miles(ROUTE_POINTS)[-1]

_JSON_SCRIPT = re.compile(
    r'<script id="(?P<id>[\w-]+)" type="application/json">(?P<body>.*?)</script>', re.S
)


def _fake_route(*args, **kwargs):
    return RouteResult(
        geometry={"type": "LineString", "coordinates": [[lng, lat] for lat, lng in ROUTE_POINTS]},
        points=ROUTE_POINTS,
        total_distance_miles=ROUTE_LEN,
        duration_minutes=600.0,
        provider="mock",
        start=ROUTE_POINTS[0],
        finish=ROUTE_POINTS[-1],
    )


def _json_blocks(html: str) -> dict:
    """Decode every json_script block ONCE, exactly as the page's JS does."""
    return {m.group("id"): json.loads(m.group("body")) for m in _JSON_SCRIPT.finditer(html)}


def _make_station(opis_id, lng, price, name=None, lat=40.0):
    return FuelStation.objects.create(
        opis_id=opis_id,
        name=name or f"STOP {opis_id}",
        address="",
        city="X",
        state="IL",
        retail_price=price,
        latitude=lat,
        longitude=lng,
        is_geocoded=True,
    )


@pytest.fixture
def stations_on_route(db):
    specs = [(-89.0, 3.40), (-87.0, 3.10), (-85.0, 2.95), (-83.0, 3.30), (-81.0, 3.05)]
    for i, (lng, price) in enumerate(specs, start=1):
        _make_station(i, lng, price)


@pytest.mark.django_db
def test_map_json_blocks_decode_to_objects(stations_on_route):
    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = APIClient().get(MAP_URL)
    assert resp.status_code == 200
    blocks = _json_blocks(resp.content.decode())
    assert set(blocks) == {"geometry-data", "stops-data", "summary-data"}

    geometry, stops, summary = blocks["geometry-data"], blocks["stops-data"], blocks["summary-data"]
    assert isinstance(geometry, dict), f"geometry decodes to {type(geometry).__name__}"
    assert isinstance(stops, list), f"stops decodes to {type(stops).__name__}"
    assert isinstance(summary, dict), f"summary decodes to {type(summary).__name__}"

    # The exact fields the inline script reads.
    assert geometry["type"] == "LineString"
    assert geometry["coordinates"] == [[-90.0, 40.0], [-80.0, 40.0]]
    assert stops and stops[0]["order"] == 1
    assert {"lat", "lng"} <= set(stops[0]["location"])
    for key in (
        "name",
        "city",
        "state",
        "route_mile_marker",
        "price_per_gallon",
        "gallons_purchased",
        "cost_usd",
    ):
        assert key in stops[0]
    assert summary["route"]["total_distance_miles"] == round(ROUTE_LEN, 1)
    assert summary["route"]["provider"] == "mock"
    assert "geometry" not in summary["route"]  # not duplicated into the panel data
    for key in ("total_gallons", "total_cost_usd", "mpg", "tank_range_miles"):
        assert key in summary["fuel"]


@pytest.mark.django_db
def test_map_matches_api_plan(stations_on_route):
    """The page must show the same plan the JSON API returns for the same inputs."""
    client = APIClient()
    with patch("trips.services.get_route", side_effect=_fake_route):
        api = client.post(
            API,
            {"start": {"lat": 40.0, "lng": -90.0}, "finish": {"lat": 40.0, "lng": -80.0}},
            format="json",
        ).json()
        page = client.get(MAP_URL)
    blocks = _json_blocks(page.content.decode())
    assert blocks["stops-data"] == api["fuel_stops"]
    assert blocks["geometry-data"] == api["route"]["geometry"]
    assert blocks["summary-data"]["fuel"] == api["fuel"]


@pytest.mark.django_db
def test_map_url_from_api_response_renders(stations_on_route):
    """Following the ``map_url`` the API hands back yields a working page."""
    client = APIClient()
    with patch("trips.services.get_route", side_effect=_fake_route):
        api = client.post(
            API,
            {"start": "40,-90", "finish": {"lat": 40.0, "lng": -80.0}, "buffer_miles": 9},
            format="json",
        )
        assert api.status_code == 200
        map_url = api.json()["route"]["map_url"]
        assert map_url.startswith("http://testserver/api/v1/route-fuel-plan/map/?")
        assert "buffer_miles=9" in map_url
        page = client.get(map_url.removeprefix("http://testserver"))
    assert page.status_code == 200
    html = page.content.decode()
    assert 'id="map"' in html
    assert "Could not render map" not in html
    assert isinstance(_json_blocks(html)["geometry-data"], dict)


@pytest.mark.django_db
def test_map_escapes_html_in_station_data(stations_on_route):
    """``json_script`` must neutralise ``</script>`` in data so a hostile station
    name cannot break out of the JSON block."""
    _make_station(99, -88.0, 1.50, name='</script><img src=x onerror="alert(1)">')
    with patch("trips.services.get_route", side_effect=_fake_route):
        html = APIClient().get(MAP_URL).content.decode()
    assert "</script><img" not in html
    assert "\\u003C/script\\u003E" in html
    # ...and it round-trips intact after decoding.
    names = [s["name"] for s in _json_blocks(html)["stops-data"]]
    assert '</script><img src=x onerror="alert(1)">' in names


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query",
    [
        "",
        "start=40,-90",
        f"{MAP_URL[len(MAP) + 1 :]}&buffer_miles=abc",
        f"{MAP_URL[len(MAP) + 1 :]}&buffer_miles=999",
        f"{MAP_URL[len(MAP) + 1 :]}&buffer_miles=0.4",
    ],
    ids=["no-params", "missing-finish", "buffer-nan-text", "buffer-too-big", "buffer-too-small"],
)
def test_map_invalid_input_is_400_error_page(query):
    resp = APIClient().get(f"{MAP}?{query}")
    assert resp.status_code == 400
    html = resp.content.decode()
    assert "Could not render map" in html
    assert 'id="map"' not in html  # the error branch has no map/JSON blocks


@pytest.mark.django_db
def test_map_infeasible_plan_is_422_error_page():
    _make_station(1, -89.9, 3.0)  # only near the start: the rest of the route has no station
    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = APIClient().get(MAP_URL)
    assert resp.status_code == 422
    assert "Could not render map" in resp.content.decode()


@pytest.mark.django_db
def test_map_without_any_stations_is_a_503_error_page():
    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = APIClient().get(MAP_URL)
    assert resp.status_code == 503
    assert "load_fuel_prices" in resp.content.decode()


def test_map_provider_failure_is_error_page(db):
    with patch("trips.services.get_route", side_effect=RouteProviderError("upstream down")):
        resp = APIClient().get(MAP_URL)
    assert resp.status_code >= 400
    assert "upstream down" in resp.content.decode()


def test_map_error_message_is_html_escaped(db):
    # Keyless provider: the unknown place name fails offline (no geocoder fallback),
    # so the real resolution error path runs without any network access.
    with patch("routing.client.get_provider", return_value=OSRMProvider()):
        resp = APIClient().get(f"{MAP}?start=<script>alert(1)</script>,ZZ&finish=Houston, TX")
    assert resp.status_code == 400
    html = resp.content.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
