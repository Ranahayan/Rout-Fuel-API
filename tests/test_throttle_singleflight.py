"""Rate limiting and single-flight routing.

- The JSON endpoint and the map page share one per-IP budget (the map page used
  to be a plain Django view outside DRF's throttling, i.e. unlimited).
- ``X-Forwarded-For`` is not trusted unless ``NUM_PROXIES`` says how many hops are.
- Concurrent identical uncached requests make ONE upstream routing call; the
  others wait for its cached outcome. Failures are remembered so retries do not
  re-hit the provider.
"""

import threading
import time
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient
from rest_framework.throttling import AnonRateThrottle

from routing import client as routing_client
from routing.client import RouteResult, get_route
from routing.exceptions import RouteProviderError, UnroutableError
from routing.providers import ProviderRoute, RouteProvider
from stations.models import FuelStation
from trips.geo import METERS_PER_MILE, cumulative_miles

API = "/api/v1/route-fuel-plan/"
MAP = "/api/v1/route-fuel-plan/map/"
MAP_URL = f"{MAP}?start=40,-90&finish=40,-80"
ROUTE_POINTS = [(40.0, -90.0), (40.0, -80.0)]
ROUTE_LEN = cumulative_miles(ROUTE_POINTS)[-1]
START, FINISH = {"lat": 40.0, "lng": -90.0}, {"lat": 40.0, "lng": -80.0}
COORDS = {"start": START, "finish": FINISH}

# DRF copies DEFAULT_THROTTLE_RATES onto the throttle class at import time, so
# override_settings(REST_FRAMEWORK=...) does not reach it; patching the class does.
TWO_PER_MINUTE = {"anon": "2/min", "user": "120/min"}


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


def _rest_framework(**overrides):
    return {**settings.REST_FRAMEWORK, **overrides}


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


# --- Throttling ------------------------------------------------------------------------


