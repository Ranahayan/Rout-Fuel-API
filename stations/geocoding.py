"""Offline (City, State) -> (lat, lng) geocoding.

The fuel CSV has no coordinates, and the assignment forbids hammering a routing
/ geocoding API. We therefore resolve coordinates entirely offline by joining
``(City, State)`` against a vendored US-cities centroid dataset
(``data/uscities.csv``, kelvins/US-Cities-Database, MIT). Measured coverage of
the fuel data's distinct city/state pairs is ~99.8%.

The same geocoder resolves request endpoints given as ``"City, ST"`` strings,
which is what keeps a typical request to a single routing API call.
"""

from __future__ import annotations

import csv
import re
import threading
from functools import lru_cache
from pathlib import Path

from django.conf import settings

# Common abbreviation expansions so "St."/"Ft."/"Mt." match the dataset spelling.
_ABBREV = [
    (re.compile(r"\bST\b"), "SAINT"),
    (re.compile(r"\bFT\b"), "FORT"),
    (re.compile(r"\bMT\b"), "MOUNT"),
]


def normalize_city(name: str) -> str:
    """Uppercase, drop punctuation, expand common abbreviations, squeeze spaces."""
    s = (name or "").strip().upper().replace(".", "")
    for pattern, repl in _ABBREV:
        s = pattern.sub(repl, s)
    return re.sub(r"\s+", " ", s).strip()


class CityGeocoder:
    """Loads the vendored city centroid dataset once into an in-memory dict."""

    def __init__(self, csv_path: str | Path | None = None) -> None:
        self.csv_path = Path(csv_path or settings.US_CITIES_CSV_PATH)
        self._index: dict[tuple[str, str], tuple[float, float]] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._loaded:
            return
        # Double-checked locking: the ~30k-row parse must run exactly once even
        # if several requests hit a cold geocoder concurrently.
        with self._lock:
            if self._loaded:
                return
            with open(self.csv_path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    state = (row.get("STATE_CODE") or "").strip().upper()
                    try:
                        lat = float(row["LATITUDE"])
                        lng = float(row["LONGITUDE"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    # First spelling wins (dataset is already one row per city).
                    self._index.setdefault((normalize_city(row.get("CITY", "")), state), (lat, lng))
            self._loaded = True

    def geocode(self, city: str, state: str) -> tuple[float, float] | None:
        """Return (lat, lng) for a (city, state) pair, or None if not found."""
        self._load()
        return self._index.get((normalize_city(city), (state or "").strip().upper()))

    def geocode_place_name(self, text: str) -> tuple[float, float] | None:
        """Resolve a free-form ``"City, ST"`` string to (lat, lng), or None."""
        if not text or "," not in text:
            return None
        city, _, state = text.rpartition(",")
        return self.geocode(city, state)


@lru_cache(maxsize=1)
def get_default_geocoder() -> CityGeocoder:
    """Process-wide singleton so the dataset is parsed only once."""
    return CityGeocoder()
