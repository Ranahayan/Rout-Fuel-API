"""Offline (City, State) -> (lat, lng) geocoding.

The fuel CSV has no coordinates, and the assignment forbids hammering a routing
/ geocoding API. We therefore resolve coordinates entirely offline by joining
``(City, State)`` against a vendored US-cities centroid dataset
(``data/uscities.csv``, kelvins/US-Cities-Database, MIT). Coverage of the fuel
data's distinct city/state pairs is ~99.9%.

The same geocoder resolves request endpoints given as place names, which is
what keeps a typical request to a single routing API call. Accepted forms:
``"City, ST"``, ``"City, State Name"``, ``"City ST"`` (no comma) and a bare
city name when it exists in only one state.

Two quirks of the data are handled here:

* Spelling variants -- ``normalize_city`` folds punctuation, spacing and common
  abbreviations so "Winston-Salem"/"Winston Salem", "O'Fallon"/"O Fallon",
  "St. Louis"/"Saint Louis" and "McDonald"/"Mc Donald" share one key. Checked
  against the whole dataset: the folding never merges two distinct places.
* Multi-county cities -- the dataset has one row per (city, county), sorted by
  county, so naively taking the first row puts Orlando in Brevard County, 48 mi
  from Orlando. ``_principal_row`` prefers the county named like the city, then
  a curated table of principal counties, and only then the first row.
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from django.conf import settings

from stations.constants import US_STATES

logger = logging.getLogger(__name__)

Coords = tuple[float, float]

STATE_NAME_TO_CODE = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC", "WASHINGTON DC": "DC",
}  # fmt: skip

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
# Applied on whole words (after punctuation has become spaces) so the dataset's
# spelled-out forms and the fuel CSV's / users' abbreviations meet in the middle.
_ABBREVIATIONS = [
    (re.compile(r"\bST\b"), "SAINT"),
    (re.compile(r"\bFT\b"), "FORT"),
    (re.compile(r"\bMT\b"), "MOUNT"),
    (re.compile(r"\bN\b"), "NORTH"),
    (re.compile(r"\bS\b"), "SOUTH"),
    (re.compile(r"\bE\b"), "EAST"),
    (re.compile(r"\bW\b"), "WEST"),
    (re.compile(r"\bHTS\b"), "HEIGHTS"),
    (re.compile(r"\bSPGS\b"), "SPRINGS"),
    (re.compile(r"\bJCT\b"), "JUNCTION"),
]


def normalize_city(name: str | None) -> str:
    """Fold a city name to a comparison key: uppercase, punctuation to spaces,
    common abbreviations expanded, then every non-alphanumeric dropped.

    "Winston-Salem" -> "WINSTONSALEM", "O'Fallon" -> "OFALLON", "St. Louis" and
    "Saint Louis" -> "SAINTLOUIS", "Mc Donald" and "McDonald" -> "MCDONALD".
    """
    # Apostrophes vanish rather than split ("Lee's" -> LEES, not a stray "S" that
    # the direction rule below would read as SOUTH); other punctuation splits words.
    text = _NON_ALNUM.sub(" ", (name or "").upper().replace("'", "").replace("\u2019", ""))
    for pattern, replacement in _ABBREVIATIONS:
        text = pattern.sub(replacement, text)
    return text.replace(" ", "")


def normalize_state(text: str | None) -> str | None:
    """'IL', 'il', 'D.C.', 'Illinois', 'New York' -> the two-letter code; None if
    the text is not a US state or DC."""
    key = _NON_ALNUM.sub(" ", (text or "").upper()).strip()
    if not key:
        return None
    compact = key.replace(" ", "")
    if compact in US_STATES:
        return compact
    return STATE_NAME_TO_CODE.get(key)


# Principal (downtown) county for multi-county cities whose principal county is
# not named like the city. Keys are normalised (city, state); values are the
# normalised COUNTY column of data/uscities.csv. Cities whose duplicate rows lie
# within ~3 mi of each other are not listed: the corridor buffer absorbs that.
PRINCIPAL_COUNTY: dict[tuple[str, str], str] = {
    ("ORLANDO", "FL"): "ORANGE", ("AUSTIN", "TX"): "TRAVIS", ("JACKSONVILLE", "FL"): "DUVAL",
    ("SANANTONIO", "TX"): "BEXAR", ("SAINTPAUL", "MN"): "RAMSEY", ("OMAHA", "NE"): "DOUGLAS",
    ("FORTWASHINGTON", "PA"): "MONTGOMERY", ("TRENTON", "NJ"): "MERCER", ("CINCINNATI", "OH"): "HAMILTON",
    ("HOUSTON", "TX"): "HARRIS", ("KISSIMMEE", "FL"): "OSCEOLA", ("WEWAHITCHKA", "FL"): "GULF",
    ("INDIANAPOLIS", "IN"): "MARION", ("LITTLETON", "CO"): "ARAPAHOE", ("TAMPA", "FL"): "HILLSBOROUGH",
    ("BIRMINGHAM", "AL"): "JEFFERSON", ("QUEENCREEK", "AZ"): "MARICOPA", ("COLUMBIA", "SC"): "RICHLAND",
    ("LONGMONT", "CO"): "BOULDER", ("LEANDER", "TX"): "WILLIAMSON", ("ROCKFORD", "MN"): "WRIGHT",
    ("MATTHEWS", "NC"): "MECKLENBURG", ("AUGUSTA", "GA"): "RICHMOND", ("KANSASCITY", "MO"): "JACKSON",
    ("SIOUXFALLS", "SD"): "MINNEHAHA", ("SPRING", "TX"): "HARRIS", ("PORTLAND", "OR"): "MULTNOMAH",
    ("BOTHELL", "WA"): "KING", ("KATY", "TX"): "HARRIS", ("BROKENARROW", "OK"): "TULSA",
    ("LIMA", "OH"): "ALLEN", ("SEBASTIAN", "FL"): "INDIANRIVER", ("SPRINGHILL", "FL"): "HERNANDO",
    ("KINGOFPRUSSIA", "PA"): "MONTGOMERY", ("BRIGHTON", "CO"): "ADAMS", ("PHENIXCITY", "AL"): "RUSSELL",
    ("SALEM", "OR"): "MARION", ("NAPERVILLE", "IL"): "DUPAGE", ("FORTMILL", "SC"): "YORK",
    ("MINNEAPOLIS", "MN"): "HENNEPIN", ("DUNNELLON", "FL"): "MARION", ("MABANK", "TX"): "KAUFMAN",
    ("ROCKYMOUNT", "NC"): "NASH", ("ATLANTA", "GA"): "FULTON", ("LANSING", "MI"): "INGHAM",
    ("DAYTON", "OH"): "MONTGOMERY", ("ENGLEWOOD", "FL"): "SARASOTA", ("CHANTILLY", "VA"): "FAIRFAX",
    ("LOSGATOS", "CA"): "SANTACLARA", ("HUNTINGTON", "WV"): "CABELL", ("AMARILLO", "TX"): "POTTER",
    ("COMMERCE", "GA"): "JACKSON", ("DULUTH", "GA"): "GWINNETT", ("COLUMBUS", "OH"): "FRANKLIN",
    ("LAUREL", "MD"): "PRINCEGEORGES", ("VALLEYFORGE", "PA"): "MONTGOMERY", ("SEDONA", "AZ"): "YAVAPAI",
    ("MIDDLETOWN", "VA"): "FREDERICK", ("NORRISTOWN", "PA"): "MONTGOMERY",
}  # fmt: skip


@dataclass(frozen=True)
class _CityRow:
    county: str  # normalised COUNTY column ("" when blank)
    lat: float
    lng: float


def _principal_row(city_key: str, state: str, rows: list[_CityRow]) -> tuple[_CityRow, bool]:
    """Pick the row for a city listed under several counties. Returns
    (row, resolved) where ``resolved`` is False for the first-row fallback."""
    if len(rows) == 1:
        return rows[0], True
    wanted = PRINCIPAL_COUNTY.get((city_key, state))
    if wanted is not None:
        for row in rows:
            if row.county == wanted:
                return row, True
    # Independent cities ("Richmond City", "Saint Louis City") and counties named
    # like the city ("Philadelphia", "Denver", "Dallas").
    for want in (city_key + "CITY", city_key):
        hits = [row for row in rows if row.county == want]
        if len(hits) == 1:
            return hits[0], True
    # "North Charleston" in Charleston County, "New Orleans" in Orleans Parish.
    hits = [row for row in rows if row.county and row.county in city_key]
    if len(hits) == 1:
        return hits[0], True
    return rows[0], False


class CityGeocoder:
    """Loads the vendored city centroid dataset once into in-memory dicts."""

    def __init__(self, csv_path: str | Path | None = None) -> None:
        self.csv_path = Path(csv_path or settings.US_CITIES_CSV_PATH)
        self._index: dict[tuple[str, str], Coords] = {}  # (city key, state) -> coords
        self._by_city: dict[str, dict[str, Coords]] = {}  # city key -> {state: coords}
        self.unresolved_duplicates = 0
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
            grouped: dict[tuple[str, str], list[_CityRow]] = {}
            with open(self.csv_path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    state = (row.get("STATE_CODE") or "").strip().upper()
                    try:
                        lat, lng = float(row["LATITUDE"]), float(row["LONGITUDE"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    key = (normalize_city(row.get("CITY", "")), state)
                    grouped.setdefault(key, []).append(
                        _CityRow(normalize_city(row.get("COUNTY", "")), lat, lng)
                    )
            unresolved = 0
            for (city_key, state), rows in grouped.items():
                chosen, resolved = _principal_row(city_key, state, rows)
                unresolved += not resolved
                self._index[(city_key, state)] = (chosen.lat, chosen.lng)
                self._by_city.setdefault(city_key, {})[state] = (chosen.lat, chosen.lng)
            self.unresolved_duplicates = unresolved
            logger.debug(
                "City geocoder loaded %d places; %d multi-county cities fell back to their first row",
                len(self._index),
                unresolved,
            )
            self._loaded = True

    def geocode(self, city: str, state: str) -> Coords | None:
        """(lat, lng) for a city and a state code or name, or None if not found."""
        self._load()
        code = normalize_state(state)
        if code is None:
            return None
        return self._index.get((normalize_city(city), code))

    def states_for_city(self, city: str) -> list[str]:
        """State codes in which a bare city name exists (for ambiguity hints)."""
        self._load()
        return sorted(self._by_city.get(normalize_city(city), {}))

    def geocode_place_name(self, text: str) -> Coords | None:
        """Resolve a free-form place name to (lat, lng), or None.

        "Chicago, IL" | "Chicago, Illinois" | "Chicago IL" | "Kansas City Missouri"
        | "Washington, D.C." | a bare "Chicago" when only one state has it.
        """
        text = (text or "").strip()
        if not text:
            return None
        self._load()
        if "," in text:
            city, _, state = text.rpartition(",")
            return self.geocode(city, state)
        words = text.split()
        for n in (
            1,
            2,
            3,
        ):  # trailing words may spell a state: "IL", "New York", "District of Columbia"
            if len(words) > n:
                code = normalize_state(" ".join(words[-n:]))
                if code is not None:
                    hit = self._index.get((normalize_city(" ".join(words[:-n])), code))
                    if hit is not None:
                        return hit
        states = self._by_city.get(normalize_city(text), {})
        if len(states) == 1:
            return next(iter(states.values()))
        return None


@cache
def get_default_geocoder() -> CityGeocoder:
    """Process-wide singleton so the dataset is parsed only once."""
    return CityGeocoder()
