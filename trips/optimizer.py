"""Minimum-cost fuel-stop selection (the "gas station problem").

Model (documented assumptions):
  * The vehicle covers ``total_distance`` miles and has a tank holding
    ``range_miles`` of range (500), consuming fuel at ``mpg`` (10) -> a tank of
    range_miles / mpg gallons.
  * It starts and finishes with an EMPTY tank, buying ALL of the trip's fuel
    (total_distance / mpg gallons) at stations along the route. This makes
    "total money spent on fuel" the cost of the whole trip's fuel, and is the
    natural reading of the assignment ("...assuming 10 mpg").
  * Buying must happen at real stations, so the first station's purchase also
    covers the short origin -> first-station lead-in (priced at that station).
    With dense data the first station sits ~mile 0, so this is negligible.
  * Off-route detour fuel is NOT counted (small corridor buffer).

Optimal greedy (Khuller-Malekian-Mestre): standing at a station priced ``p``,
  1. if a strictly-cheaper station is reachable within range -> buy just enough
     to reach the nearest such station (never overbuy expensive fuel);
  2. else if the destination is reachable -> buy exactly enough to finish;
  3. else -> fill the tank (this is a local price minimum) and drive to the
     cheapest reachable station, then re-evaluate.
An exchange argument shows this is globally minimum-cost; tests cross-check it
against an independent linear-program solution.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

EPS = 1e-9


class InfeasibleRouteError(Exception):
    """No fueling plan exists within the vehicle's range."""

    def __init__(self, message: str, segment_start: float, segment_end: float):
        super().__init__(message)
        self.segment_start = segment_start
        self.segment_end = segment_end


@dataclass(frozen=True)
class FuelCandidate:
    mile_marker: float  # distance from route start (miles)
    price: float  # $/gallon
    meta: Any = None  # opaque payload (station details), echoed back on the stop


@dataclass
class FuelStop:
    candidate: FuelCandidate
    gallons: float
    cost: float


@dataclass
class FuelPlan:
    stops: list[FuelStop]
    total_gallons: float
    total_cost: float
    total_distance_miles: float
    range_miles: float
    mpg: float


def _check_feasibility(positions: list[float], total_distance: float, range_miles: float) -> None:
    if not positions:
        raise InfeasibleRouteError(
            "No fuel station found near the route within range.", 0.0, total_distance
        )
    if positions[0] > range_miles + EPS:
        raise InfeasibleRouteError(
            f"No fuel station within {range_miles:.0f} mi of the start "
            f"(first station at mile {positions[0]:.1f}).",
            0.0,
            positions[0],
        )
    for a, b in zip(positions, positions[1:], strict=False):
        if b - a > range_miles + EPS:
            raise InfeasibleRouteError(
                f"No fuel station within range between mile {a:.1f} and {b:.1f}.", a, b
            )
    last = positions[-1]
    if total_distance - last > range_miles + EPS:
        raise InfeasibleRouteError(
            f"Destination is more than {range_miles:.0f} mi past the last "
            f"reachable station (mile {last:.1f}).",
            last,
            total_distance,
        )


def optimize_fuel_plan(
    candidates: Sequence[FuelCandidate],
    total_distance_miles: float,
    range_miles: float,
    mpg: float,
) -> FuelPlan:
    """Return the minimum-cost fuel plan, or raise InfeasibleRouteError."""
    stations = sorted(candidates, key=lambda c: c.mile_marker)
    positions = [c.mile_marker for c in stations]
    _check_feasibility(positions, total_distance_miles, range_miles)

    gpm = 1.0 / mpg
    capacity = range_miles * gpm  # gallons
    n = len(stations)

    bought = [0.0] * n  # gallons purchased at each station
    i = 0
    fuel = 0.0  # gallons in tank, empty at the first station

    while True:
        pos = positions[i]
        price = stations[i].price
        reach = pos + range_miles

        # Nearest strictly-cheaper station within range.
        cheaper = None
        for j in range(i + 1, n):
            if positions[j] > reach + EPS:
                break
            if stations[j].price < price - EPS:
                cheaper = j
                break

        dest_reachable = total_distance_miles <= reach + EPS

        if cheaper is not None:
            need = (positions[cheaper] - pos) * gpm
            buy = max(0.0, need - fuel)
            bought[i] += buy
            fuel += buy
            fuel -= (positions[cheaper] - pos) * gpm
            i = cheaper
        elif dest_reachable:
            need = (total_distance_miles - pos) * gpm
            buy = max(0.0, need - fuel)
            bought[i] += buy
            break
        else:
            # Fill the tank at this local price minimum, then go to the cheapest
            # reachable station to re-evaluate.
            buy = max(0.0, capacity - fuel)
            bought[i] += buy
            fuel += buy
            best_j = None
            best_price = float("inf")
            for j in range(i + 1, n):
                if positions[j] > reach + EPS:
                    break
                if stations[j].price < best_price - EPS:
                    best_price = stations[j].price
                    best_j = j
            # Feasibility guarantees at least one station within range here.
            fuel -= (positions[best_j] - pos) * gpm
            i = best_j

    # Fold the origin -> first-station lead-in into the first station we actually
    # buy at (priced there). With dense data the first station sits ~mile 0, so
    # this is a few thousandths of a gallon; folding it onto a real purchase
    # avoids emitting a spurious $0.00 stop.
    lead_in = positions[0] * gpm
    if lead_in > EPS:
        first_purchase = next((k for k, g in enumerate(bought) if g > EPS), 0)
        bought[first_purchase] += lead_in

    stops: list[FuelStop] = []
    total_cost = 0.0
    total_gallons = 0.0
    for idx, gallons in enumerate(bought):
        if gallons <= EPS:
            continue
        cost = gallons * stations[idx].price
        total_cost += cost
        total_gallons += gallons
        stops.append(FuelStop(candidate=stations[idx], gallons=gallons, cost=cost))

    return FuelPlan(
        stops=stops,
        total_gallons=total_gallons,
        total_cost=total_cost,
        total_distance_miles=total_distance_miles,
        range_miles=range_miles,
        mpg=mpg,
    )
