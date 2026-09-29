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


def _curved_route(n_points: int) -> RouteResult:
    """A gently curving Chicago -> Houston path sampled at OSRM ``overview=full``
    density (sub-mile segments), with a realistic road-distance factor."""
    import math

    a, b = (41.88, -87.63), (29.76, -95.37)
    points = []
    for i in range(n_points):
        f = i / (n_points - 1)
        wobble = 0.15 * math.sin(f * 2 * math.pi * 5)
        points.append((a[0] + (b[0] - a[0]) * f + wobble, a[1] + (b[1] - a[1]) * f))
    length = cumulative_miles(points)[-1]
    return RouteResult(
        geometry={"type": "LineString", "coordinates": [[lng, lat] for lat, lng in points]},
        points=points,
        total_distance_miles=length * 1.1,
        duration_minutes=1000.0,
        provider="test",
        start=points[0],
        finish=points[-1],
    )


@pytest.mark.django_db
def test_dense_route_picks_exactly_the_on_route_stations_quickly():
    """40k-point route, 100 stations just off the road plus 20 far away: the
    corridor must admit exactly the 100, in route order, with the right detours,
    and do it fast. The pure-Python projection this replaced took ~3 s on this
    input; the numpy version takes ~50 ms, so the 1 s bound catches a regression
    to an O(stations x segments) loop without being flaky on slow machines."""
    import time

    route = _curved_route(40_000)
    on_route_ids = []
    for k in range(100):
        lat, lng = route.points[(k + 1) * 390]
        _make_station(1000 + k, f"ON_{k}", lat + 0.004, lng, 3.0)  # ~0.3 mi north of the road
        on_route_ids.append(1000 + k)
    for k in range(20):
        lat, lng = route.points[(k + 1) * 1900]
        _make_station(
            2000 + k, f"FAR_{k}", lat + 0.4, lng, 2.0
        )  # ~28 mi off, outside a 7 mi buffer

    t0 = time.perf_counter()
    candidates, total = find_candidates(route, buffer_miles=7.0)
    elapsed = time.perf_counter() - t0

    assert [c.station.opis_id for c in candidates] == on_route_ids  # exactly the 100, in order
    markers = [c.mile_marker for c in candidates]
    assert markers == sorted(markers) and 0 < markers[0] and markers[-1] < total
    assert all(0.05 < c.detour_miles < 0.4 for c in candidates)  # ~0.28 mi north of a slanted road
    assert total == pytest.approx(route.total_distance_miles)
    assert elapsed < 1.0, f"find_candidates took {elapsed:.2f}s on a 40k-point route"
