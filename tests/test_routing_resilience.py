"""Routing resilience: one time budget for all upstream work, retry rules that
match each call's semantics, and a fallback provider after a provider failure."""

import time

import pytest
import requests
import responses
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from routing.client import get_provider_chain, get_route
from routing.exceptions import RouteProviderError
from routing.providers import ORSProvider, _request
from trips.geo import METERS_PER_MILE

API = "/api/v1/route-fuel-plan/"
START, FINISH = {"lat": 40.0, "lng": -90.0}, {"lat": 40.0, "lng": -80.0}
COORDS = {"start": START, "finish": FINISH}
OSRM_BASE, ORS_BASE = "http://osrm.test", "http://ors.test"
OSRM_URL = f"{OSRM_BASE}/route/v1/driving/-90.0,40.0;-80.0,40.0"
ORS_URL = f"{ORS_BASE}/v2/directions/driving-car/geojson"
GEOCODE_URL = f"{ORS_BASE}/geocode/search"
COMMON = {"OSRM_BASE_URL": OSRM_BASE, "ORS_BASE_URL": ORS_BASE}
OSRM_PRIMARY = {**COMMON, "ROUTING_PROVIDER": "osrm", "ORS_API_KEY": "testkey"}
ORS_PRIMARY = {**COMMON, "ROUTING_PROVIDER": "ors", "ORS_API_KEY": "testkey"}
COORDS_2D = [[-90.0, 40.0], [-80.0, 40.0]]
DIST = 530.0 * METERS_PER_MILE
OSRM_OK = {
    "code": "Ok",
    "routes": [{"distance": DIST, "duration": 36000, "geometry": {"coordinates": COORDS_2D}}],
}
ORS_OK = {
    "features": [
        {
            "geometry": {"coordinates": COORDS_2D},
            "properties": {"summary": {"distance": DIST, "duration": 36000}},
        }
    ]
}


# --- Fallback ------------------------------------------------------------------------------


def test_chain_is_primary_then_the_other_usable_provider():
    with override_settings(**ORS_PRIMARY):
        assert [p.name for p in get_provider_chain()] == ["ors", "osrm"]
    with override_settings(**OSRM_PRIMARY):
        assert [p.name for p in get_provider_chain()] == ["osrm", "ors"]
    with override_settings(**{**OSRM_PRIMARY, "ORS_API_KEY": ""}):
        assert [p.name for p in get_provider_chain()] == ["osrm"]  # no key: nothing to fall back to
    with override_settings(**ORS_PRIMARY, ROUTING_FALLBACK=False):
        assert [p.name for p in get_provider_chain()] == ["ors"]


def test_ors_outage_falls_back_to_osrm():
    with responses.RequestsMock() as rsps, override_settings(**ORS_PRIMARY):
        rsps.add(responses.POST, ORS_URL, status=503, json={"error": "down"})
        rsps.add(responses.GET, OSRM_URL, json=OSRM_OK, status=200)
        result = get_route(START, FINISH)
        assert len(rsps.calls) == 2
    assert result.provider == "osrm"
    assert result.total_distance_miles == pytest.approx(530.0)


def test_osrm_quota_error_falls_back_to_ors_when_a_key_exists():
    with responses.RequestsMock() as rsps, override_settings(**OSRM_PRIMARY):
        rsps.add(responses.GET, OSRM_URL, status=429, json={"message": "Too Many Requests"})
        rsps.add(responses.POST, ORS_URL, json=ORS_OK, status=200)
        result = get_route(START, FINISH)
        assert len(rsps.calls) == 2
    assert result.provider == "ors"


def test_no_fallback_for_an_unroutable_trip():
    with responses.RequestsMock() as rsps, override_settings(**ORS_PRIMARY):
        rsps.add(responses.POST, ORS_URL, status=404, json={"error": {"code": 2009}})
        resp = APIClient().post(API, COORDS, format="json")
        assert len(rsps.calls) == 1  # the road network is the same everywhere
    assert resp.status_code == 422


def test_both_providers_failing_is_a_502_naming_the_last_failure():
    with responses.RequestsMock() as rsps, override_settings(**ORS_PRIMARY):
        rsps.add(responses.POST, ORS_URL, status=503, json={})
        rsps.add(responses.GET, OSRM_URL, status=500, json={})
        resp = APIClient().post(API, COORDS, format="json")
        assert len(rsps.calls) == 2
    assert resp.status_code == 502
    assert "OSRM HTTP 500" in resp.json()["error"]


