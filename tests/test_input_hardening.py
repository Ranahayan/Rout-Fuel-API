"""Input limits at the HTTP boundary.

Place names are capped in length, coordinates must be finite and in range, and
``buffer_miles`` must be finite; the map page validates exactly like the API; and
rejected input never reaches the geocoder or the routing provider. Provider
error messages shown to clients carry no URLs, hosts, or library internals.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests
import responses
from django.test import override_settings
from rest_framework.test import APIClient

from routing.providers import RouteProvider
from trips.serializers import PLACE_NAME_MAX_LENGTH

API = "/api/v1/route-fuel-plan/"
MAP = "/api/v1/route-fuel-plan/map/"
FINISH = {"lat": 40.0, "lng": -80.0}
OSRM_SETTINGS = {
    "ROUTING_PROVIDER": "osrm",
    "ORS_API_KEY": "",
    "OSRM_BASE_URL": "http://osrm.internal.test",
}
OSRM_URL = "http://osrm.internal.test/route/v1/driving/-90.0,40.0;-80.0,40.0"


@pytest.fixture
def provider():
    prov = MagicMock(spec=RouteProvider)
    prov.name = "mock"
    prov.geocode.return_value = None
    with patch("routing.client.get_provider", return_value=prov):
        yield prov


def test_long_place_name_is_rejected_before_any_lookup(provider):
    long_name = "A" * (PLACE_NAME_MAX_LENGTH + 1) + ", TX"
    client = APIClient()
    post = client.post(API, {"start": long_name, "finish": FINISH}, format="json")
    get = client.get(f"{API}?start={long_name}&finish=40,-80")
    page = client.get(f"{MAP}?start={long_name}&finish=40,-80")
    assert (post.status_code, get.status_code, page.status_code) == (400, 400, 400)
    assert "too long" in post.json()["fields"]["start"][0]
    assert "too long" in page.content.decode()
    provider.geocode.assert_not_called()
    provider.route.assert_not_called()


def test_place_name_at_the_limit_reaches_resolution(provider):
    name = "A" * (PLACE_NAME_MAX_LENGTH - 4) + ", ZZ"  # exactly the max length
    resp = APIClient().post(API, {"start": name, "finish": FINISH}, format="json")
    assert resp.status_code == 400
    assert "Could not resolve" in resp.json()["error"]  # a resolution failure, not a validation one
    provider.geocode.assert_called_once()


@pytest.mark.parametrize(
    "start",
    [
        {"lat": 95, "lng": -90},
        {"lat": 40, "lng": -181},
        {"lat": "nan", "lng": -90},
        {"lat": 40, "lng": "inf"},
    ],
    ids=["lat-95", "lng--181", "lat-nan", "lng-inf"],
)
def test_bad_coordinates_are_400_with_a_clear_message(provider, start):
    resp = APIClient().post(API, {"start": start, "finish": FINISH}, format="json")
    assert resp.status_code == 400
    message = resp.json()["fields"]["start"][0]
    assert "within" in message or "finite" in message
    provider.route.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "start=nan,inf&finish=40,-80",
        "start=95,-90&finish=40,-80",
        "start=40,-90&finish=40,-80&buffer_miles=nan",
        "start=40,-90&finish=40,-80&buffer_miles=inf",
        "start=40,-90&finish=40,-80&buffer_miles=abc",
    ],
    ids=["nan-coords", "lat-95", "buffer-nan", "buffer-inf", "buffer-text"],
)
def test_bad_get_inputs_are_400_on_api_and_map(provider, query):
    client = APIClient()
    assert client.get(f"{API}?{query}").status_code == 400
    page = client.get(f"{MAP}?{query}")
    assert page.status_code == 400
    assert "Could not render map" in page.content.decode()
    provider.route.assert_not_called()


def test_buffer_nan_is_rejected_on_post(provider):
    body = {"start": {"lat": 40, "lng": -90}, "finish": FINISH, "buffer_miles": "nan"}
    resp = APIClient().post(API, body, format="json")
    assert resp.status_code == 400
    assert "buffer_miles" in resp.json()["fields"]  # DRF's FloatField or validate_buffer_miles


def test_map_page_reports_missing_fields_like_the_api():
    page = APIClient().get(f"{MAP}?start=40,-90")
    assert page.status_code == 400
    assert "finish: This field is required." in page.content.decode()


def test_transport_failure_body_names_the_failure_class_only():
    """URLs and hosts stay in the (redacted) log; the client sees a safe summary."""
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(
            responses.GET,
            OSRM_URL,
            body=requests.ConnectionError(
                "HTTPConnectionPool(host='osrm.internal.test', port=80): Max retries exceeded "
                "with url: /route/v1/driving/-90.0,40.0;-80.0,40.0?overview=full"
            ),
        )
        resp = APIClient().post(
            API, {"start": {"lat": 40, "lng": -90}, "finish": FINISH}, format="json"
        )
    assert resp.status_code == 502
    error = resp.json()["error"]
    assert error == "Routing provider error: OSRM request failed (ConnectionError)"
    assert "osrm.internal.test" not in error and "HTTPConnectionPool" not in error


def test_unparseable_body_error_carries_no_python_internals():
    with responses.RequestsMock() as rsps, override_settings(**OSRM_SETTINGS):
        rsps.add(
            responses.GET,
            OSRM_URL,
            json={"code": "Ok", "routes": [{"distance": 1000, "duration": 10}]},  # no geometry
            status=200,
        )
        resp = APIClient().post(
            API, {"start": {"lat": 40, "lng": -90}, "finish": FINISH}, format="json"
        )
    assert resp.status_code == 502
    assert resp.json()["error"] == "Routing provider error: OSRM returned an unparseable response"
