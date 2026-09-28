"""Tests for the load_fuel_prices management command."""

import pytest
from django.core.management import call_command

from stations.models import FuelStation

CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
1,ALPHA TRUCK STOP,"I-55, EXIT 1 & US-1",Chicago,IL,100,3.500
1,ALPHA REBRAND,"I-55, EXIT 1 & US-1",Chicago,IL,100,3.200
2,BETA FUEL,Main St,Springfield,IL,101,3.100
3,CANADA STOP,Hwy 2,Calgary,AB,102,4.000
4,GHOST STATION,Rural Rd,Nowheresvillexyz,IL,103,2.900
"""


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "fuel.csv"
    path.write_text(CSV)
    return str(path)


@pytest.mark.django_db
def test_loader_dedup_filter_geocode(csv_file):
    call_command("load_fuel_prices", csv=csv_file)

    # ID 3 (Calgary, AB) dropped as non-US; IDs 1, 2, 4 remain.
    assert FuelStation.objects.count() == 3
    assert not FuelStation.objects.filter(state="AB").exists()

    # Duplicate ID 1 collapsed to the lowest price (3.200).
    alpha = FuelStation.objects.get(opis_id=1)
    assert float(alpha.retail_price) == pytest.approx(3.200)

    # Real cities geocode; the fake city does not (kept, excluded from index).
    assert FuelStation.objects.get(opis_id=1).is_geocoded is True
    assert FuelStation.objects.get(opis_id=2).is_geocoded is True
    ghost = FuelStation.objects.get(opis_id=4)
    assert ghost.is_geocoded is False
    assert ghost.latitude is None


@pytest.mark.django_db
def test_loader_is_idempotent(csv_file):
    call_command("load_fuel_prices", csv=csv_file)
    call_command("load_fuel_prices", csv=csv_file)
    assert FuelStation.objects.count() == 3
    assert FuelStation.objects.filter(opis_id=1).count() == 1


@pytest.mark.django_db
def test_loader_handles_quoted_comma_address(csv_file):
    call_command("load_fuel_prices", csv=csv_file)
    alpha = FuelStation.objects.get(opis_id=1)
    assert alpha.address == "I-55, EXIT 1 & US-1"  # comma preserved, not split
