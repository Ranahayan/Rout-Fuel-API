"""Loader guard rails: bad files never empty the table, prices are stored at the
model's precision, --prune removes vanished stations, and the upsert is idempotent."""

import io
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from stations.models import FuelStation

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"


def _csv(tmp_path, name, body, header=HEADER):
    path = tmp_path / name
    path.write_text(header + body)
    return str(path)


def _load(path, **kwargs):
    out = io.StringIO()
    call_command("load_fuel_prices", csv=path, stdout=out, **kwargs)
    return out.getvalue()


@pytest.mark.django_db
def test_wrong_columns_fail_loudly_and_write_nothing(tmp_path):
    path = _csv(tmp_path, "wrong.csv", "1,A,3.0\n", header="id,name,price\n")
    with pytest.raises(CommandError, match="missing the column"):
        _load(path)
    assert FuelStation.objects.count() == 0


@pytest.mark.django_db
def test_zero_valid_rows_never_deletes_with_fresh(tmp_path):
    good = _csv(tmp_path, "good.csv", "1,A,addr,Chicago,IL,1,3.0\n2,B,addr,Springfield,IL,1,3.1\n")
    _load(good)
    assert FuelStation.objects.count() == 2
    only_canada = _csv(tmp_path, "canada.csv", "3,C,addr,Calgary,AB,1,4.0\n")
    with pytest.raises(CommandError, match="No valid US station rows"):
        _load(only_canada, fresh=True)
    assert FuelStation.objects.count() == 2  # the old data survived


@pytest.mark.django_db
def test_low_geocode_rate_aborts_before_writing(tmp_path):
    path = _csv(
        tmp_path,
        "ghost.csv",
        "1,A,addr,Chicago,IL,1,3.0\n2,G1,addr,Nowheresvillexyz,IL,1,3.0\n3,G2,addr,Nowheresvilleabc,IL,1,3.0\n",
    )
    with pytest.raises(CommandError, match="geocoded"):
        _load(path)  # 1 of 3 -> 33% < 95%
    assert FuelStation.objects.count() == 0
    _load(path, min_geocode_rate=0.3)
    assert FuelStation.objects.count() == 3


@pytest.mark.django_db
def test_prices_are_stored_at_four_decimals(tmp_path):
    path = _csv(
        tmp_path, "p.csv", "1,A,addr,Chicago,IL,1,3.00733333\n1,A,addr,Chicago,IL,1,3.00799999\n"
    )
    _load(path)
    assert FuelStation.objects.get(opis_id=1).retail_price == Decimal(
        "3.0073"
    )  # lowest, then quantised


@pytest.mark.django_db
def test_upsert_is_idempotent_and_updates_in_place(tmp_path):
    v1 = _csv(tmp_path, "v1.csv", "1,A,addr,Chicago,IL,1,3.0\n2,B,addr,Springfield,IL,1,3.1\n")
    v2 = _csv(
        tmp_path, "v2.csv", "1,A RENAMED,addr,Chicago,IL,1,2.9\n2,B,addr,Springfield,IL,1,3.1\n"
    )
    _load(v1)
    ids = set(FuelStation.objects.values_list("id", flat=True))
    _load(v2)
    assert set(FuelStation.objects.values_list("id", flat=True)) == ids  # same rows, no churn
    station = FuelStation.objects.get(opis_id=1)
    assert station.name == "A RENAMED" and station.retail_price == Decimal("2.9")


@pytest.mark.django_db
def test_prune_removes_stations_missing_from_the_file(tmp_path):
    v1 = _csv(tmp_path, "v1.csv", "1,A,addr,Chicago,IL,1,3.0\n2,B,addr,Springfield,IL,1,3.1\n")
    v2 = _csv(tmp_path, "v2.csv", "2,B,addr,Springfield,IL,1,3.1\n")
    _load(v1)
    _load(v2)
    assert FuelStation.objects.filter(opis_id=1).exists()  # default: keep
    out = _load(v2, prune=True)
    assert not FuelStation.objects.filter(opis_id=1).exists()
    assert "Pruned (absent from CSV) ... 1" in out


@pytest.mark.django_db
def test_report_shows_rate_and_unmatched(tmp_path):
    path = _csv(
        tmp_path, "r.csv", "1,A,addr,Chicago,IL,1,3.0\n2,G,addr,Nowheresvillexyz,IL,1,3.0\n"
    )
    out = _load(path, min_geocode_rate=0)
    assert "Geocoded ................... 1/2 (50.0%)" in out
    assert "Nowheresvillexyz, IL" in out
