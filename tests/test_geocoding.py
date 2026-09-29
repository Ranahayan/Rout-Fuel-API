"""Offline geocoder: spelling normalisation, state names, comma-less and bare
inputs, principal-county selection for multi-county cities, and GET/POST parity
for "lat,lng" strings."""

from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from routing.providers import RouteProvider
from stations.geocoding import (
    CityGeocoder,
    _CityRow,
    _principal_row,
    get_default_geocoder,
    normalize_city,
    normalize_state,
)

API = "/api/v1/route-fuel-plan/"


@pytest.mark.parametrize(
    "raw,key",
    [
        ("Winston-Salem", "WINSTONSALEM"),
        ("Winston Salem", "WINSTONSALEM"),
        ("O'Fallon", "OFALLON"),
        ("O Fallon", "OFALLON"),
        ("Coeur d'Alene", "COEURDALENE"),
        ("Lee's Summit", "LEESSUMMIT"),
        ("St. Louis", "SAINTLOUIS"),
        ("Saint Louis", "SAINTLOUIS"),
        ("  ft.  worth ", "FORTWORTH"),
        ("Mt. Vernon", "MOUNTVERNON"),
        ("McDonald", "MCDONALD"),
        ("Mc Donald", "MCDONALD"),
        ("N Little Rock", "NORTHLITTLEROCK"),
        ("Brook Park", "BROOKPARK"),
        ("1st Street", "1STSTREET"),  # \bST\b must not fire inside 1ST / STREET
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_city(raw, key):
    assert normalize_city(raw) == key


@pytest.mark.parametrize(
    "raw,code",
    [
        ("IL", "IL"),
        (" il ", "IL"),
        ("Illinois", "IL"),
        ("new york", "NY"),
        ("D.C.", "DC"),
        ("District of Columbia", "DC"),
        ("Washington DC", "DC"),
        ("ZZ", None),
        ("Ontario", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_state(raw, code):
    assert normalize_state(raw) == code


def test_spelling_variants_resolve_to_the_same_place():
    g = get_default_geocoder()
    pairs = [
        ("Winston-Salem, NC", "Winston Salem, NC"),
        ("O'Fallon, IL", "O Fallon, IL"),
        ("St. Louis, MO", "Saint Louis, MO"),
        ("Ft. Worth, TX", "Fort Worth, TX"),
        ("Lee's Summit, MO", "Lees Summit, MO"),
        ("Coeur d'Alene, ID", "Coeur D Alene, ID"),
        ("Wilkes-Barre, PA", "Wilkes Barre, PA"),
    ]
    for a, b in pairs:
        assert g.geocode_place_name(a) is not None, a
        assert g.geocode_place_name(a) == g.geocode_place_name(b), (a, b)
    assert g.geocode("Brookpark", "OH") is not None  # a fuel-station city the old key missed


def test_state_names_and_comma_less_forms():
    g = get_default_geocoder()
    chicago = g.geocode_place_name("Chicago, IL")
    assert chicago is not None
    for variant in (
        "Chicago, Illinois",
        "chicago, il",
        "Chicago IL",
        "Chicago Illinois",
        "Chicago",
    ):
        assert g.geocode_place_name(variant) == chicago, variant
    assert g.geocode_place_name("Kansas City Missouri") == g.geocode_place_name("Kansas City, MO")
    assert g.geocode_place_name("New York New York") == g.geocode_place_name("New York, NY")
    assert g.geocode_place_name("Washington, D.C.") == g.geocode_place_name("Washington, DC")
    assert g.geocode_place_name("Washington District of Columbia") is not None


def test_ambiguous_bare_city_is_not_guessed():
    g = get_default_geocoder()
    assert g.geocode_place_name("Springfield") is None
    states = g.states_for_city("Springfield")
    assert len(states) > 5 and {"IL", "MO", "MA"} <= set(states)


@pytest.mark.parametrize(
    "text", ["", "   ", ",", "a, b, IL", "Nowheresville, ZZ", "Toronto, ON", "40,-90"]
)
def test_unresolvable_inputs_return_none(text):
    assert get_default_geocoder().geocode_place_name(text) is None


@pytest.mark.parametrize(
    "place,expected",
    [
        ("Orlando, FL", (28.54, -81.373)),  # Orange County, not Brevard (48 mi east)
        ("Austin, TX", (30.23, -97.714)),  # Travis
        ("Jacksonville, FL", (30.316, -81.418)),  # Duval
        ("San Antonio, TX", (29.469, -98.526)),  # Bexar
        ("Saint Paul, MN", (44.97, -93.083)),  # Ramsey
        ("Philadelphia, PA", (40.116, -75.014)),  # county named like the city
        ("Denver, CO", (39.751, -104.989)),
        ("Richmond, VA", (37.559, -77.447)),  # "Richmond City" (independent city)
        ("St. Louis, MO", (38.635, -90.191)),  # "Saint Louis City"
        ("North Charleston, SC", (32.851, -79.976)),  # county name contained in the city name
        ("New Orleans, LA", (29.96, -90.075)),  # Orleans
    ],
)
def test_multi_county_cities_use_their_principal_county(place, expected):
    lat, lng = get_default_geocoder().geocode_place_name(place)
    assert (lat, lng) == pytest.approx(expected, abs=0.01)


def test_principal_row_rules_on_synthetic_rows():
    rows = [
        _CityRow("ALPHA", 1.0, 1.0),
        _CityRow("EXAMPLEVILLE", 2.0, 2.0),
        _CityRow("ZETA", 3.0, 3.0),
    ]
    assert _principal_row("EXAMPLEVILLE", "XX", rows) == (rows[1], True)  # county == city
    rows_city = [_CityRow("ALPHA", 1.0, 1.0), _CityRow("EXAMPLEVILLECITY", 2.0, 2.0)]
    assert _principal_row("EXAMPLEVILLE", "XX", rows_city) == (rows_city[1], True)
    rows_contained = [_CityRow("ALPHA", 1.0, 1.0), _CityRow("EXAMPLE", 2.0, 2.0)]
    assert _principal_row("NORTHEXAMPLE", "XX", rows_contained) == (rows_contained[1], True)
    rows_unknown = [_CityRow("ALPHA", 1.0, 1.0), _CityRow("BETA", 2.0, 2.0)]
    assert _principal_row("SOMEWHERE", "XX", rows_unknown) == (rows_unknown[0], False)
    with patch("stations.geocoding.PRINCIPAL_COUNTY", {("SOMEWHERE", "XX"): "BETA"}):
        assert _principal_row("SOMEWHERE", "XX", rows_unknown) == (rows_unknown[1], True)


def test_geocoder_reports_first_row_fallbacks_and_skips_bad_rows(tmp_path):
    path = tmp_path / "cities.csv"
    path.write_text(
        "ID,STATE_CODE,STATE_NAME,CITY,COUNTY,LATITUDE,LONGITUDE\n"
        "1,IL,Illinois,Goodtown,Alpha,41.0,-88.0\n"
        "2,IL,Illinois,Goodtown,Beta,42.0,-88.0\n"  # unresolved duplicate -> first row
        "3,IL,Illinois,Badtown,X,not-a-number,-88.0\n"
        "4,MO,Missouri,Goodtown,Goodtown,38.0,-90.0\n"
    )
    g = CityGeocoder(csv_path=path)
    assert g.geocode("Goodtown", "IL") == (41.0, -88.0)
    assert g.geocode("Badtown", "IL") is None
    assert g.geocode("goodtown", "Missouri") == (38.0, -90.0)
    assert g.geocode_place_name("Goodtown") is None  # two states -> ambiguous
    assert g.states_for_city("Goodtown") == ["IL", "MO"]
    assert g.unresolved_duplicates == 1


# --- Through the API ------------------------------------------------------------------


@pytest.fixture
def provider():
    prov = MagicMock(spec=RouteProvider)
    prov.name = "mock"
    prov.geocode.return_value = None
    prov.route.side_effect = RuntimeError("stop: endpoints resolved")
    with patch("routing.client.get_provider", return_value=prov):
        yield prov


def _routed_coords(provider):
    (start, finish), _ = provider.route.call_args
    return start, finish


def test_lat_lng_string_means_the_same_in_post_and_get(provider):
    client = APIClient()
    with pytest.raises(RuntimeError):
        client.post(API, {"start": "40,-90", "finish": "Houston, TX"}, format="json")
    post_coords = _routed_coords(provider)
    with pytest.raises(RuntimeError):
        client.get(f"{API}?start=40,-90&finish=Houston, TX")
    assert _routed_coords(provider) == post_coords
    assert post_coords[0] == (40.0, -90.0)


def test_lat_lng_string_with_extra_parts_is_a_place_name(provider):
    resp = APIClient().post(API, {"start": "40,-90,extra", "finish": "Houston, TX"}, format="json")
    assert resp.status_code == 400
    assert "Could not resolve '40,-90,extra'" in resp.json()["error"]


def test_full_state_name_and_bare_unique_city_resolve(provider):
    client = APIClient()
    with pytest.raises(RuntimeError):
        client.post(API, {"start": "Chicago, Illinois", "finish": "Houston Texas"}, format="json")
    start, finish = _routed_coords(provider)
    assert start == pytest.approx((41.88, -87.63), abs=0.05)
    assert finish == pytest.approx((29.76, -95.37), abs=0.05)
    provider.geocode.assert_not_called()  # all offline: zero geocoder calls


def test_ambiguous_bare_city_gets_a_hint_and_no_provider_call(provider):
    resp = APIClient().post(API, {"start": "Springfield", "finish": "Houston, TX"}, format="json")
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert "several states" in error and "Springfield, " in error
    provider.geocode.assert_not_called()
    provider.route.assert_not_called()
