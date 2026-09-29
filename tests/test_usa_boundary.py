"""The within-USA check: an offline point-in-polygon test against the vendored
Natural Earth boundary, with a coast-only tolerance for the coarse coastline.

Border twins are told apart, rural US points count, coastal downtowns that the
generalised coastline puts "in the water" are still accepted, and territories,
offshore water and foreign border cities are rejected.
"""

import json
import math
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from routing.providers import RouteProvider
from stations.boundary import COASTAL_TOLERANCE_MILES, USBoundary, get_us_boundary, in_usa

API = "/api/v1/route-fuel-plan/"

INSIDE = {
    # border towns
    "Buffalo NY": (42.8864, -78.8784),
    "Detroit MI": (42.3314, -83.0458),
    "El Paso TX": (31.7619, -106.4850),
    "San Diego CA": (32.7157, -117.1611),
    "Bellingham WA": (48.7519, -122.4787),
    "Nogales AZ": (31.3404, -110.9343),
    "Laredo TX": (27.5036, -99.5076),
    "Brownsville TX": (25.9017, -97.4975),
    "Calexico CA": (32.6789, -115.4989),
    "International Falls MN": (48.6011, -93.4108),
    "Sault Ste. Marie MI": (46.4953, -84.3453),
    "Niagara Falls NY": (43.0962, -79.0377),
    "Port Huron MI": (42.9709, -82.4249),
    "Derby Line VT": (45.0056, -72.0990),
    "Eastport ME": (44.9062, -66.9900),
    "Point Roberts WA (exclave)": (48.9856, -123.0779),
    # far-flung and rural
    "Key West FL": (24.5551, -81.7800),
    "Honolulu HI": (21.3069, -157.8583),
    "Anchorage AK": (61.2181, -149.9003),
    "Adak AK": (51.8800, -176.6580),
    "Attu Island AK (east of the antimeridian)": (52.9, 173.2),
    "Utqiagvik AK": (71.2906, -156.7887),
    "Old Faithful WY (30 mi from any town)": (44.4605, -110.8281),
    "Nevada desert": (39.5, -116.5),
    "Big Bend TX": (29.25, -103.25),
    "Grand Canyon AZ": (36.1069, -112.1129),
    # coastal downtowns, several of which the 1:10m coastline places just offshore
    "Miami FL": (25.7617, -80.1918),
    "Nome AK": (64.5011, -165.4064),
    "Juneau AK": (58.3019, -134.4197),
    "Charleston SC": (32.7765, -79.9311),
    "Miami Beach FL": (25.7907, -80.1300),
    "San Francisco CA": (37.7749, -122.4194),
    "Boston MA": (42.3601, -71.0589),
    "New York NY": (40.7128, -74.0060),
    "New Orleans LA": (29.9511, -90.0715),
    "Galveston TX": (29.3013, -94.7977),
    "Nantucket MA": (41.2835, -70.0995),
    "Friday Harbor WA": (48.5343, -123.0170),
    "Avalon CA (Catalina Island)": (33.3428, -118.3282),
    "Ketchikan AK": (55.3422, -131.6461),
    "Chicago IL": (41.8781, -87.6298),
    "Houston TX": (29.7604, -95.3698),
    "Washington DC": (38.9072, -77.0369),
}

