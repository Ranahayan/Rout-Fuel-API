"""URL configuration for the USA Fuel-Optimal Route API."""

from django.contrib import admin
from django.urls import include, path

from trips.health import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("trips.urls")),
    path("healthz/", healthz, name="healthz"),
]