@pytest.mark.django_db
def test_api_returns_429_after_the_anon_limit(stations_on_route):
    client = APIClient()
    with (
        patch.object(AnonRateThrottle, "THROTTLE_RATES", TWO_PER_MINUTE),
        patch("trips.services.get_route", side_effect=_fake_route),
    ):
        responses = [client.post(API, COORDS, format="json") for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert "Retry-After" in responses[-1]


@pytest.mark.django_db
def test_map_page_is_throttled_and_stops_calling_upstream(stations_on_route):
    """Five map requests for five distinct trips: only the first two may reach the
    routing layer; the rest are 429 before any work happens."""
    client = APIClient()
    upstream = []

    def counting(start, finish, *args, **kwargs):
        upstream.append((start, finish))
        return _fake_route()

    with (
        patch.object(AnonRateThrottle, "THROTTLE_RATES", TWO_PER_MINUTE),
        patch("trips.services.get_route", side_effect=counting),
    ):
        codes = [
            client.get(f"{MAP}?start=40,-90&finish=40,-80.{i}").status_code for i in range(1, 6)
        ]
    assert codes == [200, 200, 429, 429, 429]
    assert len(upstream) == 2


@pytest.mark.django_db
def test_map_page_and_api_share_one_budget(stations_on_route):
    client = APIClient()
    with (
        patch.object(AnonRateThrottle, "THROTTLE_RATES", TWO_PER_MINUTE),
        patch("trips.services.get_route", side_effect=_fake_route),
    ):
        assert client.post(API, COORDS, format="json").status_code == 200
        assert client.get(MAP_URL).status_code == 200
        assert client.get(MAP_URL).status_code == 429
        assert client.post(API, COORDS, format="json").status_code == 429


def test_throttled_map_response_is_html_with_retry_after(db):
    client = APIClient()
    with patch.object(AnonRateThrottle, "THROTTLE_RATES", {"anon": "1/min", "user": "120/min"}):
        assert client.get(MAP).status_code == 400  # missing params -- still counts
        resp = client.get(MAP)
    assert resp.status_code == 429
    assert resp["Content-Type"].startswith("text/html")
    assert "Retry-After" in resp
    assert "429" in resp.content.decode()


@pytest.mark.django_db
def test_forged_x_forwarded_for_cannot_mint_a_fresh_quota(stations_on_route):
    """Default NUM_PROXIES=0: the throttle keys on the socket address only, so a
    client rotating X-Forwarded-For values is still one client."""
    client = APIClient()
    with (
        override_settings(REST_FRAMEWORK=_rest_framework(NUM_PROXIES=0)),
        patch.object(AnonRateThrottle, "THROTTLE_RATES", TWO_PER_MINUTE),
        patch("trips.services.get_route", side_effect=_fake_route),
    ):
        codes = [
            client.post(
                API, COORDS, format="json", HTTP_X_FORWARDED_FOR=f"203.0.113.{i}"
            ).status_code
            for i in range(5)
        ]
    assert codes == [200, 200, 429, 429, 429]


@pytest.mark.django_db
def test_num_proxies_trusts_only_the_last_forwarded_hop(stations_on_route):
    """Behind one trusted proxy, the client is the LAST X-Forwarded-For entry (the
    one the proxy appended); anything a client prepends is ignored."""
    client = APIClient()
    with (
        override_settings(REST_FRAMEWORK=_rest_framework(NUM_PROXIES=1)),
        patch.object(AnonRateThrottle, "THROTTLE_RATES", TWO_PER_MINUTE),
        patch("trips.services.get_route", side_effect=_fake_route),
    ):
        same_client = [
            client.post(
                API, COORDS, format="json", HTTP_X_FORWARDED_FOR=f"198.51.100.{i}, 203.0.113.7"
            ).status_code
            for i in range(3)
        ]
        other_client = client.post(
            API, COORDS, format="json", HTTP_X_FORWARDED_FOR="198.51.100.9, 203.0.113.8"
        ).status_code
    assert same_client == [200, 200, 429]  # forged first hop changes nothing
    assert other_client == 200  # a genuinely different client has its own budget


# --- Single-flight + failure caching -------------------------------------------------------


class _CountingProvider(RouteProvider):
    """Fake provider: counts calls, optionally sleeps, optionally fails."""

    name = "fake"

    def __init__(self, delay: float = 0.0, fail_with: Exception | None = None):
        self.delay = delay
        self.fail_with = fail_with
        self.calls = 0
        self._lock = threading.Lock()

    def route(self, start, end, deadline=None):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        return ProviderRoute(
            coordinates=[[-90.0, 40.0], [-80.0, 40.0]],
            distance_meters=ROUTE_LEN * METERS_PER_MILE,
            duration_seconds=36000,
        )


def _hammer(n, fn):
    """Run ``fn`` in ``n`` threads released simultaneously; collect outcomes."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - we want to see every outcome
            results[i] = ("err", exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_identical_requests_share_one_upstream_call():
    provider = _CountingProvider(delay=0.3)
    with patch("routing.client.get_provider", return_value=provider):
        results = _hammer(8, lambda: get_route(START, FINISH))
    assert provider.calls == 1
    assert [kind for kind, _ in results] == ["ok"] * 8
    assert {round(r.total_distance_miles, 6) for _, r in results} == {round(ROUTE_LEN, 6)}


def test_leader_failure_is_shared_with_followers_and_remembered():
    provider = _CountingProvider(delay=0.3, fail_with=RouteProviderError("OSRM HTTP 503"))
    with patch("routing.client.get_provider", return_value=provider):
        results = _hammer(6, lambda: get_route(START, FINISH))
        assert provider.calls == 1  # followers did not go upstream after the leader failed
        assert all(kind == "err" and isinstance(exc, RouteProviderError) for kind, exc in results)
        with pytest.raises(RouteProviderError, match="OSRM HTTP 503"):
            get_route(START, FINISH)  # remembered for ROUTING_FAILURE_CACHE_SECONDS
        assert provider.calls == 1


def test_failure_caching_can_be_disabled():
    provider = _CountingProvider(fail_with=RouteProviderError("boom"))
    with (
        override_settings(ROUTING_FAILURE_CACHE_SECONDS=0),
        patch("routing.client.get_provider", return_value=provider),
    ):
        for _ in range(3):
            with pytest.raises(RouteProviderError):
                get_route(START, FINISH)
    assert provider.calls == 3


def test_unroutable_trip_is_cached_like_a_result():
    provider = _CountingProvider(fail_with=UnroutableError("No drivable route"))
    with patch("routing.client.get_provider", return_value=provider):
        for _ in range(3):
            with pytest.raises(UnroutableError):
                get_route(START, FINISH)
    assert provider.calls == 1


@pytest.mark.django_db
def test_unroutable_is_asked_upstream_once_across_api_and_map(stations_on_route):
    """Before: the map page made its own provider call for the same impossible trip."""
    provider = _CountingProvider(fail_with=UnroutableError("No drivable route"))
    client = APIClient()
    with patch("routing.client.get_provider", return_value=provider):
        api = client.post(API, COORDS, format="json")
        page = client.get(MAP_URL)
    assert (api.status_code, page.status_code) == (422, 422)
    assert provider.calls == 1


def test_stale_lock_from_a_dead_leader_does_not_block_forever():
    """If a leader dies without caching or releasing, followers wait at most the
    request's time budget and then fetch themselves."""
    provider = _CountingProvider()
    key = routing_client._cache_key((40.0, -90.0), (40.0, -80.0), provider.name)
    with (
        override_settings(ROUTING_TOTAL_DEADLINE_SECONDS=0.3),
        patch("routing.client.get_provider", return_value=provider),
    ):
        cache.add(key + ":lock", 1, timeout=0.3)  # a dead leader's lock
        t0 = time.perf_counter()
        result = get_route(START, FINISH)
        waited = time.perf_counter() - t0
    assert provider.calls == 1
    assert result.total_distance_miles == pytest.approx(ROUTE_LEN)
    assert 0.25 < waited < 2.0
