"""HTTP layer: the route-fuel-plan API endpoint and the rendered Leaflet map."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.exceptions import (
    EndpointResolutionError,
    OutsideUSAError,
    RouteProviderError,
    SameEndpointError,
)

from .optimizer import InfeasibleRouteError
from .serializers import RouteFuelPlanRequestSerializer, parse_query_endpoint
from .services import plan_trip

# Bounds for the corridor buffer, shared by the serializer and the map view.
BUFFER_MIN, BUFFER_MAX = 0.5, 50.0


def _endpoint_to_query(ep) -> str:
    if isinstance(ep, dict):
        return f"{ep['lat']},{ep['lng']}"
    return str(ep)


def _build_map_url(request, start, finish, buffer_miles) -> str:
    params = {"start": _endpoint_to_query(start), "finish": _endpoint_to_query(finish)}
    if buffer_miles is not None:
        params["buffer_miles"] = buffer_miles
    return request.build_absolute_uri(reverse("trips:plan-map") + "?" + urlencode(params))


def _run_plan(request, start, finish, buffer_miles):
    """Shared planning + exception mapping. Returns (data, status_code)."""
    try:
        result = plan_trip(start, finish, buffer_miles)
    except (EndpointResolutionError, OutsideUSAError, SameEndpointError) as exc:
        return {"error": str(exc)}, status.HTTP_400_BAD_REQUEST
    except InfeasibleRouteError as exc:
        return {
            "error": str(exc),
            "infeasible_segment": {
                "from_mile": round(exc.segment_start, 1),
                "to_mile": round(exc.segment_end, 1),
            },
            "hint": "Increase buffer_miles to admit more stations near the route.",
        }, status.HTTP_422_UNPROCESSABLE_ENTITY
    except RouteProviderError as exc:
        return {"error": f"Routing provider error: {exc}"}, status.HTTP_502_BAD_GATEWAY

    result["route"]["map_url"] = _build_map_url(request, start, finish, buffer_miles)
    return result, status.HTTP_200_OK


class RouteFuelPlanView(APIView):
    """POST JSON {start, finish, buffer_miles?} or GET ?start=&finish=&buffer_miles=.

    Both verbs validate through the same serializer, so a malformed buffer or a
    missing endpoint is always a 400 (never a 500)."""

    def post(self, request):
        serializer = RouteFuelPlanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result, code = _run_plan(request, data["start"], data["finish"], data.get("buffer_miles"))
        return Response(result, status=code)

    def get(self, request):
        raw = {
            "start": parse_query_endpoint(request.query_params.get("start", "")),
            "finish": parse_query_endpoint(request.query_params.get("finish", "")),
        }
        buffer_miles = request.query_params.get("buffer_miles")
        if buffer_miles:
            raw["buffer_miles"] = buffer_miles
        serializer = RouteFuelPlanRequestSerializer(data=raw)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result, code = _run_plan(request, data["start"], data["finish"], data.get("buffer_miles"))
        return Response(result, status=code)


def _map_error(request, message, code):
    return render(request, "trips/map.html", {"error": message}, status=code)


def plan_map(request):
    """Render an interactive Leaflet map of the route + fuel stops (no API key,
    OSM tiles loaded client-side)."""
    start = request.GET.get("start")
    finish = request.GET.get("finish")
    if not start or not finish:
        return _map_error(request, "Missing 'start' or 'finish'.", 400)

    buffer_val = None
    raw_buffer = request.GET.get("buffer_miles")
    if raw_buffer:
        try:
            buffer_val = float(raw_buffer)
        except ValueError:
            return _map_error(request, "buffer_miles must be a number.", 400)
        if not (BUFFER_MIN <= buffer_val <= BUFFER_MAX):
            return _map_error(
                request, f"buffer_miles must be between {BUFFER_MIN} and {BUFFER_MAX}.", 400
            )

    try:
        result = plan_trip(parse_query_endpoint(start), parse_query_endpoint(finish), buffer_val)
    except (EndpointResolutionError, OutsideUSAError, SameEndpointError, RouteProviderError) as exc:
        return _map_error(request, str(exc), 400)
    except InfeasibleRouteError as exc:
        return _map_error(request, str(exc), 422)

    context = {
        "geometry_json": json.dumps(result["route"]["geometry"]),
        "stops_json": json.dumps(result["fuel_stops"]),
        "summary_json": json.dumps(
            {
                "route": {k: v for k, v in result["route"].items() if k != "geometry"},
                "fuel": result["fuel"],
            }
        ),
    }
    return render(request, "trips/map.html", context)