def test_fallback_can_be_disabled():
    with responses.RequestsMock() as rsps, override_settings(**ORS_PRIMARY, ROUTING_FALLBACK=False):
        rsps.add(responses.POST, ORS_URL, status=503, json={})
        with pytest.raises(RouteProviderError, match="ORS HTTP 503"):
            get_route(START, FINISH)
        assert len(rsps.calls) == 1


# --- Retry rules -----------------------------------------------------------------------------


def test_osrm_get_retries_once_on_read_timeout():
    with responses.RequestsMock() as rsps, override_settings(**{**OSRM_PRIMARY, "ORS_API_KEY": ""}):
        rsps.add(responses.GET, OSRM_URL, body=requests.ReadTimeout("slow"))
        rsps.add(responses.GET, OSRM_URL, json=OSRM_OK, status=200)
        assert get_route(START, FINISH).provider == "osrm"
        assert len(rsps.calls) == 2


def test_ors_post_is_not_retried_on_read_timeout_but_is_on_connect_failure():
    with responses.RequestsMock() as rsps, override_settings(**ORS_PRIMARY, ROUTING_FALLBACK=False):
        rsps.add(responses.POST, ORS_URL, body=requests.ReadTimeout("slow"))
        with pytest.raises(RouteProviderError):
            get_route(START, FINISH)
        assert len(rsps.calls) == 1  # the server may have processed (and billed) the request
    cache.clear()  # forget the remembered failure before the second scenario
    with responses.RequestsMock() as rsps, override_settings(**ORS_PRIMARY, ROUTING_FALLBACK=False):
        rsps.add(responses.POST, ORS_URL, body=requests.ConnectionError("refused"))
        rsps.add(responses.POST, ORS_URL, json=ORS_OK, status=200)
        assert get_route(START, FINISH).provider == "ors"
        assert len(rsps.calls) == 2


def test_geocode_is_never_retried():
    provider = ORSProvider(api_key="testkey", base_url=ORS_BASE)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, GEOCODE_URL, body=requests.ConnectionError("refused"))
        assert provider.geocode("Nowhere, ZZ") is None
        assert len(rsps.calls) == 1


# --- The time budget ------------------------------------------------------------------------


def test_request_timeout_is_capped_by_the_remaining_budget(monkeypatch):
    seen = {}

    def fake_request(method, url, timeout=None, **kwargs):
        seen["timeout"] = timeout
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "request", fake_request)
    with override_settings(ROUTING_TIMEOUT_SECONDS=8.0):
        with pytest.raises(requests.ConnectionError):
            _request("GET", "http://x.test/", deadline=time.monotonic() + 2.0, retry=False)
    assert 1.5 < seen["timeout"] <= 2.0


def test_no_retry_when_the_budget_is_nearly_gone(monkeypatch):
    calls = []

    def fake_request(method, url, timeout=None, **kwargs):
        calls.append(timeout)
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "request", fake_request)
    with pytest.raises(requests.ConnectionError):
        _request("GET", "http://x.test/", deadline=time.monotonic() + 0.5)
    assert len(calls) == 1  # < 1 s left: the retry is skipped


def test_exhausted_budget_before_the_call_is_a_provider_error():
    with (
        responses.RequestsMock() as rsps,
        override_settings(
            **{**OSRM_PRIMARY, "ORS_API_KEY": ""}, ROUTING_TOTAL_DEADLINE_SECONDS=1e-9
        ),
    ):
        with pytest.raises(RouteProviderError, match="RoutingDeadlineExceeded"):
            get_route(START, FINISH)
        assert len(rsps.calls) == 0


def test_slow_primary_does_not_get_a_fallback_once_the_budget_is_spent():
    """The fallback is skipped when the primary already used up the budget."""

    def slow(request):
        time.sleep(0.4)
        return (503, {}, "{}")

    with (
        responses.RequestsMock(
            assert_all_requests_are_fired=False
        ) as rsps,  # OSRM must stay unused
        override_settings(
            **ORS_PRIMARY, ROUTING_TOTAL_DEADLINE_SECONDS=0.3, ROUTING_TIMEOUT_SECONDS=5.0
        ),
    ):
        rsps.add_callback(responses.POST, ORS_URL, callback=slow)
        rsps.add(responses.GET, OSRM_URL, json=OSRM_OK, status=200)
        t0 = time.perf_counter()
        with pytest.raises(RouteProviderError, match="ORS HTTP 503"):
            get_route(START, FINISH)
        elapsed = time.perf_counter() - t0
        assert len(rsps.calls) == 1  # no OSRM attempt
    assert elapsed < 2.0
