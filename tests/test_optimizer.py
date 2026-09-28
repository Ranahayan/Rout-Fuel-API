"""Tests for the minimum-cost fuel optimizer.

Includes hand-computed cases and a property test that cross-checks the greedy
against an independent linear-program solution on thousands of random instances.
"""

import random

import pytest
from scipy.optimize import linprog

from trips.optimizer import (
    FuelCandidate,
    InfeasibleRouteError,
    optimize_fuel_plan,
)

R = 500.0
MPG = 10.0


def _cands(pairs):
    return [FuelCandidate(mile_marker=p, price=c, meta=i) for i, (p, c) in enumerate(pairs)]


def test_total_gallons_equals_distance_over_mpg():
    plan = optimize_fuel_plan(_cands([(0, 3.0), (400, 2.5)]), 700, R, MPG)
    assert plan.total_gallons == pytest.approx(700 / MPG)


def test_buy_just_enough_to_reach_cheaper_station():
    # Start pricey, a cheaper station 100 mi ahead, destination at 150 mi.
    # Buy 10 gal @3.00 to reach the cheaper one, then 5 gal @2.50 to finish.
    plan = optimize_fuel_plan(_cands([(0, 3.00), (100, 2.50)]), 150, R, MPG)
    assert plan.total_cost == pytest.approx(10 * 3.00 + 5 * 2.50)
    assert [round(s.gallons, 4) for s in plan.stops] == [10.0, 5.0]


def test_fill_up_when_no_cheaper_in_range():
    # Cheapest is first; later stations are pricier. Fill at the cheap origin
    # station as far as the tank allows (and lead-in is at mile 0 => 0 gal extra).
    plan = optimize_fuel_plan(_cands([(0, 2.00), (450, 5.00)]), 900, R, MPG)
    # Total fuel must be 90 gal; the cheap 50 gal tank is filled at the origin.
    assert plan.total_gallons == pytest.approx(90.0)
    assert plan.stops[0].gallons == pytest.approx(50.0)  # filled the tank @2.00
    assert plan.stops[1].gallons == pytest.approx(40.0)  # remainder @5.00


def test_exact_range_boundary_is_feasible():
    # Gaps of exactly 500 are allowed.
    plan = optimize_fuel_plan(_cands([(0, 3.0), (500, 3.0)]), 1000, R, MPG)
    assert plan.total_gallons == pytest.approx(100.0)


def test_single_short_trip_one_stop():
    plan = optimize_fuel_plan(_cands([(0, 3.123)]), 120, R, MPG)
    assert len(plan.stops) == 1
    assert plan.total_cost == pytest.approx(12.0 * 3.123)


def test_infeasible_when_first_station_out_of_range():
    with pytest.raises(InfeasibleRouteError):
        optimize_fuel_plan(_cands([(600, 3.0)]), 700, R, MPG)


def test_infeasible_gap_between_stations():
    with pytest.raises(InfeasibleRouteError) as exc:
        optimize_fuel_plan(_cands([(0, 3.0), (600, 3.0)]), 700, R, MPG)
    assert exc.value.segment_start == 0
    assert exc.value.segment_end == 600


def test_infeasible_destination_past_last_station():
    with pytest.raises(InfeasibleRouteError):
        optimize_fuel_plan(_cands([(0, 3.0), (100, 3.0)]), 650, R, MPG)


def test_no_candidates_is_infeasible():
    with pytest.raises(InfeasibleRouteError):
        optimize_fuel_plan([], 100, R, MPG)


# --- Independent LP reference + property test (proves optimality) ---


def _lp_min_cost(positions, prices, D, R=R, mpg=MPG):
    n = len(positions)
    gpm = 1.0 / mpg
    cap = R * gpm
    p = positions
    A_ub, b_ub = [], []
    for i in range(n):  # tank after buying at i <= capacity
        A_ub.append([1.0 if k <= i else 0.0 for k in range(n)])
        b_ub.append(cap + gpm * (p[i] - p[0]))
    for i in range(n):  # fuel on arrival at i >= 0 (purchases strictly before i)
        A_ub.append([-1.0 if k < i else 0.0 for k in range(n)])
        b_ub.append(-gpm * (p[i] - p[0]))
    A_ub.append([-1.0] * n)  # arrive destination with >= 0
    b_ub.append(-gpm * (D - p[0]))
    res = linprog(prices, A_ub=A_ub, b_ub=b_ub, bounds=[(0, None)] * n, method="highs")
    assert res.success, res.message
    return res.fun + p[0] * gpm * prices[0]  # + origin -> first-station lead-in


def test_greedy_matches_lp_on_random_instances():
    rng = random.Random(2024)
    for _ in range(2000):
        n = rng.randint(1, 9)
        positions = [round(rng.uniform(0, R), 2)]
        for _ in range(n - 1):
            positions.append(round(positions[-1] + rng.uniform(1, R), 2))
        D = round(positions[-1] + rng.uniform(0, R), 2)
        prices = [round(rng.uniform(2.5, 6.0), 3) for _ in positions]
        plan = optimize_fuel_plan(_cands(list(zip(positions, prices, strict=False))), D, R, MPG)
        assert plan.total_gallons == pytest.approx(D / MPG, abs=1e-4)
        assert plan.total_cost == pytest.approx(_lp_min_cost(positions, prices, D), abs=1e-3)
