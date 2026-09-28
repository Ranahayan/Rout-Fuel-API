"""In-memory spatial index of geocoded fuel stations.

Built ONCE per process from the FuelStation table (the table is the source of
truth; this index is a rebuildable cache). At ~6.6k points the scipy cKDTree is
built in tens of milliseconds, so per-request corridor queries are sub-millisecond.

Stations are embedded in a local "flat" XYZ space on the unit sphere so that a
Euclidean radius on the tree corresponds to a great-circle distance, letting us
query "all stations within R miles of a point" directly.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from trips.geo import EARTH_RADIUS_MILES


@dataclass
class Station:
    opis_id: int
    name: str
    city: str
    state: str
    price: float
    lat: float
    lng: float


def _to_unit_xyz(lats: np.ndarray, lngs: np.ndarray) -> np.ndarray:
    rlat = np.radians(lats)
    rlng = np.radians(lngs)
    x = np.cos(rlat) * np.cos(rlng)
    y = np.cos(rlat) * np.sin(rlng)
    z = np.sin(rlat)
    return np.column_stack([x, y, z])


def _chord_for_miles(miles: float) -> float:
    """Euclidean chord length on the unit sphere for a given surface distance."""
    central_angle = miles / EARTH_RADIUS_MILES
    return 2.0 * math.sin(central_angle / 2.0)


class StationSpatialIndex:
    def __init__(self, stations: list[Station]):
        self.stations = stations
        if stations:
            lats = np.array([s.lat for s in stations])
            lngs = np.array([s.lng for s in stations])
            self._tree = cKDTree(_to_unit_xyz(lats, lngs))
        else:
            self._tree = None

    def query_near_points(
        self, points: list[tuple[float, float]], radius_miles: float
    ) -> list[int]:
        """Return indices of stations within ``radius_miles`` of ANY of the
        given [(lat, lng), ...] points (the union, deduplicated)."""
        if self._tree is None or not points:
            return []
        lats = np.array([p[0] for p in points])
        lngs = np.array([p[1] for p in points])
        xyz = _to_unit_xyz(lats, lngs)
        chord = _chord_for_miles(radius_miles)
        results = self._tree.query_ball_point(xyz, r=chord)
        found: set[int] = set()
        for idx_list in results:
            found.update(idx_list)
        return sorted(found)


_index_lock = threading.Lock()
_index: StationSpatialIndex | None = None


def _load_stations() -> list[Station]:
    from stations.models import FuelStation

    rows = FuelStation.objects.filter(
        is_geocoded=True, latitude__isnull=False, longitude__isnull=False
    ).values_list("opis_id", "name", "city", "state", "retail_price", "latitude", "longitude")
    # retail_price is a DecimalField (the DB is the money source of truth); we
    # cast to float here because the optimizer runs heavy float arithmetic. The
    # reported dollar amounts are quantized back to cents with Decimal in
    # trips.services._serialize, so this cast only affects intermediate
    # precision, never the cents shown to the caller.
    return [
        Station(
            opis_id=r[0],
            name=r[1],
            city=r[2],
            state=r[3],
            price=float(r[4]),
            lat=r[5],
            lng=r[6],
        )
        for r in rows
    ]


def get_index() -> StationSpatialIndex:
    """Process-wide singleton, built lazily on first use."""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = StationSpatialIndex(_load_stations())
    return _index


def reset_index() -> None:
    """Drop the cached index (used by tests after loading fixture data)."""
    global _index
    with _index_lock:
        _index = None
