"""HTTP layer: the route-fuel-plan JSON endpoint and the rendered Leaflet map.

Views only validate input, call ``plan_trip`` and shape the success response.
Every failure -- domain exceptions from the routing/optimizer layers, validation
errors, throttling, wrong method/media type -- is turned into the common error
envelope by ``trips.api_errors`` (the DRF ``EXCEPTION_HANDLER``); the map page
reuses the same mapping so both entry points always agree.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

from django.urls import reverse
from rest_framework import status
from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .api_errors import error_response
from .serializers import RouteFuelPlanRequestSerializer, describe_errors, query_payload
from .services import plan_trip


def _endpoint_to_query(ep) -> str:
    if isinstance(ep, dict):
        return f"{ep['lat']},{ep['lng']}"
    return str(ep)


def _build_map_url(request, start, finish, buffer_miles) -> str:
    params = {"start": _endpoint_to_query(start), "finish": _endpoint_to_query(finish)}
    if buffer_miles is not None:
        params["buffer_miles"] = buffer_miles
    return request.build_absolute_uri(reverse("trips:plan-map") + "?" + urlencode(params))


class RouteFuelPlanView(APIView):
    """POST JSON {start, finish, buffer_miles?} or GET ?start=&finish=&buffer_miles=.

    Both verbs validate through the same serializer and share one code path."""

    def post(self, request):
        return self._plan(request, request.data)

    def get(self, request):
        return self._plan(request, query_payload(request.query_params))

    def _plan(self, request, raw) -> Response:
        serializer = RouteFuelPlanRequestSerializer(data=raw)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        buffer_miles = data.get("buffer_miles")
        result = plan_trip(data["start"], data["finish"], buffer_miles)  # errors -> api_errors
        result["route"]["map_url"] = _build_map_url(
            request, data["start"], data["finish"], buffer_miles
        )
        return Response(result)


# Content-Security-Policy for the map page. The page needs exactly: itself, the
# Leaflet assets on unpkg (pinned by Subresource Integrity in the template), the
# OpenStreetMap tile servers, and its own inline style/script, which are allowed
# by a per-request nonce rather than 'unsafe-inline'.
_MAP_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}' https://unpkg.com; "
    "style-src 'self' 'nonce-{nonce}' https://unpkg.com; "
    "img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class PlanMapView(APIView):
    """GET ?start=&finish=&buffer_miles= -> an interactive Leaflet map page of the
    route and its fuel stops (OSM tiles loaded client-side, no API key).

    This is a DRF view rather than a plain Django view on purpose: it triggers
    the same routing call and corridor work as the JSON endpoint, so it must
    share the same per-IP throttle (``DEFAULT_THROTTLE_CLASSES``)."""

    renderer_classes = [TemplateHTMLRenderer]
    template_name = "trips/map.html"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        nonce = getattr(request, "_csp_nonce", None)
        if nonce:
            response["Content-Security-Policy"] = _MAP_CSP.format(nonce=nonce)
        return response

    def get(self, request):
        request._csp_nonce = nonce = secrets.token_urlsafe(16)

        serializer = RouteFuelPlanRequestSerializer(data=query_payload(request.query_params))
        if not serializer.is_valid():
            return self._error_page(
                describe_errors(serializer.errors), status.HTTP_400_BAD_REQUEST, nonce
            )
        data = serializer.validated_data
        try:
            result = plan_trip(data["start"], data["finish"], data.get("buffer_miles"))
        except Exception as exc:
            mapped = error_response(exc)  # the same table the JSON API uses
            if mapped is None:
                raise
            payload, status_code = mapped
            return self._error_page(payload["error"], status_code, nonce)

        # Plain Python objects: the template's ``json_script`` filter serialises them
        # (and escapes ``<``/``>``/``&``) itself. Pre-dumping to a JSON string here
        # would make the browser's JSON.parse() yield a *string*, not an object.
        return Response(
            {
                "csp_nonce": nonce,
                "geometry": result["route"]["geometry"],
                "stops": result["fuel_stops"],
                "summary": {
                    "route": {k: v for k, v in result["route"].items() if k != "geometry"},
                    "fuel": result["fuel"],
                },
            }
        )

    @staticmethod
    def _error_page(message: str, code: int, nonce: str) -> Response:
        return Response({"error": message, "csp_nonce": nonce}, status=code)
