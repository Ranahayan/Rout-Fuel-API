"""Runtime-configuration checks, run by ``manage.py check`` / ``runserver`` / ``migrate``.

Misconfiguration should fail at boot with a clear message, not as a 500 on the
first request: ``VEHICLE_MPG=0`` would raise ZeroDivisionError inside the
optimizer, a wrong ``US_CITIES_CSV_PATH`` a FileNotFoundError inside the
geocoder, ``ROUTING_PROVIDER=ors`` without a key a 502 on every trip, and
``REDIS_URL`` without the ``redis`` package a 500 on every request (DRF's
throttle touches the cache before any view runs). Registered from
``TripsConfig.ready()``.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core import checks

from trips.serializers import BUFFER_MILES_MAX, BUFFER_MILES_MIN

# DRF rate syntax: "<count>/<period>" where the period starts with s, m, h or d.
_THROTTLE_RATE = re.compile(r"^\d+/[smhd][a-z]*$")


def _error(message: str, code: str, hint: str | None = None) -> checks.Error:
    return checks.Error(message, hint=hint, id=f"trips.{code}")


@checks.register("configuration")
def check_runtime_configuration(app_configs, **kwargs):
    s = settings
    problems: list[checks.CheckMessage] = []

    # --- Algorithm tunables ---
    if not s.VEHICLE_MPG > 0:
        problems.append(_error(f"VEHICLE_MPG must be > 0 (got {s.VEHICLE_MPG!r}).", "E001"))
    if not s.VEHICLE_RANGE_MILES > 0:
        problems.append(
            _error(f"VEHICLE_RANGE_MILES must be > 0 (got {s.VEHICLE_RANGE_MILES!r}).", "E002")
        )
    if not BUFFER_MILES_MIN <= s.CORRIDOR_BUFFER_MILES <= BUFFER_MILES_MAX:
        problems.append(
            _error(
                f"CORRIDOR_BUFFER_MILES must be within [{BUFFER_MILES_MIN}, {BUFFER_MILES_MAX}] "
                f"(got {s.CORRIDOR_BUFFER_MILES!r}).",
                "E003",
            )
        )

    # --- Routing provider ---
    if not s.ROUTING_TIMEOUT_SECONDS > 0:
        problems.append(
            _error(
                f"ROUTING_TIMEOUT_SECONDS must be > 0 (got {s.ROUTING_TIMEOUT_SECONDS!r}).", "E004"
            )
        )
    if not s.ROUTING_TOTAL_DEADLINE_SECONDS > 0:
        problems.append(
            _error(
                "ROUTING_TOTAL_DEADLINE_SECONDS must be > 0 "
                f"(got {s.ROUTING_TOTAL_DEADLINE_SECONDS!r}).",
                "E005",
            )
        )
    if s.ROUTING_FAILURE_CACHE_SECONDS < 0:
        problems.append(
            _error(
                "ROUTING_FAILURE_CACHE_SECONDS must be >= 0 "
                f"(got {s.ROUTING_FAILURE_CACHE_SECONDS!r}).",
                "E006",
            )
        )
    provider = (s.ROUTING_PROVIDER or "").strip().lower()
    if provider not in ("", "ors", "osrm"):
        problems.append(
            _error(
                f"ROUTING_PROVIDER must be 'ors', 'osrm' or empty (got {s.ROUTING_PROVIDER!r}).",
                "E007",
            )
        )
    if provider == "ors" and not s.ORS_API_KEY:
        problems.append(
            _error(
                "ROUTING_PROVIDER=ors requires ORS_API_KEY.",
                "E008",
                hint="Get a free key at https://openrouteservice.org/dev/#/signup or use osrm.",
            )
        )
    for name in ("ORS_BASE_URL", "OSRM_BASE_URL"):
        if not str(getattr(s, name)).startswith(("http://", "https://")):
            problems.append(
                _error(
                    f"{name} must start with http:// or https:// (got {getattr(s, name)!r}).",
                    "E009",
                )
            )

    # --- Data files ---
    for name in ("US_CITIES_CSV_PATH", "US_BOUNDARY_GEOJSON_PATH"):
        if not Path(getattr(s, name)).is_file():
            problems.append(
                _error(
                    f"{name} does not exist: {getattr(s, name)!r} (needed to serve requests).",
                    "E010",
                )
            )
    if not Path(s.FUEL_CSV_PATH).is_file():
        problems.append(
            checks.Warning(
                f"FUEL_CSV_PATH does not exist: {s.FUEL_CSV_PATH!r}.",
                hint="Only `manage.py load_fuel_prices` needs it; pass --csv to point elsewhere.",
                id="trips.W001",
            )
        )

    # --- Cache / throttling ---
    if s.CACHES["default"]["BACKEND"].endswith("RedisCache"):
        try:
            import redis  # noqa: F401
        except ImportError:
            problems.append(
                _error(
                    "REDIS_URL is set but the 'redis' package is not installed.",
                    "E011",
                    hint="pip install -r requirements.lock",
                )
            )
    rates = s.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
    for scope, rate in rates.items():
        if rate and not _THROTTLE_RATE.match(str(rate)):
            problems.append(
                _error(
                    f"Throttle rate for '{scope}' must look like '30/min' (got {rate!r}).", "E012"
                )
            )
    num_proxies = s.REST_FRAMEWORK.get("NUM_PROXIES")
    if num_proxies is not None and num_proxies < 0:
        problems.append(_error(f"NUM_PROXIES must be >= 0 (got {num_proxies!r}).", "E013"))

    return problems
