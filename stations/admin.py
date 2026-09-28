from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "retail_price", "is_geocoded")
    list_filter = ("state", "is_geocoded")
    search_fields = ("name", "city", "opis_id")
