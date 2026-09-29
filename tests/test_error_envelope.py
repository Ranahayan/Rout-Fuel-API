"""Every failure, from any layer, comes back in one envelope:
{"error": <message>, "code": <machine-readable>, ...extras} -- and the map page
reports the same message and status for the same failure."""

import html
import json
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client
from rest_framework.test import APIClient
from rest_framework.throttling import AnonRateThrottle

from routing.client import RouteResult
from routing.exceptions import (
    EndpointResolutionError,
    OutsideUSAError,
    RouteProviderError,
    SameEndpointError,
    UnroutableError,
)
from stations.models import FuelStation
from trips import spatial_index
from trips.api_errors import DOMAIN_ERRORS, error_response
from trips.geo import cumulative_miles
from trips.optimizer import InfeasibleRouteError
from trips.services import NoStationsLoadedError

API = "/api/v1/route-fuel-plan/"
MAP = "/api/v1/route-fuel-plan/map/?start=40,-90&finish=40,-80"
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


def _envelope(resp):
    body = resp.json()
    assert isinstance(body["error"], str) and body["error"]
    assert isinstance(body["code"], str) and body["code"]
    return body


# --- The mapping table itself ------------------------------------------------------------


def test_every_domain_exception_is_mapped_once():
    assert set(DOMAIN_ERRORS) == {
        EndpointResolutionError,
        OutsideUSAError,
        SameEndpointError,
        UnroutableError,
        InfeasibleRouteError,
        NoStationsLoadedError,
        RouteProviderError,
    }
    assert error_response(RuntimeError("not ours")) is None


def test_infeasible_payload_carries_segment_and_conditional_hint():
    payload, code = error_response(InfeasibleRouteError("gap", 10.0, 600.0, buffer_miles=7.0))
    assert code == 422
    assert payload["infeasible_segment"] == {"from_mile": 10.0, "to_mile": 600.0}
    assert "hint" in payload
    payload, _ = error_response(InfeasibleRouteError("gap", 10.0, 600.0, buffer_miles=50.0))
    assert "hint" not in payload  # buffer already at its maximum


# --- Domain errors through the API ---------------------------------------------------------


@pytest.mark.parametrize(
    "exc,status_code,code",
    [
        (EndpointResolutionError("Could not resolve 'X'."), 400, "unresolvable_endpoint"),
        (OutsideUSAError("The start location is outside the USA."), 400, "outside_usa"),
        (SameEndpointError("Start and finish must be different locations."), 400, "same_endpoint"),
        (UnroutableError("No drivable route."), 422, "unroutable"),
        (RouteProviderError("OSRM HTTP 503"), 502, "provider_error"),
    ],
    ids=["unresolvable", "outside_usa", "same_endpoint", "unroutable", "provider_error"],
)
def test_domain_errors_share_the_envelope(db, exc, status_code, code):
    with patch("trips.services.get_route", side_effect=exc):
        resp = APIClient().post(API, COORDS, format="json")
    assert resp.status_code == status_code
    body = _envelope(resp)
    assert body["code"] == code
    assert str(exc) in body["error"]


@pytest.mark.django_db
def test_infeasible_and_no_stations_envelopes():
    client = APIClient()
    with patch("trips.services.get_route", side_effect=_fake_route):
        empty = client.post(API, COORDS, format="json")
        FuelStation.objects.create(
            opis_id=1,
            name="ONLY",
            address="",
            city="X",
            state="IL",
            retail_price=3.0,
            latitude=40.0,
            longitude=-89.9,
            is_geocoded=True,
        )
        spatial_index.reset_index()  # the index re-checks the table every 5 s; don\'t wait
        infeasible = client.post(API, COORDS, format="json")
    assert empty.status_code == 503 and _envelope(empty)["code"] == "no_stations_loaded"
    body = _envelope(infeasible)
    assert infeasible.status_code == 422 and body["code"] == "infeasible"
    assert {"from_mile", "to_mile"} <= set(body["infeasible_segment"]) and "hint" in body


# --- DRF's own exceptions re-shaped -----------------------------------------------------


def test_validation_error_envelope_keeps_field_details():
    resp = APIClient().post(API, {"start": "", "buffer_miles": "abc"}, format="json")
    assert resp.status_code == 400
    body = _envelope(resp)
    assert body["code"] == "invalid_input"
    assert set(body["fields"]) == {"start", "finish", "buffer_miles"}
    assert "finish: This field is required." in body["error"]


def test_method_media_type_parse_and_throttle_envelopes(db):
    client = APIClient()
    put = client.put(API, COORDS, format="json")
    assert put.status_code == 405 and _envelope(put)["code"] == "method_not_allowed"

    form = client.post(API, {"start": "Chicago, IL", "finish": "Houston, TX"})  # multipart
    assert form.status_code == 415 and _envelope(form)["code"] == "unsupported_media_type"

    bad_json = Client().post(API, data="{not json", content_type="application/json")
    assert bad_json.status_code == 400 and _envelope(bad_json)["code"] == "parse_error"

    cache.clear()  # fresh throttle history: one allowed request, then a 429 with a wait

    with patch.object(AnonRateThrottle, "THROTTLE_RATES", {"anon": "1/min"}):
        client.get(f"{API}?start=40,-90")
        throttled = client.get(f"{API}?start=40,-90")
    assert throttled.status_code == 429
    assert _envelope(throttled)["code"] == "throttled"
    assert "Retry-After" in throttled


# --- The map page agrees ----------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "exc,status_code",
    [
        (EndpointResolutionError("Could not resolve 'X'."), 400),
        (UnroutableError("No drivable route."), 422),
        (RouteProviderError("OSRM HTTP 503"), 502),
    ],
    ids=["400", "422", "502"],
)
def test_map_page_reports_the_same_status_and_message(exc, status_code):
    with patch("trips.services.get_route", side_effect=exc):
        api = APIClient().post(API, COORDS, format="json")
        page = APIClient().get(MAP)
    assert api.status_code == page.status_code == status_code
    page_text = html.unescape(page.content.decode())
    assert api.json()["error"] in page_text
    assert "Could not render map" in page_text


def test_unexpected_exceptions_still_become_500s(db):
    """Anything outside the mapping table is not swallowed into a fake 4xx."""
    with patch("trips.services.get_route", side_effect=RuntimeError("bug")):
        resp = Client(raise_request_exception=False).post(
            API, data=json.dumps(COORDS), content_type="application/json"
        )
    assert resp.status_code == 500
