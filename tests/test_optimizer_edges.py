"""Optimizer edge cases the review found under-tested: co-located stations
(every station in a city shares its centroid), equal prices, stations past the
destination, random infeasible instances, and an independent LP reference that
does not share the repo oracle's lead-in assumption."""

import random

import pytest
from scipy.optimize import linprog

from trips.optimizer import FuelCandidate, InfeasibleRouteError, optimize_fuel_plan

R, MPG = 500.0, 10.0
EPS = 1e-9


def _cands(pairs):
    return [FuelCandidate(mile_marker=p, price=c, meta=i) for i, (p, c) in enumerate(pairs)]


def _lp_trip_cost(positions, prices, distance):
    """Independent LP for the trip cost starting EMPTY at the first station; stations
    beyond the destination are excluded (they cannot help)."""
    keep = [k for k, p in enumerate(positions) if p <= distance + EPS]
    p = [positions[k] for k in keep]
    c = [prices[k] for k in keep]
    n, gpm, cap = len(p), 1.0 / MPG, R / MPG
    a_ub, b_ub = [], []
    for i in range(n):
        a_ub.append([1.0 if k <= i else 0.0 for k in range(n)])  # tank after buying at i <= cap
        b_ub.append(cap + gpm * (p[i] - p[0]))
        a_ub.append([-1.0 if k < i else 0.0 for k in range(n)])  # arrive at i with fuel >= 0
        b_ub.append(-gpm * (p[i] - p[0]))
    a_ub.append([-1.0] * n)  # arrive at the destination with fuel >= 0
    b_ub.append(-gpm * (distance - p[0]))
    res = linprog(c, A_ub=a_ub, b_ub=b_ub, bounds=[(0, None)] * n, method="highs")
    assert res.success, res.message
    return res.fun


def _reference_total(positions, prices, distance):
    """LP trip cost + the origin -> first-station lead-in bought at the CHEAPEST
    station of the co-located cluster at the first marker."""
    first = min(positions)
    cluster_min = min(pr for p, pr in zip(positions, prices, strict=True) if abs(p - first) < EPS)
    return _lp_trip_cost(positions, prices, distance) + (first / MPG) * cluster_min


def test_colocated_cheaper_station_gets_the_lead_in():
    plan = optimize_fuel_plan(_cands([(50, 3.0), (50, 2.0)]), 150, R, MPG)
    assert len(plan.stops) == 1
    assert plan.stops[0].candidate.price == 2.0
    assert plan.total_gallons == pytest.approx(15.0)
    assert plan.total_cost == pytest.approx(30.0)


def test_equal_prices_everywhere_cost_is_independent_of_the_plan():
    plan = optimize_fuel_plan(_cands([(0, 3.0), (200, 3.0), (400, 3.0), (600, 3.0)]), 800, R, MPG)
    assert plan.total_gallons == pytest.approx(80.0)
    assert plan.total_cost == pytest.approx(240.0)


def test_station_past_the_destination_is_ignored():
    # Reachable trip: a far station must neither make it infeasible...
    plan = optimize_fuel_plan(_cands([(0, 3.0), (700, 2.0)]), 100, R, MPG)
    assert plan.total_gallons == pytest.approx(10.0)
    assert [s.candidate.mile_marker for s in plan.stops] == [0]
    # ...nor tempt the greedy into buying fuel to reach a cheaper station it will never visit.
    plan = optimize_fuel_plan(_cands([(0, 3.0), (100, 3.0), (200, 2.0)]), 150, R, MPG)
    assert plan.total_gallons == pytest.approx(15.0)
    assert plan.total_cost == pytest.approx(45.0)


def test_greedy_matches_independent_lp_with_clusters_ties_and_far_stations():
    rng = random.Random(99)
    for _ in range(1500):
        n = rng.randint(1, 14)
        positions = [round(rng.uniform(0, R), 2)]
        for _ in range(n - 1):
            step = rng.choice(
                [0.0, 0.0, rng.uniform(0.01, R)]
            )  # two-in-three chance of co-location
            positions.append(round(positions[-1] + step, 2))
        distance = round(positions[-1] + rng.uniform(0, R), 2)
        if rng.random() < 0.3:  # sometimes add stations beyond the destination
            positions += [round(distance + rng.uniform(1, R), 2) for _ in range(rng.randint(1, 3))]
        pool = [round(rng.uniform(2.5, 6.0), 2) for _ in range(max(1, len(positions) // 2))]
        prices = [rng.choice(pool) for _ in positions]  # frequent equal prices
        order = list(range(len(positions)))
        rng.shuffle(order)  # input order must not matter
        plan = optimize_fuel_plan(
            _cands([(positions[k], prices[k]) for k in order]), distance, R, MPG
        )
        assert plan.total_gallons == pytest.approx(distance / MPG, abs=1e-6)
        assert plan.total_cost == pytest.approx(
            _reference_total(positions, prices, distance), abs=1e-6
        )


def test_random_infeasible_instances_report_the_offending_gap():
    rng = random.Random(5)
    for _ in range(300):
        n = rng.randint(2, 8)
        positions = [round(rng.uniform(0, R), 2)]
        for _ in range(n - 1):
            positions.append(round(positions[-1] + rng.uniform(1, R), 2))
        gap_at = rng.randrange(1, n)
        bump = R + rng.uniform(0.01, 200)
        positions = positions[:gap_at] + [p + bump for p in positions[gap_at:]]
        distance = positions[-1] + rng.uniform(0, R)
        with pytest.raises(InfeasibleRouteError) as exc:
            optimize_fuel_plan(_cands([(p, 3.0) for p in positions]), distance, R, MPG)
        assert (exc.value.segment_start, exc.value.segment_end) == (
            positions[gap_at - 1],
            positions[gap_at],
        )


def test_exact_range_boundary_with_float_noise_is_feasible():
    plan = optimize_fuel_plan(_cands([(0.1, 3.0), (500.1 + 5e-10, 3.0)]), 1000.1, R, MPG)
    assert plan.total_gallons == pytest.approx(100.01)
