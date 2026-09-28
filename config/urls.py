"""URL configuration for the USA Fuel-Optimal Route API."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("trips.urls")),
]
