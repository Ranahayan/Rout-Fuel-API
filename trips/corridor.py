"""Select fuel stations within a corridor of the route and place each at a
distance-from-start "mile marker".

The KD-tree is used only as a fast prefilter (a generous radius around densified
route points); exact corridor membership is then decided by the true
perpendicular distance from each candidate to the route polyline. Mile markers
are scaled to the provider's reported road distance so the downstream 500-mile
range checks use consistent units.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from trips.geo import cumulative_miles, haversine_miles, project_to_route
from trips.spatial_index import Station, get_index


@dataclass
class Candidate:
    station: Station
    mile_marker: float  # distance from route start, miles (scaled to road distance)
    detour_miles: float  # perpendicular distance from the route, miles


def _densify(points: list[tuple[float, float]], spacing: float) -> list[tuple[float, float]]:
    """Insert intermediate points so consecutive query points are <= spacing
    apart, ensuring the KD-tree prefilter cannot skip over stations that sit
    near the middle of a long, sparsely-sampled segment."""
    if len(points) < 2:
        return list(points)
    out = [points[0]]
    for (lat1, lng1), (lat2, lng2) in zip(points, points[1:], strict=False):
        dist = haversine_miles(lat1, lng1, lat2, lng2)
        if dist > spacing:
            steps = int(dist // spacing)
            for k in range(1, steps + 1):
                frac = k * spacing / dist
                if frac >= 1.0:
                    break
                out.append((lat1 + (lat2 - lat1) * frac, lng1 + (lng2 - lng1) * frac))
        out.append((lat2, lng2))
    return out


def find_candidates(route, buffer_miles: float | None = None) -> tuple[list[Candidate], float]:
    """Return (candidates ordered by mile marker, total_distance_miles)."""
    if buffer_miles is None:
        buffer_miles = settings.CORRIDOR_BUFFER_MILES

    points = route.points
    cum = cumulative_miles(points)
    polyline_len = cum[-1] if cum else 0.0
    total_miles = route.total_distance_miles
    # Scale haversine polyline positions to the provider's road distance.
    scale = (total_miles / polyline_len) if polyline_len > 0 else 1.0

    index = get_index()
    query_pts = _densify(points, spacing=buffer_miles)
    # Generous prefilter radius (superset); exact perp filter below trims it.
    idxs = index.query_near_points(query_pts, radius_miles=buffer_miles * 2.0)

    candidates: list[Candidate] = []
    for i in idxs:
        s = index.stations[i]
        marker, perp = project_to_route(s.lat, s.lng, points, cum)
        if perp <= buffer_miles:
            candidates.append(Candidate(station=s, mile_marker=marker * scale, detour_miles=perp))

    candidates.sort(key=lambda c: c.mile_marker)
    return candidates, total_miles
