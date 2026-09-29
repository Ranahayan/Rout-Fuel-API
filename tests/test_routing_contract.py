"""Routing-layer contract the README relies on: one upstream call per distinct
trip, zero on repeats (including a different buffer), zero for place names the
offline geocoder knows, same-endpoint detection at ~11 m, disclosure of fuzzy
geocode results, and the ORS geocoder's accept/reject rules."""

import re
from unittest.mock import MagicMock, patch

import pytest
import responses
from django.test import override_settings
from rest_framework.test import APIClient

from routing.client import get_route
from routing.providers import GeocodeHit, ORSProvider, ProviderRoute, RouteProvider
from stations.models import FuelStation
from trips.geo import METERS_PER_MILE
from trips.services import plan_trip

API = "/api/v1/route-fuel-plan/"
START, FINISH = {"lat": 40.0, "lng": -90.0}, {"lat": 40.0, "lng": -80.0}
OSRM_BASE, ORS_BASE = "http://osrm.test", "http://ors.test"
OSRM_URL = f"{OSRM_BASE}/route/v1/driving/-90.0,40.0;-80.0,40.0"
OSRM_SETTINGS = {"ROUTING_PROVIDER": "osrm", "ORS_API_KEY": "", "OSRM_BASE_URL": OSRM_BASE}
DIST = 530.0 * METERS_PER_MILE
OSRM_OK = {
    "code": "Ok",
    "routes": [
        {
            "distance": DIST,
            "duration": 36000,
            "geometry": {"coordinates": [[-90.0, 40.0], [-80.0, 40.0]]},
        }
    ],
}


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


def test_route_cache_hit_makes_no_second_http_call():
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json=OSRM_OK, status=200)
        first = get_route(START, FINISH)
        second = get_route(START, FINISH)
        assert len(rsps.calls) == 1
    assert first.total_distance_miles == second.total_distance_miles == pytest.approx(530.0)


@pytest.mark.django_db
def test_plan_and_route_caches_share_one_upstream_call(stations_on_route):
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, OSRM_URL, json=OSRM_OK, status=200)
        a = plan_trip(START, FINISH)  # the one HTTP call
        b = plan_trip(START, FINISH)  # plan-cache hit
        c = plan_trip(START, FINISH, 9.0)  # new plan key, route-cache hit
        assert len(rsps.calls) == 1
    assert a["fuel"]["total_cost_usd"] == b["fuel"]["total_cost_usd"]
    assert c["route"]["total_distance_miles"] == a["route"]["total_distance_miles"]


@pytest.mark.django_db
def test_known_place_names_cost_exactly_one_routing_call_and_no_geocode(db):
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(responses.GET, re.compile(rf"{OSRM_BASE}/route/v1/driving/.*"), json=OSRM_OK)
        resp = APIClient().post(
            API, {"start": "Chicago, IL", "finish": "Houston, TX"}, format="json"
        )
        assert len(rsps.calls) == 1
        assert "geocode" not in rsps.calls[0].request.url
    assert resp.status_code in (
        200,
        422,
        503,
    )  # planning outcome depends on test data; the budget is what matters


def test_same_endpoint_is_detected_within_11m_rounding(db):
    provider = MagicMock(spec=RouteProvider)
    provider.name = "mock"
    provider.route.return_value = ProviderRoute(
        coordinates=[[-90.0, 40.0], [-90.0, 40.001]], distance_meters=120.0, duration_seconds=10.0
    )
    with patch("routing.client.get_provider", return_value=provider):
        exact = APIClient().post(API, {"start": START, "finish": START}, format="json")
        near = APIClient().post(
            API, {"start": START, "finish": {"lat": 40.00004, "lng": -90.00004}}, format="json"
        )
        apart = APIClient().post(
            API, {"start": START, "finish": {"lat": 40.001, "lng": -90.0}}, format="json"
        )
    assert exact.status_code == near.status_code == 400
    assert exact.json()["code"] == "same_endpoint"
    assert apart.status_code != 400 or apart.json()["code"] != "same_endpoint"
    assert provider.route.call_count == 1  # only the genuinely different pair went upstream


@pytest.mark.django_db
def test_fuzzy_geocode_result_is_disclosed_in_the_response(stations_on_route):
    class FakeORS(RouteProvider):
        name = "fake-ors"

        def geocode(self, text, deadline=None):
            return GeocodeHit(lat=40.0, lng=-90.0, label="Resolved Place, XX, USA", confidence=0.9)

        def route(self, start, end, deadline=None):
            return ProviderRoute(
                coordinates=[[-90.0, 40.0], [-80.0, 40.0]],
                distance_meters=DIST,
                duration_seconds=36000,
            )

    with patch("routing.client.get_provider", return_value=FakeORS()):
        resp = APIClient().post(
            API, {"start": "Nowheresvillexyz, ZZ", "finish": FINISH}, format="json"
        )
    assert resp.status_code == 200, resp.content[:200]
    route = resp.json()["route"]
    assert route["start"]["resolved_from"] == "Resolved Place, XX, USA"
    assert "resolved_from" not in route["finish"]  # exact coordinates: nothing to disclose


def _pelias(props, coords=(-80.0, 40.5)):
    return {
        "features": [
            {"geometry": {"type": "Point", "coordinates": list(coords)}, "properties": props}
        ]
    }


@pytest.mark.parametrize(
    "props,expect_hit",
    [
        (
            {
                "confidence": 0.9,
                "match_type": "exact",
                "layer": "locality",
                "label": "Toronto, OH, USA",
            },
            True,
        ),
        ({"confidence": 0.9, "match_type": "exact", "layer": "county", "label": "X County"}, True),
        ({"confidence": 0.3, "match_type": "exact", "layer": "locality"}, False),
        ({"confidence": 0.9, "match_type": "fallback", "layer": "locality"}, False),
        ({"confidence": 0.9, "match_type": "exact", "layer": "venue"}, False),
        ({"confidence": 0.9, "match_type": "exact", "layer": "region"}, False),
        ({"confidence": 0.9, "match_type": "exact", "layer": "country"}, False),
        ({}, False),
    ],
    ids=["locality", "county", "low-confidence", "fallback", "venue", "region", "country", "empty"],
)
def test_ors_geocode_accept_and_reject_rules(props, expect_hit):
    provider = ORSProvider(api_key="testkey", base_url=ORS_BASE)
    with responses.RequestsMock() as rsps, override_settings(ORS_GEOCODE_MIN_CONFIDENCE=0.5):
        rsps.add(responses.GET, f"{ORS_BASE}/geocode/search", json=_pelias(props), status=200)
        hit = provider.geocode("Toronto, ON")
        assert "boundary.country=US" in rsps.calls[0].request.url  # US-only search
    assert (hit is not None) is expect_hit
    if hit:
        assert (hit.lat, hit.lng, hit.label) == (40.5, -80.0, props["label"])


def test_ors_geocode_without_a_key_makes_no_call():
    with responses.RequestsMock() as rsps:
        assert ORSProvider(api_key="", base_url=ORS_BASE).geocode("Anywhere, XX") is None
        assert len(rsps.calls) == 0
