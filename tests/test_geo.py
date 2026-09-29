"""Unit tests for the vectorised geo math (``trips/geo.py``) and corridor densification."""

import math
import random
from itertools import pairwise

import numpy as np
import pytest

from trips.corridor import _densify
from trips.geo import (
    EARTH_RADIUS_MILES,
    cumulative_miles,
    haversine_miles,
    project_points_to_route,
    project_to_route,
    segment_miles,
)


def test_haversine_one_degree_of_latitude():
    assert haversine_miles(40.0, -90.0, 41.0, -90.0) == pytest.approx(
        math.pi * EARTH_RADIUS_MILES / 180, rel=1e-9
    )  # ~69.09 mi


def test_segment_and_cumulative_miles_match_scalar_haversine():
    # Includes a zero-length segment (duplicate vertex), as real geometry sometimes does.
    pts = [(40.0, -90.0), (40.5, -89.5), (41.0, -89.9), (41.0, -89.9), (40.2, -88.0)]
    expected = [haversine_miles(*a, *b) for a, b in pairwise(pts)]

    seg = segment_miles(pts)
    assert seg.tolist() == pytest.approx(expected, abs=1e-12)
    assert seg[2] == 0.0

    cum = cumulative_miles(pts)
    assert len(cum) == len(pts) and cum[0] == 0.0
    assert cum == pytest.approx(np.concatenate(([0.0], np.cumsum(expected))).tolist(), abs=1e-12)


def test_cumulative_miles_degenerate_paths():
    assert cumulative_miles([]) == [0.0]
    assert cumulative_miles([(40.0, -90.0)]) == [0.0]
    assert segment_miles([]).shape == (0,)


def test_project_to_route_known_geometry():
    route = [(40.0, -90.0), (41.0, -90.0)]  # due north, ~69 mi
    cum = cumulative_miles(route)

    marker, perp = project_to_route(40.5, -90.1, route, cum)
    assert perp == pytest.approx(haversine_miles(40.5, -90.1, 40.5, -90.0), rel=0.01)
    assert marker == pytest.approx(cum[-1] / 2, rel=0.01)

    # Before the start clamps to the first vertex (marker 0); past the end to the last.
    marker, perp = project_to_route(39.5, -90.0, route, cum)
    assert marker == 0.0
    assert perp == pytest.approx(haversine_miles(39.5, -90.0, 40.0, -90.0), rel=0.01)
    marker, perp = project_to_route(41.5, -90.0, route, cum)
    assert marker == pytest.approx(cum[-1], rel=0.005)
    assert perp == pytest.approx(haversine_miles(41.5, -90.0, 41.0, -90.0), rel=0.01)

    # Exactly on a vertex.
    assert project_to_route(41.0, -90.0, route, cum)[1] == pytest.approx(0.0, abs=1e-9)


def test_project_handles_degenerate_routes_and_zero_length_segments():
    assert project_to_route(40.0, -90.0, [], [0.0]) == (0.0, math.inf)
    assert project_to_route(40.0, -90.0, [(40.0, -90.0)], [0.0]) == (0.0, math.inf)
    # A zero-length segment projects onto its single point.
    dup = [(40.0, -90.0), (40.0, -90.0), (40.0, -89.0)]
    assert project_to_route(40.0, -90.0, dup, cumulative_miles(dup)) == (0.0, 0.0)


def test_project_points_matches_scalar_and_pruning_is_exact():
    rng = random.Random(11)
    route = [(35.0, -100.0)]
    for _ in range(80):
        route.append((route[-1][0] + rng.uniform(-0.2, 0.2), route[-1][1] + rng.uniform(-0.2, 0.2)))
    route += route[::-1][1:]  # out-and-back: points sit near two passes of the route
    cum = cumulative_miles(route)
    lats = [rng.choice(route)[0] + rng.uniform(-0.4, 0.4) for _ in range(400)]
    lngs = [rng.choice(route)[1] + rng.uniform(-0.4, 0.4) for _ in range(400)]

    markers, perps = project_points_to_route(lats, lngs, route, cum)
    for i in range(0, 400, 7):  # the batch version is the scalar version, element-wise
        assert (markers[i], perps[i]) == project_to_route(lats[i], lngs[i], route, cum)

    buffer = 7.0
    pruned_markers, pruned_perps = project_points_to_route(
        lats, lngs, route, cum, max_perp_miles=buffer
    )
    inside = perps <= buffer
    assert inside.any() and (~inside).any()
    # Pruning never changes which points are in the corridor, nor their values...
    assert np.array_equal(pruned_perps <= buffer, inside)
    assert np.array_equal(pruned_markers[inside], markers[inside])
    assert np.array_equal(pruned_perps[inside], perps[inside])
    # ...and points outside are skipped (inf) or truthfully far.
    assert np.all(pruned_perps[~inside] > buffer)


def test_project_points_empty_inputs():
    markers, perps = project_points_to_route([], [], [(40.0, -90.0), (41.0, -90.0)], [0.0, 69.0])
    assert markers.shape == (0,) and perps.shape == (0,)


# --- _densify ---------------------------------------------------------------------


def test_densify_inserts_only_on_long_segments_and_keeps_order():
    pts = [(40.0, -90.0), (40.0, -89.95), (40.0, -89.0), (40.0, -88.99)]  # ~2.6, ~50, ~0.5 mi
    out = _densify(pts, spacing=7.0)

    assert [p for p in out if p in pts] == pts  # originals kept, in order
    assert out[0] == pts[0] and out[-1] == pts[-1]
    gaps = [haversine_miles(*a, *b) for a, b in pairwise(out)]
    assert max(gaps) <= 7.0 * 1.001
    long_seg = haversine_miles(*pts[1], *pts[2])
    assert len(out) == len(pts) + math.floor(long_seg / 7.0)


def test_densify_short_paths_unchanged():
    assert _densify([], 5.0) == []
    assert _densify([(1.0, 2.0)], 5.0) == [(1.0, 2.0)]
    pts = [(40.0, -90.0), (40.0, -89.9), (40.0, -89.8)]  # ~5 mi segments
    assert _densify(pts, spacing=7.0) == pts


def test_densify_prefilter_is_superset_of_exact_corridor():
    """The corridor relies on: perp(station) <= buffer  =>  some densified query
    point is within 2 * buffer (the KD-tree radius). Randomised check."""
    rng = random.Random(7)
    checked = 0
    for _ in range(60):
        pts = [(rng.uniform(30, 48), rng.uniform(-120, -75))]
        for _ in range(rng.randint(4, 14)):
            pts.append((pts[-1][0] + rng.uniform(-0.5, 0.5), pts[-1][1] + rng.uniform(-0.5, 0.5)))
        cum = cumulative_miles(pts)
        buffer = rng.choice([0.5, 2.0, 7.0, 25.0])
        dense = _densify(pts, spacing=buffer)
        for _ in range(30):
            base = rng.choice(pts)
            plat, plng = base[0] + rng.uniform(-0.6, 0.6), base[1] + rng.uniform(-0.6, 0.6)
            _, perp = project_to_route(plat, plng, pts, cum)
            if perp <= buffer:
                checked += 1
                nearest_query = min(haversine_miles(plat, plng, a, b) for a, b in dense)
                assert nearest_query <= 2.0 * buffer, (perp, nearest_query, buffer)
    assert checked > 100
