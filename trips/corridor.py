"""Select fuel stations within a corridor of the route and place each at a
distance-from-start "mile marker".

The KD-tree is used only as a fast prefilter (a generous radius around densified
route points); exact corridor membership is then decided by the perpendicular
distance from each candidate to the route polyline, computed for all candidates
at once with numpy. Mile markers are scaled to the provider's reported road
distance so the downstream 500-mile range checks use consistent units.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from django.conf import settings

from trips.geo import FloatArray, project_points_to_route, segment_miles
from trips.spatial_index import Station, get_index


@dataclass
class Candidate:
    station: Station
    mile_marker: float  # distance from route start, miles (scaled to road distance)
    detour_miles: float  # perpendicular distance from the route, miles


def _densify(
    points: list[tuple[float, float]],
    spacing: float,
    seg_miles: FloatArray | None = None,
) -> list[tuple[float, float]]:
    """Insert intermediate points so consecutive query points are <= spacing
    apart, ensuring the KD-tree prefilter cannot skip over stations that sit
    near the middle of a long, sparsely-sampled segment.

    ``seg_miles`` (per-segment lengths) may be passed in to avoid recomputing
    them. Only segments longer than ``spacing`` need work; with full-resolution
    routing geometry that is a handful out of tens of thousands.
    """
    if len(points) < 2:
        return list(points)
    if seg_miles is None:
        seg_miles = segment_miles(points)
    long_segments = np.flatnonzero(seg_miles > spacing)
    if len(long_segments) == 0:
        return list(points)

    out: list[tuple[float, float]] = []
    start = 0
    for i in long_segments:
        out.extend(points[start : i + 1])  # vertices up to and including this segment's start
        (lat1, lng1), (lat2, lng2) = points[i], points[i + 1]
        dist = float(seg_miles[i])
        for k in range(1, int(dist // spacing) + 1):
            frac = k * spacing / dist
            if frac >= 1.0:
                break
            out.append((lat1 + (lat2 - lat1) * frac, lng1 + (lng2 - lng1) * frac))
        start = i + 1
    out.extend(points[start:])
    return out


def find_candidates(route, buffer_miles: float | None = None) -> tuple[list[Candidate], float]:
    """Return (candidates ordered by mile marker, total_distance_miles)."""
    if buffer_miles is None:
        buffer_miles = settings.CORRIDOR_BUFFER_MILES

    points = route.points
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    seg = segment_miles(pts)
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    polyline_len = float(cum[-1])
    total_miles = route.total_distance_miles
    # Scale haversine polyline positions to the provider's road distance.
    scale = (total_miles / polyline_len) if polyline_len > 0 else 1.0

    index = get_index()
    query_pts = _densify(points, spacing=buffer_miles, seg_miles=seg)
    # Generous prefilter radius (superset); exact perp filter below trims it.
    idxs = index.query_near_points(query_pts, radius_miles=buffer_miles * 2.0)
    if not idxs:
        return [], total_miles

    stations = [index.stations[i] for i in idxs]
    markers, perps = project_points_to_route(
        [s.lat for s in stations],
        [s.lng for s in stations],
        pts,
        cum,
        max_perp_miles=buffer_miles,
    )
    candidates = [
        Candidate(station=s, mile_marker=float(marker) * scale, detour_miles=float(perp))
        for s, marker, perp in zip(stations, markers, perps, strict=True)
        if perp <= buffer_miles
    ]
    candidates.sort(key=lambda c: c.mile_marker)
    return candidates, total_miles
