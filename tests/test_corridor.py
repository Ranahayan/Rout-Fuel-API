"""Tests for corridor candidate selection and mile-marker projection."""

import pytest

from routing.client import RouteResult
from stations.models import FuelStation
from trips.corridor import find_candidates
from trips.geo import cumulative_miles


def _make_station(opis_id, name, lat, lng, price):
    return FuelStation.objects.create(
        opis_id=opis_id,
        name=name,
        address="",
        city="X",
        state="IL",
        retail_price=price,
        latitude=lat,
        longitude=lng,
        is_geocoded=True,
    )


def _straight_route():
    # West-to-east line along latitude 40.0.
    points = [(40.0, -90.0), (40.0, -80.0)]
    cum = cumulative_miles(points)
    return RouteResult(
        geometry={"type": "LineString", "coordinates": [[lng, lat] for lat, lng in points]},
        points=points,
        total_distance_miles=cum[-1],  # scale factor 1.0
        duration_minutes=600.0,
        provider="test",
        start=points[0],
        finish=points[-1],
    )


@pytest.mark.django_db
def test_corridor_selects_on_route_excludes_far():
    on_start = _make_station(1, "ON_START", 40.0, -89.0, 3.0)  # ~on the line, near start
    far = _make_station(2, "FAR", 42.0, -85.0, 2.0)  # ~138 mi off the line
    on_end = _make_station(3, "ON_END", 40.02, -81.0, 2.5)  # ~1.4 mi off, near end

    route = _straight_route()
    candidates, total = find_candidates(route, buffer_miles=7.0)

    ids = [c.station.opis_id for c in candidates]
    assert on_start.opis_id in ids
    assert on_end.opis_id in ids
    assert far.opis_id not in ids  # detour far exceeds the buffer


@pytest.mark.django_db
def test_mile_markers_are_ordered_and_reasonable():
    _make_station(1, "NEAR_START", 40.0, -89.0, 3.0)
    _make_station(2, "MIDDLE", 40.0, -85.0, 3.0)
    _make_station(3, "NEAR_END", 40.0, -81.0, 3.0)

    route = _straight_route()
    candidates, total = find_candidates(route, buffer_miles=7.0)
    markers = [c.mile_marker for c in candidates]

    assert markers == sorted(markers)  # ordered start -> finish
    assert 0 < markers[0] < markers[-1] < total  # within the route
    assert all(c.detour_miles < 1.0 for c in candidates)  # all sit on the line
