"""Pure geographic math on (lat, lng) coordinates, in miles.

We deliberately use haversine arc-length rather than planar projection for
distances along the route: 1 degree of longitude varies from ~69 mi at the
equator to ~49 mi at 49 N, so projecting raw lon/lat onto a polyline with planar
math distorts distances by 20-30% on a transcontinental route -- which would
corrupt the hard 500-mile range constraint.

Projecting a point onto a single segment uses a local equirectangular plane
centred on the segment's start vertex; road polylines have sub-mile segments,
so the local planar error is negligible (measured < 0.02 mi). Everything is
vectorised with numpy: a 30k-point route with a few hundred candidate stations
projects in tens of milliseconds rather than seconds.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import ArrayLike, NDArray

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344
# Local-plane scale for segment projection: miles per degree of latitude, derived
# from the same Earth radius as the haversine so along-segment distances and the
# cumulative polyline distance agree (longitude is additionally scaled by
# cos(lat) at the segment start).
MILES_PER_DEG_LAT = math.pi * EARTH_RADIUS_MILES / 180.0  # ~69.09

FloatArray = NDArray[np.float64]


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in miles."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def _haversine_np(
    lat1: FloatArray, lng1: FloatArray, lat2: FloatArray, lng2: FloatArray
) -> FloatArray:
    """Element-wise haversine over arrays (same formula as ``haversine_miles``)."""
    rlat1, rlat2 = np.radians(lat1), np.radians(lat2)
    dlat = rlat2 - rlat1
    dlng = np.radians(lng2 - lng1)
    a = np.sin(dlat / 2) ** 2 + np.cos(rlat1) * np.cos(rlat2) * np.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


def _as_points(points: ArrayLike) -> FloatArray:
    """(N, 2) float array of [lat, lng] rows (N may be 0)."""
    return np.asarray(points, dtype=float).reshape(-1, 2)


def segment_miles(points: ArrayLike) -> FloatArray:
    """Great-circle length of each consecutive segment of a [(lat, lng), ...] path.

    Length ``len(points) - 1``; empty for fewer than two points.
    """
    pts = _as_points(points)
    if len(pts) < 2:
        return np.zeros(0)
    return _haversine_np(pts[:-1, 0], pts[:-1, 1], pts[1:, 0], pts[1:, 1])


def cumulative_miles(points: ArrayLike) -> list[float]:
    """Cumulative arc-length (miles) at each vertex of a [(lat, lng), ...] path.

    Index 0 is 0.0 and the last entry is the total polyline length. A path with
    fewer than two points yields ``[0.0]``.
    """
    return np.concatenate(([0.0], np.cumsum(segment_miles(points)))).tolist()


def project_points_to_route(
    lats: ArrayLike,
    lngs: ArrayLike,
    route: ArrayLike,
    cum: ArrayLike,
    max_perp_miles: float | None = None,
) -> tuple[FloatArray, FloatArray]:
    """Project many points onto a route polyline at once.

    ``route`` is [(lat, lng), ...]; ``cum`` is the matching ``cumulative_miles``
    sequence. Returns ``(mile_markers, perp_miles)`` aligned with the inputs: for
    each point, the distance from the route start to the nearest point on the
    route, and the perpendicular (detour) distance to it. A point is projected
    onto each segment in a local plane centred on the segment start (the
    projection is clamped to the segment); the nearest segment wins, the first
    one on ties.

    ``max_perp_miles`` is an optional pruning hint. A point is then only tested
    against segments whose start vertex lies within ``reach = max_perp_miles +
    longest segment`` of it on both axes of the local plane (segments are
    binary-searched by start latitude, then filtered by longitude). Any segment
    within ``max_perp_miles`` of the point necessarily passes that test, so the
    result is identical for every point whose true perpendicular distance is
    <= ``max_perp_miles``; points farther away than that are either skipped
    (``perp = inf``) or reported with their true ``perp > max_perp_miles`` --
    callers discard them either way. This pruning is what makes a 30k-segment
    route cheap.
    """
    lat_arr = np.asarray(lats, dtype=float).ravel()
    lng_arr = np.asarray(lngs, dtype=float).ravel()
    n = len(lat_arr)
    markers = np.zeros(n)
    perps = np.full(n, np.inf)

    pts = _as_points(route)
    if n == 0 or len(pts) < 2:  # nothing to project, or a route with no segments
        return markers, perps

    alat, alng = pts[:-1, 0], pts[:-1, 1]
    blat, blng = pts[1:, 0], pts[1:, 1]
    # Local plane per segment: A is the origin, x = east (scaled by cos lat), y = north.
    deg_lng = MILES_PER_DEG_LAT * np.cos(np.radians(alat))
    bx = (blng - alng) * deg_lng
    by = (blat - alat) * MILES_PER_DEG_LAT
    seg_sq = bx * bx + by * by
    nonzero = seg_sq > 0.0  # zero-length segments (duplicate vertices) project onto A
    cum_a = np.asarray(cum, dtype=float)[:-1]  # marker at each segment start

    if max_perp_miles is not None:
        reach = max_perp_miles + math.sqrt(float(seg_sq.max()))
        by_lat = np.argsort(alat, kind="stable")  # segment indices ordered by start latitude
        alat_sorted = alat[by_lat]
        # Latitude half-window in degrees, widened a hair so a segment sitting exactly
        # on the boundary is never dropped (extra segments can only help).
        half_lat_deg = reach / MILES_PER_DEG_LAT * 1.001

    for k in range(n):
        plat, plng = lat_arr[k], lng_arr[k]
        if max_perp_miles is None:
            sel: slice | NDArray[np.intp] = slice(None)
        else:
            lo = np.searchsorted(alat_sorted, plat - half_lat_deg, side="left")
            hi = np.searchsorted(alat_sorted, plat + half_lat_deg, side="right")
            near = by_lat[lo:hi]
            near = near[np.abs(alng[near] - plng) * deg_lng[near] <= reach]
            if near.size == 0:
                continue  # every segment is farther than max_perp_miles
            sel = np.sort(near)  # original order, so ties still resolve to the first segment
        px = (plng - alng[sel]) * deg_lng[sel]
        py = (plat - alat[sel]) * MILES_PER_DEG_LAT
        dot = px * bx[sel] + py * by[sel]
        frac = np.zeros_like(dot)
        np.divide(dot, seg_sq[sel], out=frac, where=nonzero[sel])
        frac = np.clip(frac, 0.0, 1.0)
        proj_x, proj_y = frac * bx[sel], frac * by[sel]
        perp = np.hypot(px - proj_x, py - proj_y)
        j = int(np.argmin(perp))
        perps[k] = perp[j]
        markers[k] = cum_a[sel][j] + math.hypot(proj_x[j], proj_y[j])
    return markers, perps


def project_to_route(
    plat: float,
    plng: float,
    route: Sequence[tuple[float, float]],
    cum: Sequence[float],
) -> tuple[float, float]:
    """Project one point onto a route polyline.

    Returns (mile_marker, perp_miles): distance from the route start to the
    nearest point on the route, and the perpendicular detour distance in miles.
    Single-point convenience wrapper around ``project_points_to_route``.
    """
    markers, perps = project_points_to_route([plat], [plng], route, cum)
    return float(markers[0]), float(perps[0])
