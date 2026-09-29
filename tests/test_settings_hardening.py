"""Hardened defaults: DEBUG off, JSON-only outside debug, no API authentication,
password validators on, HSTS preload opt-in, proxy header behind a flag, a CSP
and Subresource Integrity on the map page, and safe test settings."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from django.conf import settings
from rest_framework.test import APIClient
from rest_framework.throttling import AnonRateThrottle

from routing.client import RouteResult
from stations.models import FuelStation
from trips.geo import cumulative_miles

REPO = Path(__file__).resolve().parents[1]
API = "/api/v1/route-fuel-plan/"
MAP_URL = "/api/v1/route-fuel-plan/map/?start=40,-90&finish=40,-80"
ROUTE_POINTS = [(40.0, -90.0), (40.0, -80.0)]
COORDS = {"start": {"lat": 40.0, "lng": -90.0}, "finish": {"lat": 40.0, "lng": -80.0}}

PROBE = (
    "import django, json; django.setup(); from django.conf import settings as s; "
    "print(json.dumps({'debug': s.DEBUG, 'renderers': s.REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'], "
    "'auth': s.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES'], "
    "'validators': len(s.AUTH_PASSWORD_VALIDATORS), 'preload': getattr(s, 'SECURE_HSTS_PRELOAD', None), "
    "'proxy_header': getattr(s, 'SECURE_PROXY_SSL_HEADER', None), "
    "'redirect': getattr(s, 'SECURE_SSL_REDIRECT', None)}))"
)


def _boot(**env):
    full = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", **env}
    proc = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=REPO, env=full, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


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


# --- Defaults ------------------------------------------------------------------------------


def test_debug_defaults_to_false_in_the_settings_schema():
    from config.settings import env

    assert env.scheme["DEBUG"] == (bool, False)


def test_production_boot_is_json_only_with_validators_and_optional_preload():
    prod = {"DEBUG": "False", "SECRET_KEY": "x" * 50, "ALLOWED_HOSTS": "example.com"}
    out = _boot(**prod)
    assert out["debug"] is False
    assert out["renderers"] == ["rest_framework.renderers.JSONRenderer"]
    assert out["auth"] == []
    assert out["validators"] == 4
    assert out["preload"] is False
    assert out["proxy_header"] == ["HTTP_X_FORWARDED_PROTO", "https"]
    assert out["redirect"] is True

    out = _boot(**prod, TRUST_X_FORWARDED_PROTO="False", SECURE_HSTS_PRELOAD="True")
    assert out["proxy_header"] is None
    assert out["preload"] is True


def test_debug_boot_keeps_the_browsable_api():
    out = _boot(DEBUG="True")
    assert out["debug"] is True
    assert "rest_framework.renderers.BrowsableAPIRenderer" in out["renderers"]


def test_test_settings_force_the_local_memory_cache_and_debug_mode():
    assert settings.CACHES["default"]["BACKEND"] == "django.core.cache.backends.locmem.LocMemCache"
    # pytest-django flips settings.DEBUG to False while tests run, so check an
    # artefact of the settings having been *built* in debug mode instead.
    assert (
        "rest_framework.renderers.BrowsableAPIRenderer"
        in settings.REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]
    )


# --- No API authentication -----------------------------------------------------------------


@pytest.mark.django_db
def test_forged_basic_auth_header_is_ignored_and_throttled_like_anyone(stations_on_route):
    client = APIClient()
    with (
        patch.object(AnonRateThrottle, "THROTTLE_RATES", {"anon": "2/min"}),
        patch("trips.services.get_route", side_effect=_fake_route),
    ):
        codes = [
            client.post(
                API, COORDS, format="json", HTTP_AUTHORIZATION="Basic YWRtaW46Z3Vlc3M="
            ).status_code
            for _ in range(3)
        ]
    assert codes == [200, 200, 429]  # no 401/403 detour, and the throttle still applies


# --- Map page: CSP + SRI ------------------------------------------------------------------


@pytest.mark.django_db
def test_map_page_sets_a_nonce_based_csp_and_sri_hashes(stations_on_route):
    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = APIClient().get(MAP_URL)
    assert resp.status_code == 200
    csp = resp["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([A-Za-z0-9_-]{16,})'", csp).group(1)
    html = resp.content.decode()
    assert f'<script nonce="{nonce}">' in html and f'<style nonce="{nonce}">' in html
    assert "'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp
    assert "https://*.tile.openstreetmap.org" in csp and "https://unpkg.com" in csp
    assert 'integrity="sha384-' in html and 'crossorigin="anonymous"' in html
    assert html.count("integrity=") == 2  # both Leaflet assets are pinned


@pytest.mark.django_db
def test_map_csp_nonce_is_fresh_per_request(stations_on_route):
    with patch("trips.services.get_route", side_effect=_fake_route):
        a = APIClient().get(MAP_URL)["Content-Security-Policy"]
        b = APIClient().get(MAP_URL)["Content-Security-Policy"]
    assert a != b


def test_map_error_page_also_carries_the_csp(db):
    resp = APIClient().get("/api/v1/route-fuel-plan/map/?start=40,-90")  # missing finish -> 400
    assert resp.status_code == 400
    assert "Content-Security-Policy" in resp
