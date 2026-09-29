"""Liveness/readiness probe: ``GET /healthz``.

Returns 200 when the service can plan trips (database reachable, stations
loaded) and 503 otherwise, with a small JSON body operators can read at a
glance. It is a plain Django view: probes must not be throttled, and it is
exempt from the HTTPS redirect (``SECURE_REDIRECT_EXEMPT``) because load
balancers probe over plain HTTP.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from routing.providers import get_provider
from stations.models import FuelStation
from trips import spatial_index

logger = logging.getLogger(__name__)


@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    body: dict = {"status": "ok", "checks": {}}
    stations = 0
    try:
        stations = FuelStation.objects.filter(is_geocoded=True).count()
        body["checks"]["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - a probe must answer, whatever broke
        logger.warning("healthz: database check failed: %s", exc)
        body["checks"]["database"] = f"error ({type(exc).__name__})"
        body["status"] = "unavailable"
    body["stations_indexed"] = stations
    if stations == 0 and body["status"] == "ok":
        body["status"] = "degraded"
        body["hint"] = "No geocoded stations; run `python manage.py load_fuel_prices`."
    body["spatial_index_built"] = spatial_index._index is not None
    body["routing_provider"] = get_provider().name

    cache.set("healthz:probe", "1", 10)  # SafeRedisCache never raises; LocMem never fails
    body["checks"]["cache"] = "ok" if cache.get("healthz:probe") == "1" else "unavailable"

    return JsonResponse(body, status=200 if body["status"] == "ok" else 503)
