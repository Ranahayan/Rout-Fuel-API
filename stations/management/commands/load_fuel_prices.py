"""Load the OPIS fuel-price CSV into the FuelStation table.

Pipeline (one-time, idempotent):
  1. Parse with the csv module (handles CRLF + quoted commas in Address).
  2. Drop non-US rows (Canadian provinces) and malformed rows.
  3. Dedup by OPIS ID, keeping the LOWEST of any conflicting prices
     ("best available price at that truckstop" — a cost-lens choice).
  4. Geocode each station offline via (City, State) -> centroid join.
  5. Persist with update_or_create keyed on opis_id (re-run -> identical state).
  6. Print a coverage report (rows read, skipped, dedup conflicts, geocode hits/misses).

Usage:
    python manage.py load_fuel_prices [--csv PATH] [--fresh]
"""

from __future__ import annotations

import csv
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from stations.constants import US_STATES
from stations.geocoding import get_default_geocoder
from stations.models import FuelStation


class Command(BaseCommand):
    help = "Load and geocode the OPIS fuel-price CSV into the FuelStation table."

    def add_arguments(self, parser):
        parser.add_argument("--csv", dest="csv_path", default=settings.FUEL_CSV_PATH)
        parser.add_argument(
            "--fresh",
            action="store_true",
            help="Delete all existing stations before loading (faster bulk path).",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        geocoder = get_default_geocoder()

        rows_read = 0
        skipped_non_us = 0
        skipped_bad = 0
        # opis_id -> chosen row dict (with the lowest price seen so far)
        best: dict[int, dict] = {}
        price_conflicts = 0
        duplicate_ids = 0

        with open(csv_path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                rows_read += 1
                state = (raw.get("State") or "").strip().upper()
                if state not in US_STATES:
                    skipped_non_us += 1
                    continue
                try:
                    opis_id = int((raw.get("OPIS Truckstop ID") or "").strip())
                    price = Decimal((raw.get("Retail Price") or "").strip())
                except (TypeError, ValueError, InvalidOperation):
                    skipped_bad += 1
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
                else:
                    duplicate_ids += 1
                    if price != existing["retail_price"]:
                        price_conflicts += 1
                    # Keep the lowest price for this truckstop.
                    if price < existing["retail_price"]:
                        best[opis_id] = row

        # Geocode the deduped set.
        geocode_hits = 0
        unmatched: Counter[tuple[str, str]] = Counter()
        for row in best.values():
            coords = geocoder.geocode(row["city"], row["state"])
            if coords:
                row["latitude"], row["longitude"] = coords
                row["is_geocoded"] = True
                geocode_hits += 1
            else:
                row["latitude"] = row["longitude"] = None
                row["is_geocoded"] = False
                unmatched[(row["city"], row["state"])] += 1

        # Persist.
        with transaction.atomic():
            if options["fresh"]:
                FuelStation.objects.all().delete()
                FuelStation.objects.bulk_create(FuelStation(**row) for row in best.values())
            else:
                for row in best.values():
                    FuelStation.objects.update_or_create(
                        opis_id=row["opis_id"],
                        defaults={k: v for k, v in row.items() if k != "opis_id"},
                    )

        total = len(best)
        misses = total - geocode_hits
        self.stdout.write(self.style.SUCCESS("Fuel price load complete."))
        self.stdout.write(f"  Rows read .................. {rows_read}")
        self.stdout.write(f"  Skipped (non-US) ........... {skipped_non_us}")
        self.stdout.write(f"  Skipped (malformed) ........ {skipped_bad}")
        self.stdout.write(
            f"  Duplicate ID rows collapsed  {duplicate_ids} ({price_conflicts} with conflicting prices)"
        )
        self.stdout.write(f"  Distinct stations written .. {total}")
        pct = (100 * geocode_hits / total) if total else 0.0
        self.stdout.write(f"  Geocoded ................... {geocode_hits}/{total} ({pct:.1f}%)")
        self.stdout.write(f"  Geocode misses (excluded) .. {misses}")
        if unmatched:
            sample = ", ".join(f"{c}, {s}" for (c, s), _ in unmatched.most_common(10))
            self.stdout.write(
                f"  Unmatched (city,state) ..... {len(unmatched)} distinct; e.g. {sample}"
            )
