"""Load the OPIS fuel-price CSV into the FuelStation table.

Pipeline (idempotent):
  1. Parse with the csv module (handles CRLF + quoted commas in Address);
     refuse to run if the expected columns are missing.
  2. Drop non-US rows (Canadian provinces) and malformed rows.
  3. Dedup by OPIS ID, keeping the LOWEST of any conflicting prices
     ("best available price at that truckstop" -- a cost-lens choice), and store
     prices at the model's 4-decimal precision on every database backend.
  4. Geocode each station offline via (City, State) -> centroid join; abort if
     the hit rate is suspiciously low (wrong file, wrong city dataset).
  5. Upsert in one bulk statement keyed on opis_id (re-run -> identical state);
     optionally prune stations that disappeared from the file.
  6. Print a coverage report.

Guard rails: nothing is written -- and with ``--fresh`` nothing is deleted --
unless at least one valid US row was parsed, so a bad file can never empty the
table and still exit 0.

Usage:
    python manage.py load_fuel_prices [--csv PATH] [--fresh] [--prune] [--min-geocode-rate 0.95]
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from stations.constants import US_STATES
from stations.geocoding import get_default_geocoder
from stations.models import FuelStation

REQUIRED_COLUMNS = {
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
}
PRICE_PRECISION = Decimal(
    "0.0001"
)  # matches FuelStation.retail_price (max_digits=6, decimal_places=4)
UPSERT_FIELDS = [
    "name",
    "address",
    "city",
    "state",
    "rack_id",
    "retail_price",
    "latitude",
    "longitude",
    "is_geocoded",
    "updated_at",
]


@dataclass
class LoadSummary:
    rows_read: int = 0
    skipped_non_us: int = 0
    skipped_bad: int = 0
    duplicate_ids: int = 0
    price_conflicts: int = 0
    stations: int = 0
    geocoded: int = 0
    pruned: int = 0
    unmatched: Counter = field(default_factory=Counter)

    @property
    def geocode_rate(self) -> float:
        return self.geocoded / self.stations if self.stations else 0.0


def parse_rows(csv_path: str, summary: LoadSummary) -> dict[int, dict]:
    """Parse, filter and dedupe the CSV into ``{opis_id: row}`` (lowest price wins)."""
    best: dict[int, dict] = {}
    with open(csv_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise CommandError(
                f"{csv_path} is missing the column(s) {sorted(missing)}; "
                f"expected {sorted(REQUIRED_COLUMNS)}."
            )
        for raw in reader:
            summary.rows_read += 1
            state = (raw.get("State") or "").strip().upper()
            if state not in US_STATES:
                summary.skipped_non_us += 1
                continue
            try:
                opis_id = int((raw.get("OPIS Truckstop ID") or "").strip())
                price = Decimal((raw.get("Retail Price") or "").strip())
            except (TypeError, ValueError, InvalidOperation):
                summary.skipped_bad += 1
                continue
            rack_raw = (raw.get("Rack ID") or "").strip()
            try:
                rack_id = int(rack_raw) if rack_raw else None
            except ValueError:
                rack_id = None

            row = {
                "opis_id": opis_id,
                "name": (raw.get("Truckstop Name") or "").strip(),
                "address": (raw.get("Address") or "").strip(),
                "city": (raw.get("City") or "").strip(),
                "state": state,
                "rack_id": rack_id,
                "retail_price": price,
            }
            existing = best.get(opis_id)
            if existing is None:
                best[opis_id] = row
                continue
            summary.duplicate_ids += 1
            if price != existing["retail_price"]:
                summary.price_conflicts += 1
            if price < existing["retail_price"]:  # keep the lowest price for this truckstop
                best[opis_id] = row
    for row in best.values():  # store at the model's precision on every backend
        row["retail_price"] = row["retail_price"].quantize(PRICE_PRECISION)
    summary.stations = len(best)
    return best


def geocode_rows(best: dict[int, dict], summary: LoadSummary) -> None:
    geocoder = get_default_geocoder()
    for row in best.values():
        coords = geocoder.geocode(row["city"], row["state"])
        if coords:
            row["latitude"], row["longitude"] = coords
            row["is_geocoded"] = True
            summary.geocoded += 1
        else:
            row["latitude"] = row["longitude"] = None
            row["is_geocoded"] = False
            summary.unmatched[(row["city"], row["state"])] += 1


def persist(best: dict[int, dict], *, fresh: bool, prune: bool, summary: LoadSummary) -> None:
    """One transaction: replace everything (--fresh), or upsert by opis_id and
    optionally delete rows the file no longer contains (--prune)."""
    stamp = timezone.now()
    objs = [FuelStation(updated_at=stamp, **row) for row in best.values()]
    with transaction.atomic():
        if fresh:
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(objs, batch_size=500)
        else:
            FuelStation.objects.bulk_create(
                objs,
                batch_size=500,
                update_conflicts=True,
                unique_fields=["opis_id"],
                update_fields=UPSERT_FIELDS,
            )
            if prune:  # anything this load did not touch is gone from the file
                summary.pruned, _ = FuelStation.objects.filter(updated_at__lt=stamp).delete()


def load_fuel_prices(
    csv_path: str, *, fresh: bool = False, prune: bool = False, min_geocode_rate: float = 0.95
) -> LoadSummary:
    """The whole pipeline; raises CommandError instead of writing a bad state."""
    summary = LoadSummary()
    best = parse_rows(csv_path, summary)
    if not best:
        raise CommandError(
            f"No valid US station rows found in {csv_path}; refusing to write (or delete) anything."
        )
    geocode_rows(best, summary)
    if summary.geocode_rate < min_geocode_rate:
        sample = ", ".join(f"{c}, {s}" for (c, s), _ in summary.unmatched.most_common(5))
        raise CommandError(
            f"Only {summary.geocode_rate:.1%} of stations geocoded (minimum {min_geocode_rate:.0%}); "
            f"aborting before writing. Unmatched e.g. {sample}. Check --csv and US_CITIES_CSV_PATH, "
            f"or lower --min-geocode-rate."
        )
    persist(best, fresh=fresh, prune=prune, summary=summary)
    return summary


class Command(BaseCommand):
    help = "Load and geocode the OPIS fuel-price CSV into the FuelStation table."

    def add_arguments(self, parser):
        parser.add_argument("--csv", dest="csv_path", default=settings.FUEL_CSV_PATH)
        parser.add_argument(
            "--fresh",
            action="store_true",
            help="Delete all existing stations before loading (only after the file parsed OK).",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete stations that are no longer present in the CSV.",
        )
        parser.add_argument(
            "--min-geocode-rate",
            type=float,
            default=0.95,
            help="Abort if fewer than this fraction of stations geocode (default 0.95).",
        )

    def handle(self, *args, **options):
        summary = load_fuel_prices(
            options["csv_path"],
            fresh=options["fresh"],
            prune=options["prune"],
            min_geocode_rate=options["min_geocode_rate"],
        )
        out = self.stdout.write
        out(self.style.SUCCESS("Fuel price load complete."))
        out(f"  Rows read .................. {summary.rows_read}")
        out(f"  Skipped (non-US) ........... {summary.skipped_non_us}")
        out(f"  Skipped (malformed) ........ {summary.skipped_bad}")
        out(
            f"  Duplicate ID rows collapsed  {summary.duplicate_ids} "
            f"({summary.price_conflicts} with conflicting prices)"
        )
        out(f"  Distinct stations written .. {summary.stations}")
        out(
            f"  Geocoded ................... {summary.geocoded}/{summary.stations} "
            f"({summary.geocode_rate:.1%})"
        )
        out(f"  Geocode misses (excluded) .. {summary.stations - summary.geocoded}")
        if options["prune"]:
            out(f"  Pruned (absent from CSV) ... {summary.pruned}")
        if summary.unmatched:
            sample = ", ".join(f"{c}, {s}" for (c, s), _ in summary.unmatched.most_common(10))
            out(f"  Unmatched (city,state) ..... {len(summary.unmatched)} distinct; e.g. {sample}")