OUTSIDE = {
    # foreign border twins (0.6-4 mi past a border edge)
    "Windsor ON": (42.3149, -83.0364),
    "Fort Erie ON": (42.9053, -78.9370),
    "Niagara Falls ON": (43.0896, -79.0849),
    "Sarnia ON": (42.9745, -82.4066),
    "Fort Frances ON": (48.6116, -93.4006),
    "Stanstead QC": (45.0125, -72.0989),
    "White Rock BC": (49.0253, -122.8029),
    "Tsawwassen BC": (49.0130, -123.0830),
    "Campobello Island NB": (44.8827, -66.9508),
    "Tijuana MX": (32.5149, -117.0382),
    "Ciudad Juarez MX": (31.6904, -106.4245),
    "Mexicali MX": (32.6245, -115.4523),
    "Nuevo Laredo MX": (27.4763, -99.5164),
    "Matamoros MX": (25.8690, -97.5027),
    "Nogales Sonora MX": (31.3186, -110.9459),
    "Reynosa MX": (26.0806, -98.2883),
    # farther afield
    "Toronto ON": (43.6532, -79.3832),
    "Vancouver BC": (49.2827, -123.1207),
    "Victoria BC": (48.4284, -123.3656),
    "Winnipeg MB": (49.8951, -97.1384),
    "Montreal QC": (45.5017, -73.5673),
    "Mexico City MX": (19.4326, -99.1332),
    "Havana CU": (23.1136, -82.3666),
    "Nassau BS": (25.0480, -77.3554),
    "Bermuda": (32.3078, -64.7505),
    "San Juan PR (territory)": (18.4655, -66.1057),
    "Paris FR": (48.8566, 2.3522),
    "Lake Erie, Canadian half": (42.35, -81.0),
    "Gulf of Mexico offshore": (27.0, -90.0),
    "Mid-Atlantic": (35.0, -60.0),
}


@pytest.mark.parametrize("coord", INSIDE.values(), ids=list(INSIDE))
def test_us_points_are_inside(coord):
    assert in_usa(*coord)


@pytest.mark.parametrize("coord", OUTSIDE.values(), ids=list(OUTSIDE))
def test_foreign_points_are_outside(coord):
    assert not in_usa(*coord)


@pytest.mark.xfail(
    strict=True,
    reason="1:10m generalisation: the centre of Sault Ste. Marie ON is ~0.1 mi from the "
    "border line and falls on the US side of the coarse polygon",
)
def test_known_generalisation_limit_sault_ste_marie_on():
    assert not in_usa(46.5136, -84.3358)


def test_lake_water_counts_as_us_territory():
    # Natural Earth's country polygons include lake area up to the international
    # boundary. Harmless: there is no road there for the routing provider.
    assert in_usa(43.5, -87.0)  # Lake Michigan


@pytest.mark.parametrize(
    "lat,lng", [(math.nan, -90.0), (40.0, math.inf), (200.0, -90.0), (40.0, 400.0), (0.0, 0.0)]
)
def test_non_finite_or_impossible_coordinates_are_outside(lat, lng):
    assert not in_usa(lat, lng)


def test_vendored_data_file_is_the_expected_shape():
    feature = json.loads(Path(settings.US_BOUNDARY_GEOJSON_PATH).read_text(encoding="utf-8"))
    assert feature["geometry"]["type"] == "MultiPolygon"
    assert 300 <= len(feature["geometry"]["coordinates"]) <= 400
    runs = feature["properties"]["border_edges"]
    assert runs and all(len(run) == 4 for run in runs)
    assert 1500 <= sum(last - first + 1 for _, _, first, last in runs) <= 3000  # CA + MX borders


def test_queries_are_fast_after_a_single_load():
    boundary = get_us_boundary()
    boundary.contains(40.0, -90.0)  # warm
    t0 = time.perf_counter()
    for i in range(200):
        boundary.contains(25 + (i % 100) * 0.25, -125 + (i % 100) * 0.55)
    assert (time.perf_counter() - t0) / 200 < 0.005  # 5 ms budget; typically ~0.1 ms


# --- The geometry rules themselves, on a hand-made polygon ---------------------------


