from django.urls import path

from . import views

app_name = "trips"

urlpatterns = [
    path("route-fuel-plan/", views.RouteFuelPlanView.as_view(), name="plan"),
    path("route-fuel-plan/map/", views.plan_map, name="plan-map"),
]
