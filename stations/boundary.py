"""Is a coordinate inside the USA? An offline point-in-polygon test.

The boundary is a vendored Natural Earth 1:10m country polygon (public domain)
for the 50 states + DC -- ``data/us_boundary_ne10m.geojson``, built by
``scripts/build_us_boundary.py``: 344 polygons, ~36k vertices, coastline
generalised to roughly 1-2 km. It includes the Aleutians beyond the antimeridian
and counts rural interior points as US soil (a nearest-city heuristic would not).
US territories (Puerto Rico, Guam, ...) are excluded, matching the ``US_STATES``
filter applied to the fuel data.

Two refinements handle the generalisation:

* Every edge is classified (at build time) as a *land/lake border* shared with
  Canada or Mexico, or as *coastline*.
* A point outside the polygon is still accepted when it lies within
  ``COASTAL_TOLERANCE_MILES`` of a coastline edge and no border edge is nearer.
  That rescues downtowns the coarse coastline puts "in the water" (Miami, Nome,
  Juneau, Charleston sit 0.2-0.5 mi off it) while border-city centres such as
  Windsor, Tijuana or Nogales, Sonora (0.6-4 mi past a border edge) stay out.
"""

from __future__ import annotations

import json
import math
import threading
from functools import cache
from pathlib import Path

import numpy as np
from django.conf import settings

MILES_PER_DEG = 69.0934  # great-circle miles per degree of latitude
COASTAL_TOLERANCE_MILES = 1.5


class _Ring:
    """One closed polygon ring: vertices, per-edge border flags, bounding box."""

    __slots__ = ("min_x", "min_y", "max_x", "max_y", "xs", "ys", "xs_prev", "ys_prev", "is_border")

    def __init__(self, coords: list[list[float]], border_edges: np.ndarray) -> None:
        xy = np.asarray(coords, dtype=float)
        self.xs, self.ys = xy[:, 0], xy[:, 1]  # x = longitude, y = latitude
        # Edge i runs from vertex i-1 to vertex i; rolling by one pairs the first
        # vertex with the last (identical in a closed ring), which closes it.
        self.xs_prev, self.ys_prev = np.roll(self.xs, 1), np.roll(self.ys, 1)
        self.is_border = border_edges
        self.min_x, self.max_x = float(self.xs.min()), float(self.xs.max())
        self.min_y, self.max_y = float(self.ys.min()), float(self.ys.max())

    def near_bbox(self, x: float, y: float, pad_x: float, pad_y: float) -> bool:
        return (self.min_x - pad_x <= x <= self.max_x + pad_x) and (
            self.min_y - pad_y <= y <= self.max_y + pad_y
        )

    def contains(self, x: float, y: float) -> bool:
        """Even-odd ray casting (PNPoly), vectorised over the ring's edges."""
        if not self.near_bbox(x, y, 0.0, 0.0):
            return False
        crosses = (self.ys > y) != (self.ys_prev > y)  # edges straddling the horizontal ray
        if not crosses.any():
            return False
        x1, y1 = self.xs[crosses], self.ys[crosses]
        x0, y0 = self.xs_prev[crosses], self.ys_prev[crosses]
        x_at_y = x1 + (y - y1) * (x0 - x1) / (y0 - y1)  # y0 != y1 wherever `crosses`
        return bool(np.count_nonzero(x < x_at_y) % 2)

    def edge_distances_miles(self, x: float, y: float) -> tuple[float, float]:
        """(nearest coastline edge, nearest border edge) in miles, using a local
        equirectangular plane centred on the point (accurate for a few miles)."""
        kx = math.cos(math.radians(y)) * MILES_PER_DEG
        x1, y1 = (self.xs - x) * kx, (self.ys - y) * MILES_PER_DEG
        x0, y0 = (self.xs_prev - x) * kx, (self.ys_prev - y) * MILES_PER_DEG
        dx, dy = x1 - x0, y1 - y0
        seg_sq = dx * dx + dy * dy
        safe = np.where(seg_sq > 0.0, seg_sq, 1.0)
        t = np.clip(-(x0 * dx + y0 * dy) / safe, 0.0, 1.0)
        dist = np.hypot(x0 + t * dx, y0 + t * dy)
        coast = dist[~self.is_border]
        border = dist[self.is_border]
        return (
            float(coast.min()) if coast.size else math.inf,
            float(border.min()) if border.size else math.inf,
        )


class USBoundary:
    """Lazily loads the vendored GeoJSON once per process (thread-safe)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.US_BOUNDARY_GEOJSON_PATH)
        self._polygons: list[tuple[_Ring, list[_Ring]]] | None = None  # (outer, holes)
        self._bbox = (0.0, 0.0, 0.0, 0.0)
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._polygons is not None:
            return
        with self._lock:
            if self._polygons is not None:
                return
            feature = json.loads(self.path.read_text(encoding="utf-8"))
            geometry = feature["geometry"]
            polygons = (
                geometry["coordinates"]
                if geometry["type"] == "MultiPolygon"
                else [geometry["coordinates"]]
            )
            border_runs: dict[tuple[int, int], list[tuple[int, int]]] = {}
            for p, r, first, last in feature["properties"].get("border_edges", []):
                border_runs.setdefault((p, r), []).append((first, last))

            loaded = []
            for p, rings in enumerate(polygons):
                built = []
                for r, coords in enumerate(rings):
                    flags = np.zeros(len(coords), dtype=bool)
                    for first, last in border_runs.get((p, r), []):
                        flags[first : last + 1] = True
                    built.append(_Ring(coords, flags))
                outer, *holes = built
                loaded.append((outer, holes))
            self._bbox = (
                min(o.min_x for o, _ in loaded),
                min(o.min_y for o, _ in loaded),
                max(o.max_x for o, _ in loaded),
                max(o.max_y for o, _ in loaded),
            )
            self._polygons = loaded

    def contains(self, lat: float, lng: float) -> bool:
        """Inside the polygon, or just off a coastline edge (see module docstring)."""
        if not (math.isfinite(lat) and math.isfinite(lng)):
            return False
        self._load()
        polygons = self._polygons or []
        x, y = lng, lat
        pad_y = COASTAL_TOLERANCE_MILES / MILES_PER_DEG
        pad_x = pad_y / max(math.cos(math.radians(lat)), 1e-6)
        min_x, min_y, max_x, max_y = self._bbox
        if not (min_x - pad_x <= x <= max_x + pad_x and min_y - pad_y <= y <= max_y + pad_y):
            return False

        for outer, holes in polygons:
            if outer.contains(x, y):
                return not any(hole.contains(x, y) for hole in holes)

        # Outside every polygon: allow the coastal tolerance, but never across a border.
        nearest_coast = nearest_border = math.inf
        for outer, holes in polygons:
            for ring in (outer, *holes):
                if ring.near_bbox(x, y, pad_x, pad_y):
                    coast, border = ring.edge_distances_miles(x, y)
                    nearest_coast, nearest_border = (
                        min(nearest_coast, coast),
                        min(nearest_border, border),
                    )
        return nearest_coast <= COASTAL_TOLERANCE_MILES and nearest_coast < nearest_border


@cache
def get_us_boundary() -> USBoundary:
    """Process-wide singleton so the GeoJSON is parsed only once."""
    return USBoundary()


def in_usa(lat: float, lng: float) -> bool:
    """True when the point lies on the land of the 50 US states or DC (or just off its coast)."""
    return get_us_boundary().contains(lat, lng)
