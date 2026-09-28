from django.db import models


class FuelStation(models.Model):
    """A truck-stop fuel station loaded from the OPIS price CSV.

    The CSV has no coordinates, so ``latitude``/``longitude`` are populated at
    load time by an offline (City, State) -> centroid geocode. Rows that fail to
    geocode are kept (``is_geocoded=False``) for auditability but excluded from
    the spatial index, so they can never be selected as a fuel stop.

    ``opis_id`` is unique. The raw CSV contains duplicate OPIS IDs (often with
    conflicting prices); the loader collapses them to one row per ID (keeping the
    lowest price). The uniqueness invariant is enforced at the DB level so it
    cannot be violated by a partial load or an out-of-band write — and so
    ``update_or_create(opis_id=...)`` and the spatial index stay unambiguous.
    """

    opis_id = models.IntegerField()
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=512, blank=True)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField(null=True, blank=True)
    # Money -> Decimal. Observed range $2.687-$6.399 fits 6 digits / 4 decimals.
    retail_price = models.DecimalField(max_digits=6, decimal_places=4)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    is_geocoded = models.BooleanField(default=False, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["opis_id"], name="uniq_fuelstation_opis_id"),
        ]
        indexes = [
            models.Index(fields=["state", "city"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
