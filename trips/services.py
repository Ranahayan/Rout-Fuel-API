"""Trip planning orchestration: one routing call -> corridor -> optimize."""

from __future__ import annotations

import hashlib
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache

from routing.client import get_route

from .corridor import find_candidates
from .optimizer import FuelCandidate, FuelPlan, InfeasibleRouteError, optimize_fuel_plan
from .spatial_index import get_index

logger = logging.getLogger(__name__)


class NoStationsLoadedError(Exception):
    """The station table is empty: ``load_fuel_prices`` has not been run here."""


_CENTS = Decimal("0.01")


def _usd(amount: float) -> Decimal:
    """Quantize a dollar amount to cents (the model's source-of-truth price is a
    Decimal; the optimizer works in float for speed, so money is rounded back to
    cents here, where per-stop costs sum exactly to the reported total)."""
    return Decimal(str(amount)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _canonical_endpoint(ep) -> str:
    """Normalize an endpoint to a stable cache token so the same physical route
    keys identically regardless of input form (dict key order, coordinate string
    vs. object, or place-name casing/whitespace)."""
    if isinstance(ep, dict):
        return f"coord:{round(float(ep['lat']), 4)},{round(float(ep['lng']), 4)}"
    return "place:" + " ".join(str(ep).strip().lower().split())


def _plan_cache_key(start, finish, buffer_miles) -> str:
    # Everything that shapes a plan: endpoints, corridor width, and the vehicle /
    # provider settings (so changing them with a shared Redis cannot serve a
    # stale plan for the cache TTL).
    raw = (
        f"{_canonical_endpoint(start)}|{_canonical_endpoint(finish)}|{buffer_miles}|"
        f"{settings.VEHICLE_RANGE_MILES}|{settings.VEHICLE_MPG}|{settings.ROUTING_PROVIDER}"
    )
    return "plan:" + hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


def plan_trip(start, finish, buffer_miles: float | None = None) -> dict:
    """Resolve the route, pick cost-optimal fuel stops, and assemble the response.

    Cached by (start, finish, buffer); on a cache hit no routing call is made.
    May raise routing.exceptions.* or optimizer.InfeasibleRouteError.
    """
    if buffer_miles is None:
        buffer_miles = settings.CORRIDOR_BUFFER_MILES

    key = _plan_cache_key(start, finish, buffer_miles)
    cached = cache.get(key)
    if cached is not None:
        logger.debug("Plan cache hit (%s)", key)
        return cached

    route = get_route(start, finish)
    candidates, total_miles = find_candidates(route, buffer_miles)
    if not candidates and get_index().station_count == 0:
        raise NoStationsLoadedError(
            "No fuel stations are loaded on this server; run `python manage.py load_fuel_prices`."
        )

    fuel_candidates = [
        FuelCandidate(mile_marker=c.mile_marker, price=c.station.price, meta=c) for c in candidates
    ]
    try:
        plan = optimize_fuel_plan(
            fuel_candidates,
            total_distance_miles=total_miles,
            range_miles=settings.VEHICLE_RANGE_MILES,
            mpg=settings.VEHICLE_MPG,
        )
    except InfeasibleRouteError as exc:
        exc.buffer_miles = (
            buffer_miles  # let the HTTP layer judge whether a wider buffer could help
        )
        raise

    result = _serialize(route, plan)
    cache.set(key, result)
    return result


def _serialize(route, plan: FuelPlan) -> dict:
    stops = []
    total_cost = Decimal("0.00")
    total_gallons = 0.0
    for order, stop in enumerate(plan.stops, start=1):
        candidate = stop.candidate.meta  # corridor.Candidate
        station = candidate.station
        cost = _usd(stop.cost)
        total_cost += cost
        gallons = round(stop.gallons, 2)
        total_gallons += gallons
        stops.append(
            {
                "order": order,
                "opis_id": station.opis_id,
                "name": station.name,
                "city": station.city,
                "state": station.state,
                "location": {"lat": round(station.lat, 6), "lng": round(station.lng, 6)},
                "route_mile_marker": round(stop.candidate.mile_marker, 1),
                "detour_miles": round(candidate.detour_miles, 1),
                "price_per_gallon": f"{station.price:.3f}",
                "gallons_purchased": gallons,
                "cost_usd": f"{cost:.2f}",
            }
        )

    start = {"lat": route.start[0], "lng": route.start[1]}
    finish = {"lat": route.finish[0], "lng": route.finish[1]}
    # Disclose where a fuzzy place-name match actually resolved to (e.g.
    # "Toronto, ON" -> "Toronto, OH, USA"), so the caller can verify it.
    if route.start_resolved_from:
        start["resolved_from"] = route.start_resolved_from
    if route.finish_resolved_from:
        finish["resolved_from"] = route.finish_resolved_from

    return {
        "route": {
            "geometry": route.geometry,
            "total_distance_miles": round(route.total_distance_miles, 1),
            "duration_minutes": round(route.duration_minutes, 1),
            "provider": route.provider,
            "start": start,
            "finish": finish,
            # map_url is attached by the view (needs the request to build it).
        },
        "fuel": {
            "mpg": plan.mpg,
            "tank_range_miles": plan.range_miles,
            "start_tank_assumption": "empty",
            # Sums of the per-stop rounded figures, so the parts reconcile exactly
            # (the unrounded total is distance / mpg to within a cent-gallon per stop).
            "total_gallons": round(total_gallons, 2),
            "total_cost_usd": f"{total_cost:.2f}",
        },
        "fuel_stops": stops,
    }
