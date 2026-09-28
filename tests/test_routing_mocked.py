"""Routing client tests with the external HTTP API mocked.

Asserts the 1-call budget, meters->miles conversion, provider auto-selection,
and error handling -- with no real network access.
"""

import pytest
import requests
import responses
from django.test import override_settings

from routing.client import get_route
from routing.exceptions import OutsideUSAError, RouteProviderError

START = {"lat": 40.0, "lng": -90.0}
FINISH = {"lat": 30.0, "lng": -95.0}
HUNDRED_MI_M = 160934.4  # 100 miles in meters

OSRM_BASE = "http://osrm.test"
ORS_BASE = "http://ors.test"

OSRM_BODY = {
    "code": "Ok",
    "routes": [
        {
            "distance": HUNDRED_MI_M,
            "duration": 3600,
            "geometry": {"type": "LineString", "coordinates": [[-90.0, 40.0], [-95.0, 30.0]]},
        }
    ],
}
ORS_BODY = {
    "features": [
        {
            "geometry": {"type": "LineString", "coordinates": [[-90.0, 40.0], [-95.0, 30.0]]},
            "properties": {"summary": {"distance": HUNDRED_MI_M, "duration": 3600}},
        }
    ],
}


@responses.activate
@override_settings(ROUTING_PROVIDER="", ORS_API_KEY="", OSRM_BASE_URL=OSRM_BASE)
def test_osrm_single_call_and_units():
    responses.add(
        responses.GET,
        f"{OSRM_BASE}/route/v1/driving/-90.0,40.0;-95.0,30.0",
        json=OSRM_BODY,
        status=200,
    )
    result = get_route(START, FINISH)
    assert result.provider == "osrm"
    assert result.total_distance_miles == pytest.approx(100.0)
    assert result.duration_minutes == pytest.approx(60.0)
    assert len(responses.calls) == 1  # exactly one routing call (coords given)


@responses.activate
@override_settings(ROUTING_PROVIDER="", ORS_API_KEY="testkey", ORS_BASE_URL=ORS_BASE)
def test_ors_used_when_key_present():
    responses.add(
        responses.POST,
        f"{ORS_BASE}/v2/directions/driving-car/geojson",
        json=ORS_BODY,
        status=200,
    )
    result = get_route(START, FINISH)
    assert result.provider == "ors"
    assert result.total_distance_miles == pytest.approx(100.0)
    assert len(responses.calls) == 1


@responses.activate
@override_settings(ROUTING_PROVIDER="", ORS_API_KEY="", OSRM_BASE_URL=OSRM_BASE)
def test_provider_error_raised_on_connection_failure():
    responses.add(
        responses.GET,
        f"{OSRM_BASE}/route/v1/driving/-90.0,40.0;-95.0,30.0",
        body=requests.exceptions.ConnectionError("boom"),
    )
    with pytest.raises(RouteProviderError):
        get_route(START, FINISH)


@override_settings(ROUTING_PROVIDER="", ORS_API_KEY="", OSRM_BASE_URL=OSRM_BASE)
def test_outside_usa_rejected_before_any_call():
    # Paris coordinates -> rejected by the USA bounds check (no HTTP call).
    # (Note: a rectangular bbox cannot separate e.g. Toronto from Buffalo;
    #  place-name endpoints are constrained to US cities by the offline geocoder.)
    with pytest.raises(OutsideUSAError):
        get_route({"lat": 48.85, "lng": 2.35}, FINISH)
