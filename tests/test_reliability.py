"""Reliability: boot-time configuration checks, the degrading Redis cache, the
health probe, the no-stations response, and spatial-index refresh after a reload."""

import logging
from unittest.mock import patch

import pytest
from django.core.cache.backends.redis import RedisCache
from django.utils import timezone
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.test import APIClient

from config.cache import SafeRedisCache
from routing.client import RouteResult
from stations.models import FuelStation
from trips import spatial_index
from trips.checks import check_runtime_configuration
from trips.geo import cumulative_miles

API = "/api/v1/route-fuel-plan/"
ROUTE_POINTS = [(40.0, -90.0), (40.0, -80.0)]
COORDS = {"start": {"lat": 40.0, "lng": -90.0}, "finish": {"lat": 40.0, "lng": -80.0}}


def _fake_route(*args, **kwargs):
    return RouteResult(
        geometry={"type": "LineString", "coordinates": [[lng, lat] for lat, lng in ROUTE_POINTS]},
        points=ROUTE_POINTS,
        total_distance_miles=cumulative_miles(ROUTE_POINTS)[-1],
        duration_minutes=600.0,
        provider="mock",
        start=ROUTE_POINTS[0],
        finish=ROUTE_POINTS[-1],
    )


def _station(opis_id, lng, price=3.0):
    return FuelStation.objects.create(
        opis_id=opis_id,
        name=f"S{opis_id}",
        address="",
        city="X",
        state="IL",
        retail_price=price,
        latitude=40.0,
        longitude=lng,
        is_geocoded=True,
    )


# --- System checks ---------------------------------------------------------------------


def _ids(problems):
    return sorted(p.id for p in problems)


def test_checks_pass_with_the_test_settings():
    assert check_runtime_configuration(None) == []


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"VEHICLE_MPG": 0}, "trips.E001"),
        ({"VEHICLE_MPG": -10}, "trips.E001"),
        ({"VEHICLE_RANGE_MILES": 0}, "trips.E002"),
        ({"CORRIDOR_BUFFER_MILES": 0.1}, "trips.E003"),
        ({"CORRIDOR_BUFFER_MILES": 1000}, "trips.E003"),
        ({"ROUTING_TIMEOUT_SECONDS": 0}, "trips.E004"),
        ({"ROUTING_TOTAL_DEADLINE_SECONDS": 0}, "trips.E005"),
        ({"ROUTING_FAILURE_CACHE_SECONDS": -1}, "trips.E006"),
        ({"ROUTING_PROVIDER": "bogus"}, "trips.E007"),
        ({"ROUTING_PROVIDER": "ors", "ORS_API_KEY": ""}, "trips.E008"),
        ({"OSRM_BASE_URL": "osrm.test/no-scheme"}, "trips.E009"),
        ({"US_CITIES_CSV_PATH": "/nope/cities.csv"}, "trips.E010"),
        ({"US_BOUNDARY_GEOJSON_PATH": "/nope/usa.geojson"}, "trips.E010"),
        ({"FUEL_CSV_PATH": "/nope/fuel.csv"}, "trips.W001"),
    ],
    ids=lambda v: str(v)[:40],
)
def test_checks_flag_each_misconfiguration(settings, overrides, expected):
    for key, value in overrides.items():
        setattr(settings, key, value)
    assert expected in _ids(check_runtime_configuration(None))


def test_checks_validate_throttle_rates_and_num_proxies(settings):
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {"anon": "lots", "user": "120/min"},
        "NUM_PROXIES": -1,
    }
    ids = _ids(check_runtime_configuration(None))
    assert "trips.E012" in ids and "trips.E013" in ids


def test_checks_require_the_redis_package_when_redis_is_configured(settings):
    settings.CACHES = {
        "default": {"BACKEND": "config.cache.SafeRedisCache", "LOCATION": "redis://x"}
    }
    assert "trips.E011" not in _ids(check_runtime_configuration(None))  # installed in this venv
    with patch.dict("sys.modules", {"redis": None}):
        assert "trips.E011" in _ids(check_runtime_configuration(None))


# --- SafeRedisCache ---------------------------------------------------------------------


