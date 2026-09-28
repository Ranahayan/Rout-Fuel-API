"""API-level tests for the documented error contract (400 / 422 / 502) and the
shared GET/POST validation path, with the routing call mocked (no network)."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from routing.client import RouteResult
from routing.exceptions import OutsideUSAError, RouteProviderError
from stations.models import FuelStation
from trips.geo import cumulative_miles

ROUTE_POINTS = [(40.0, -90.0), (40.0, -80.0)]
ROUTE_LEN = cumulative_miles(ROUTE_POINTS)[-1]
COORDS = {"start": {"lat": 40.0, "lng": -90.0}, "finish": {"lat": 40.0, "lng": -80.0}}


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


@pytest.fixture
def client():
    return APIClient()


# --- Error contract -------------------------------------------------------


def test_outside_usa_returns_400(client):
    with patch("trips.services.get_route", side_effect=OutsideUSAError("outside the USA")):
        resp = client.post("/api/v1/route-fuel-plan/", COORDS, format="json")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_provider_failure_returns_502(client):
    with patch("trips.services.get_route", side_effect=RouteProviderError("upstream down")):
        resp = client.post("/api/v1/route-fuel-plan/", COORDS, format="json")
    assert resp.status_code == 502
    assert "Routing provider error" in resp.json()["error"]


@pytest.mark.django_db
def test_infeasible_returns_422_with_segment(client):
    # No stations loaded -> no fueling plan within range -> 422 with the segment.
    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = client.post("/api/v1/route-fuel-plan/", COORDS, format="json")
    assert resp.status_code == 422
    body = resp.json()
    assert "infeasible_segment" in body
    assert {"from_mile", "to_mile"} <= set(body["infeasible_segment"])


# --- Shared GET/POST validation (no longer 500s on bad input) -------------


@pytest.mark.parametrize("bad", ["abc", "999", "0"])
def test_bad_buffer_is_400_not_500_post(client, bad):
    resp = client.post("/api/v1/route-fuel-plan/", {**COORDS, "buffer_miles": bad}, format="json")
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", ["abc", "999"])
def test_bad_buffer_is_400_not_500_get(client, bad):
    resp = client.get(f"/api/v1/route-fuel-plan/?start=40,-90&finish=40,-80&buffer_miles={bad}")
    assert resp.status_code == 400


def test_missing_endpoints_get_is_400(client):
    assert client.get("/api/v1/route-fuel-plan/?start=40,-90").status_code == 400


# --- Money reconciles exactly to the penny --------------------------------


@pytest.mark.django_db
def test_stop_costs_sum_exactly_to_total(client):
    specs = [(-89.0, 3.401), (-87.0, 3.117), (-85.0, 2.953), (-81.0, 3.059)]
    for i, (lng, price) in enumerate(specs, start=1):
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
    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = client.post("/api/v1/route-fuel-plan/", COORDS, format="json")
    assert resp.status_code == 200
    data = resp.json()
    stop_sum = sum(Decimal(s["cost_usd"]) for s in data["fuel_stops"])
    assert stop_sum == Decimal(data["fuel"]["total_cost_usd"])  # exact, no drift