@pytest.fixture
def square_with_hole(tmp_path):
    """A 1x1 degree square at (40..41 N, -90..-89 W) with a hole in its middle.
    Its west edge (x = -90) is flagged as a land border; the other three are coast."""
    outer = [[-90.0, 40.0], [-89.0, 40.0], [-89.0, 41.0], [-90.0, 41.0], [-90.0, 40.0]]
    hole = [[-89.6, 40.4], [-89.4, 40.4], [-89.4, 40.6], [-89.6, 40.6], [-89.6, 40.4]]
    feature = {
        "type": "Feature",
        "properties": {"border_edges": [[0, 0, 4, 4]]},  # edge 4: vertex 3 -> 4, the west side
        "geometry": {"type": "MultiPolygon", "coordinates": [[outer, hole]]},
    }
    path = tmp_path / "square.geojson"
    path.write_text(json.dumps(feature))
    return USBoundary(path)


def test_synthetic_inside_hole_and_far(square_with_hole):
    assert square_with_hole.contains(40.2, -89.8)  # inside
    assert not square_with_hole.contains(40.5, -89.5)  # inside the hole
    assert not square_with_hole.contains(45.0, -89.5)  # far away
    assert square_with_hole.contains(40.0, -89.5) or square_with_hole.contains(40.0001, -89.5)


def test_synthetic_coast_tolerance_but_no_border_tolerance(square_with_hole):
    one_mile_deg = 1.0 / 69.0934
    # Just off the EAST edge (coast): accepted within the tolerance, rejected beyond it.
    assert square_with_hole.contains(
        40.5, -89.0 + 0.5 * one_mile_deg / math.cos(math.radians(40.5))
    )
    assert not square_with_hole.contains(
        40.5, -89.0 + (COASTAL_TOLERANCE_MILES + 0.5) * one_mile_deg / math.cos(math.radians(40.5))
    )
    # Just off the WEST edge (land border): rejected even when very close.
    assert not square_with_hole.contains(
        40.5, -90.0 - 0.1 * one_mile_deg / math.cos(math.radians(40.5))
    )
    # Just off the SOUTH edge (coast) but near the SW corner where the border edge is
    # nearer: rejected -- the tolerance never applies across a border.
    assert not square_with_hole.contains(40.0 - 0.3 * one_mile_deg, -90.0 - 0.1 * one_mile_deg)


# --- Through the API ------------------------------------------------------------------


@pytest.fixture
def provider():
    prov = MagicMock(spec=RouteProvider)
    prov.name = "mock"
    prov.geocode.return_value = None
    with patch("routing.client.get_provider", return_value=prov):
        yield prov


@pytest.mark.parametrize(
    "start,finish,which",
    [
        (
            {"lat": 43.6532, "lng": -79.3832},
            {"lat": 42.8864, "lng": -78.8784},
            "start",
        ),  # Toronto -> Buffalo
        (
            {"lat": 29.7604, "lng": -95.3698},
            {"lat": 19.4326, "lng": -99.1332},
            "finish",
        ),  # Houston -> Mexico City
        (
            {"lat": 42.3314, "lng": -83.0458},
            {"lat": 42.3149, "lng": -83.0364},
            "finish",
        ),  # Detroit -> Windsor
    ],
    ids=["toronto-start", "mexico-city-finish", "windsor-finish"],
)
def test_api_rejects_foreign_coordinates_before_any_provider_call(provider, start, finish, which):
    resp = APIClient().post(API, {"start": start, "finish": finish}, format="json")
    assert resp.status_code == 400
    assert resp.json()["error"] == f"The {which} location is outside the USA."
    provider.route.assert_not_called()


def test_api_accepts_coastal_downtown_coordinates(provider):
    """Miami's canonical coordinate sits just off the generalised coastline; it must
    pass the USA check and reach the routing provider."""
    provider.route.side_effect = RuntimeError("stop here: the USA check passed")
    with pytest.raises(RuntimeError, match="USA check passed"):
        APIClient().post(
            API,
            {
                "start": {"lat": 25.7617, "lng": -80.1918},
                "finish": {"lat": 29.7604, "lng": -95.3698},
            },
            format="json",
        )
    provider.route.assert_called_once()