def test_safe_redis_cache_degrades_instead_of_raising(caplog):
    cache = SafeRedisCache("redis://127.0.0.1:1/0", {})
    down = RedisConnectionError("Connection refused")
    with (
        patch.object(RedisCache, "get", side_effect=down),
        patch.object(RedisCache, "set", side_effect=down),
        patch.object(RedisCache, "add", side_effect=down),
        patch.object(RedisCache, "delete", side_effect=down),
        patch.object(RedisCache, "get_many", side_effect=down),
        patch.object(RedisCache, "has_key", side_effect=down),
        patch("config.cache._last_logged_at", 0.0),
        caplog.at_level(logging.WARNING, logger="config.cache"),
    ):
        assert cache.get("k", "fallback") == "fallback"
        assert cache.set("k", "v") is None
        assert cache.add("lock", 1) is True  # a leader is still elected locally
        assert cache.delete("k") is False
        assert cache.get_many(["a", "b"]) == {}
        assert cache.has_key("k") is False
    assert any("Redis unavailable" in r.message for r in caplog.records)
    assert sum("Redis unavailable" in r.message for r in caplog.records) == 1  # rate-limited


def test_safe_redis_cache_is_configured_when_redis_url_is_set(monkeypatch):
    import importlib

    import config.settings as settings_module

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    reloaded = importlib.reload(settings_module)
    try:
        assert reloaded.CACHES["default"]["BACKEND"] == "config.cache.SafeRedisCache"
    finally:
        monkeypatch.delenv("REDIS_URL")
        importlib.reload(settings_module)


# --- /healthz and the no-stations response -----------------------------------------------


@pytest.mark.django_db
def test_healthz_is_degraded_without_stations_and_ok_with_them():
    client = APIClient()
    resp = client.get("/healthz/")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["stations_indexed"] == 0
    assert "load_fuel_prices" in body["hint"]
    assert body["checks"] == {"database": "ok", "cache": "ok"}

    _station(1, -89.0)
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["stations_indexed"] == 1
    assert body["routing_provider"] in ("osrm", "ors")
    assert "spatial_index_built" in body


@pytest.mark.django_db
def test_healthz_is_a_plain_view_not_throttled_and_get_only():
    from rest_framework.throttling import AnonRateThrottle

    client = APIClient()
    with patch.object(AnonRateThrottle, "THROTTLE_RATES", {"anon": "1/min", "user": "1/min"}):
        codes = [client.get("/healthz/").status_code for _ in range(5)]
    assert 429 not in codes
    assert client.post("/healthz/").status_code == 405


@pytest.mark.django_db
def test_healthz_reports_a_database_failure_as_unavailable():
    with patch("trips.health.FuelStation.objects.filter", side_effect=RuntimeError("db down")):
        resp = APIClient().get("/healthz/")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"].startswith("error")


def test_healthz_is_exempt_from_the_https_redirect_in_production():
    import importlib
    import os

    import config.settings as settings_module

    env = {"DEBUG": "False", "SECRET_KEY": "x" * 50, "ALLOWED_HOSTS": "example.com"}
    with patch.dict(os.environ, env):
        reloaded = importlib.reload(settings_module)
        assert reloaded.SECURE_REDIRECT_EXEMPT == [r"^healthz/?$"]
    importlib.reload(settings_module)


@pytest.mark.django_db
def test_no_stations_loaded_is_a_503_not_a_buffer_hint():
    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = APIClient().post(API, COORDS, format="json")
    assert resp.status_code == 503
    assert "load_fuel_prices" in resp.json()["error"]
    assert "buffer_miles" not in resp.json()


@pytest.mark.django_db
def test_buffer_hint_only_when_it_can_still_help():
    _station(1, -89.9)  # ~mile 5; the rest of the 530 mi route has nothing -> infeasible
    client = APIClient()
    with patch("trips.services.get_route", side_effect=_fake_route):
        default = client.post(API, COORDS, format="json")
        maxed = client.post(API, {**COORDS, "buffer_miles": 50}, format="json")
    assert default.status_code == maxed.status_code == 422
    assert "hint" in default.json()
    assert "hint" not in maxed.json()  # buffer already at its maximum


# --- Spatial index refresh --------------------------------------------------------------


@pytest.mark.django_db
def test_spatial_index_rebuilds_when_the_table_changes():
    _station(1, -89.0)
    first = spatial_index.get_index()
    assert first.station_count == 1
    assert spatial_index.get_index() is first  # within the check interval: no query, same object

    _station(2, -85.0)
    with patch.object(spatial_index, "_checked_at", 0.0):  # pretend the interval elapsed
        rebuilt = spatial_index.get_index()
    assert rebuilt is not first and rebuilt.station_count == 2

    # A price update alone (same row count) must also be noticed: updated_at moves.
    FuelStation.objects.filter(opis_id=1).update(retail_price=2.5, updated_at=timezone.now())
    with patch.object(spatial_index, "_checked_at", 0.0):
        refreshed = spatial_index.get_index()
    assert refreshed is not rebuilt
    assert {s.price for s in refreshed.stations} == {2.5, 3.0}

    with patch.object(spatial_index, "_checked_at", 0.0):
        assert spatial_index.get_index() is refreshed  # unchanged table: same index
