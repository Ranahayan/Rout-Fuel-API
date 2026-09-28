"""Trip planning orchestration: one routing call -> corridor -> optimize."""

from __future__ import annotations

import hashlib
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache

from routing.client import get_route

from .corridor import find_candidates
from .optimizer import FuelCandidate, FuelPlan, optimize_fuel_plan

logger = logging.getLogger(__name__)

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
    raw = f"{_canonical_endpoint(start)}|{_canonical_endpoint(finish)}|{buffer_miles}"
    return "plan:" + hashlib.md5(raw.encode()).hexdigest()


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

    fuel_candidates = [
        FuelCandidate(mile_marker=c.mile_marker, price=c.station.price, meta=c) for c in candidates
    ]
    plan = optimize_fuel_plan(
        fuel_candidates,
        total_distance_miles=total_miles,
        range_miles=settings.VEHICLE_RANGE_MILES,
        mpg=settings.VEHICLE_MPG,
    )

    result = _serialize(route, plan)
    cache.set(key, result)
    return result


def _serialize(route, plan: FuelPlan) -> dict:
    stops = []
    total_cost = Decimal("0.00")
    for order, stop in enumerate(plan.stops, start=1):
        candidate = stop.candidate.meta  # corridor.Candidate
        station = candidate.station
        cost = _usd(stop.cost)
        total_cost += cost
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
                "gallons_purchased": round(stop.gallons, 2),
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
            "total_gallons": round(plan.total_gallons, 2),
            # Sum of the per-stop rounded costs, so the parts reconcile exactly.
            "total_cost_usd": f"{total_cost:.2f}",
        },
        "fuel_stops": stops,
    }
