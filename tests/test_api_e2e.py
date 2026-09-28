"""End-to-end API test with the routing call mocked (no network)."""

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from routing.client import RouteResult
from stations.models import FuelStation
from trips.geo import cumulative_miles

ROUTE_POINTS = [(40.0, -90.0), (40.0, -80.0)]
ROUTE_LEN = cumulative_miles(ROUTE_POINTS)[-1]


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
def stations_on_route(db):
    # Stations spread along the line (detour ~0), prices varying.
    specs = [(-89.0, 3.40), (-87.0, 3.10), (-85.0, 2.95), (-83.0, 3.30), (-81.0, 3.05)]
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


@pytest.mark.django_db
def test_post_route_fuel_plan(stations_on_route):
    client = APIClient()
    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = client.post(
            "/api/v1/route-fuel-plan/",
            {"start": {"lat": 40.0, "lng": -90.0}, "finish": {"lat": 40.0, "lng": -80.0}},
            format="json",
        )
    assert resp.status_code == 200
    data = resp.json()

    # Shape
    assert set(data) == {"route", "fuel", "fuel_stops"}
    assert data["route"]["provider"] == "mock"
    assert "route-fuel-plan/map/" in data["route"]["map_url"]
    assert data["route"]["geometry"]["type"] == "LineString"

    # Fuel stops ordered by mile marker.
    markers = [s["route_mile_marker"] for s in data["fuel_stops"]]
    assert markers == sorted(markers)
    assert [s["order"] for s in data["fuel_stops"]] == list(range(1, len(markers) + 1))

    # Totals reconcile: total gallons == distance / mpg, and the per-stop costs
    # sum to the reported total cost.
    total_gallons = data["fuel"]["total_gallons"]
    assert total_gallons == pytest.approx(ROUTE_LEN / 10.0, abs=0.05)
    stop_cost_sum = sum(float(s["cost_usd"]) for s in data["fuel_stops"])
    assert stop_cost_sum == pytest.approx(float(data["fuel"]["total_cost_usd"]), abs=0.02)


@pytest.mark.django_db
def test_get_form_and_missing_params(stations_on_route):
    client = APIClient()
    # Missing params -> 400.
    assert client.get("/api/v1/route-fuel-plan/").status_code == 400

    with patch("trips.services.get_route", side_effect=_fake_route):
        resp = client.get("/api/v1/route-fuel-plan/?start=40.0,-90.0&finish=40.0,-80.0")
    assert resp.status_code == 200
    assert resp.json()["fuel_stops"]
