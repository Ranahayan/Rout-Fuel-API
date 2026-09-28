"""Pure geographic math on (lat, lng) coordinates, in miles.

We deliberately use haversine arc-length rather than planar projection: 1 degree
of longitude varies from ~69 mi at the equator to ~49 mi at 49 N, so projecting
raw lon/lat onto a polyline with planar math distorts distances by 20-30% on a
transcontinental route -- which would corrupt the hard 500-mile range constraint.
"""

from __future__ import annotations

import math

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in miles."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def cumulative_miles(points: list[tuple[float, float]]) -> list[float]:
    """Cumulative arc-length (miles) at each vertex of a [(lat, lng), ...] path.

    Returns a list the same length as ``points`` where index 0 is 0.0 and the
    last entry is the total polyline length.
    """
    cum = [0.0]
    for (lat1, lng1), (lat2, lng2) in zip(points, points[1:], strict=False):
        cum.append(cum[-1] + haversine_miles(lat1, lng1, lat2, lng2))
    return cum


def _project_point_on_segment(
    plat: float,
    plng: float,
    alat: float,
    alng: float,
    blat: float,
    blng: float,
) -> tuple[float, float, float]:
    """Project point P onto segment A->B using a local equirectangular plane
    centred at A (accurate for the short segments of a road polyline).

    Returns (frac, perp_miles, along_miles): the clamped fraction along the
    segment, the perpendicular distance from P to the segment in miles, and the
    along-segment distance from A to the projection in miles.
    """
    cos_lat = math.cos(math.radians(alat))
    deg_lat = 69.0  # miles per degree latitude (approx, good enough locally)
    deg_lng = 69.0 * cos_lat

    # A is the local origin (0, 0); B and P are expressed relative to it.
    bx, by = (blng - alng) * deg_lng, (blat - alat) * deg_lat
    px, py = (plng - alng) * deg_lng, (plat - alat) * deg_lat

    seg_len_sq = bx * bx + by * by
    if seg_len_sq == 0.0:
        frac = 0.0
    else:
        frac = (px * bx + py * by) / seg_len_sq
        frac = max(0.0, min(1.0, frac))

    proj_x, proj_y = frac * bx, frac * by
    perp = math.hypot(px - proj_x, py - proj_y)
    along = math.hypot(proj_x, proj_y)
    return frac, perp, along


def project_to_route(
    plat: float,
    plng: float,
    route: list[tuple[float, float]],
    cum: list[float],
) -> tuple[float, float]:
    """Project a point onto a route polyline.

    ``route`` is [(lat, lng), ...]; ``cum`` is the matching cumulative_miles list.
    Returns (mile_marker, perp_miles): distance from the route start to the
    nearest point on the route, and the perpendicular detour distance in miles.
    """
    best_perp = float("inf")
    best_marker = 0.0
    for i in range(len(route) - 1):
        alat, alng = route[i]
        blat, blng = route[i + 1]
        frac, perp, along = _project_point_on_segment(plat, plng, alat, alng, blat, blng)
        if perp < best_perp:
            best_perp = perp
            best_marker = cum[i] + along
    return best_marker, best_perp
